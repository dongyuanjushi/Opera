"""Shared helpers for the agent harnesses: the trial's time budget, container-side install env,
the critic proxy's address and conversation channel, and the launcher that starts the proxy.

A harness routes its agent through the proxy by pointing the LLM base URL at it, passing a conversation id and
step budget (headers, or the ``/c/<session>/<max_turns>/v1`` URL channel) and a placeholder API key.
"""
from __future__ import annotations

import atexit
import json
import math
import os
import socket
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from typing import Any

import yaml

OPERA_ROOT = Path(__file__).resolve().parents[1]

EXEC_TIMEOUT_MARGIN_S = 60.0         # a pier exec outlives pier's own wait_for, so pier raises the clean AgentTimeoutError first


def trial_agent_budget_s(logs_dir: str | Path | None) -> float | None:
    """The agent's wall-clock budget in seconds for this trial, or None when it cannot be resolved.

    ``logs_dir`` is the trial's agent log dir; its parent holds the trial ``config.json``, whose agent timeout
    override, cap and multiplier are applied to the task's ``[agent] timeout_sec``.
    """
    if not logs_dir:
        return None
    cfg = Path(logs_dir).resolve().parent / "config.json"
    try:
        c = json.loads(cfg.read_text(encoding="utf-8"))
        agent = c.get("agent") or {}
        base = agent.get("override_timeout_sec")
        if base is None:
            task_path = (c.get("task") or {}).get("path")
            with open(Path(task_path) / "task.toml", "rb") as fh:
                base = (tomllib.load(fh).get("agent") or {}).get("timeout_sec")
        cap = agent.get("max_timeout_sec") or float("inf")
        multiplier = c.get("agent_timeout_multiplier")
        if multiplier is None:
            multiplier = c.get("timeout_multiplier", 1.0)
        budget = min(float(base), float(cap)) * float(multiplier)
    except Exception:
        return None
    return budget if math.isfinite(budget) and budget > 0 else None


def pier_exec_timeout_s(logs_dir: str | Path | None) -> int | None:
    """The exec deadline (seconds) a pier harness passes with its agent command: the trial budget plus a margin,
    because the pier environment otherwise applies a flat default. None when the budget is unknown."""
    budget = trial_agent_budget_s(logs_dir)
    return int(budget + EXEC_TIMEOUT_MARGIN_S) if budget else None

LOCK_PATH = Path(__file__).resolve().parent / "harnesses.lock.yaml"
# Optional LAN artifact mirrors, passed to the container's install steps through the agent env.
PYPI_INDEX_ENV = "OPERA_PYPI_INDEX_URL"            # e.g. http://<host>:3141/index/
UV_PYTHON_MIRROR_ENV = "OPERA_UV_PYTHON_MIRROR"     # e.g. http://<host>:8081/python-build-standalone/
UV_INSTALLER_MIRROR_ENV = "OPERA_UV_INSTALLER_BASE_URL"   # e.g. http://<host>:8081/github  (uv release tarballs)


def load_lock(path: Path = LOCK_PATH) -> dict[str, str]:
    """The pinned container-side package versions (``plugins/harnesses.lock.yaml``) as ``{name: version}``."""
    return {k: str(v) for k, v in (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).items()}


def install_env(environ: dict[str, str] | None = None) -> dict[str, str]:
    """uv/pip env for the container-side installs: quiet output, plus the LAN mirrors set in ``environ``
    (default: this process's environment)."""
    src = os.environ if environ is None else environ
    env = {"UV_NO_PROGRESS": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1"}
    if idx := src.get(PYPI_INDEX_ENV):
        env.update({"UV_DEFAULT_INDEX": idx, "UV_INDEX_URL": idx, "PIP_INDEX_URL": idx})
    if m := src.get(UV_PYTHON_MIRROR_ENV):
        env["UV_PYTHON_INSTALL_MIRROR"] = m
    if m := src.get(UV_INSTALLER_MIRROR_ENV):
        env["UV_INSTALLER_GITHUB_BASE_URL"] = m
    return env

PROXY_URL_ENV = "OPERA_CRITIC_PROXY_URL"          # e.g. http://<host>:8790
DUMMY_API_KEY = "opera-critic-proxy"              # the agent's placeholder key; the proxy holds the real one


def resolve_proxy_url(explicit: str | None = None) -> str | None:
    """The critic proxy's root URL (``explicit``, else ``$OPERA_CRITIC_PROXY_URL``) without a trailing slash, or None."""
    url = explicit or os.environ.get(PROXY_URL_ENV)
    return url.rstrip("/") if url else None


def channel_base_url(proxy_url: str, session: str, max_turns: int | None) -> str:
    """LLM base URL for agents that cannot set headers: the conversation id and the step budget
    (``max_turns``; None or 0 = no cap) ride in the path."""
    return f"{proxy_url}/c/{session}/{max_turns or 0}/v1"


def plain_base_url(proxy_url: str) -> str:
    """LLM base URL for agents that identify the conversation through request headers."""
    return f"{proxy_url}/v1"


def trial_session_id(logs_dir) -> str:
    """The conversation id for a trial: the trial dir name (``<task>__<id>``), taken from its ``…/agent`` logs dir."""
    from pathlib import Path as _P
    p = _P(str(logs_dir)).resolve()
    return p.parent.name if p.name == "agent" else p.name


def resolve_critic_run(config: str | Path, *, critic_model: str | None = None, strategy: str | None = None,
                       reasoning_effort: str | None = None, harness: str | None = None,
                       overrides: dict[str, Any] | None = None) -> tuple[Any, str]:
    """Load the critic config as the proxy will and return ``(cfg, job-id suffix)``.

    ``config`` is the critic policy YAML; ``critic_model`` a ``configs/critic-models.yaml`` preset, ``strategy`` a
    ``configs/strategies`` name, ``harness`` the critic's harness pack, ``overrides`` policy-field overrides. The
    suffix is ``-critic-<strategy>-<model tag>`` (``-critic-passthrough`` for the no-review control)."""
    import sys as _sys
    if str(OPERA_ROOT / "src") not in _sys.path:
        _sys.path.insert(0, str(OPERA_ROOT / "src"))
    from critics.config import PASSTHROUGH_STRATEGY, CriticConfig
    cfg = CriticConfig.from_yaml(config, critic_model=critic_model, strategy=strategy, reasoning_effort=reasoning_effort,
                                 harness=harness, overrides=overrides or None)
    suffix = f"-critic-{cfg.strategy}" + ("" if cfg.strategy == PASSTHROUGH_STRATEGY else f"-{cfg.model_tag}")
    return cfg, suffix


def start_critic_proxy(config: str | Path, *, upstream: str, upstream_key_env: str | None, log_dir: Path,
                       port: int | None = None, critic_model: str | None = None, strategy: str | None = None,
                       upstream_auth: str = "bearer", python: str = sys.executable,
                       proxy_workers: int | None = None, critic_reasoning_effort: str | None = None,
                       record_prompts: bool = False, record_requests: bool = False,
                       harness: str | None = None, review: bool = True,
                       reasoning_passback: bool = False, reasoning_reinject: bool = False,
                       turn_reminder: str | None = None) -> tuple[subprocess.Popen, str]:
    """Start ``python -m critics.proxy`` for one job and wait until it is healthy.

    ``upstream`` is the agent's real LLM endpoint and ``upstream_key_env`` the env var holding its key
    (``upstream_auth``: how the key is sent, default ``bearer``); ``critic_model`` / ``strategy`` /
    ``critic_reasoning_effort`` / ``harness`` override the critic config; ``review=False`` runs a passthrough
    proxy without a critic; ``port`` None picks a free one. The remaining flags are forwarded to the proxy CLI.
    Logs go under ``log_dir``. Returns ``(process, root URL on this host's routable IP)``; the process is
    terminated at interpreter exit."""
    from critics.proxy import routable_ip
    if port is None:
        with socket.socket() as s:
            s.bind(("0.0.0.0", 0)); port = s.getsockname()[1]
    log_dir = Path(log_dir); log_dir.mkdir(parents=True, exist_ok=True)
    cmd = [python, "-m", "critics.proxy", "--config", str(config), "--upstream", upstream, "--host", "0.0.0.0",
           "--port", str(port), "--log-dir", str(log_dir), "--upstream-auth", upstream_auth]
    if upstream_key_env:
        cmd += ["--upstream-key-env", upstream_key_env]
    if critic_model:
        cmd += ["--critic-model", critic_model]
    if strategy:
        cmd += ["--critic-strategy", strategy]
    if critic_reasoning_effort:
        cmd += ["--critic-reasoning-effort", critic_reasoning_effort]
    if proxy_workers:
        cmd += ["--workers", str(int(proxy_workers))]
    if record_prompts:
        cmd += ["--record-prompts"]
    if record_requests:
        cmd += ["--record-requests"]
    if harness:
        cmd += ["--harness", harness]
    if not review:
        cmd += ["--no-review"]
    if reasoning_passback:
        cmd += ["--reasoning-passback"]
    if reasoning_reinject:
        cmd += ["--reasoning-reinject"]
    if turn_reminder:
        cmd += ["--turn-reminder", turn_reminder]
    env = {**os.environ, "PYTHONPATH": str(OPERA_ROOT / "src") + os.pathsep + str(OPERA_ROOT) + os.pathsep + os.environ.get("PYTHONPATH", "")}
    proc = subprocess.Popen(cmd, env=env, stdout=open(log_dir / "proxy.log", "ab"), stderr=subprocess.STDOUT)
    atexit.register(lambda: proc.poll() is None and proc.terminate())
    import urllib.request
    for _ in range(60):
        if proc.poll() is not None:
            raise SystemExit(f"critic proxy exited early — see {log_dir / 'proxy.log'}")
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2); break
        except Exception:
            time.sleep(0.5)
    else:
        raise SystemExit("critic proxy did not become healthy in 30 s")
    return proc, f"http://{routable_ip()}:{port}"
