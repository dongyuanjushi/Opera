#!/usr/bin/env python3
"""Launch one benchmark run: an agent (harness x policy preset), optionally behind a critic proxy, to pass@k.

    .venv/bin/python src/launch.py tb21 --harness terminus-2 --agent-model qwen38-27b --critic-model gpt-5.6 --pass-at-k 5
    .venv/bin/python src/launch.py swebench-pro --harness openhands --agent-model qwen38-27b --critic-model gpt-5.6 --critic-strategy swe_prm
    .venv/bin/python src/launch.py --spec my-run.yaml --dry-run

Results go to results/<benchmark>/<selection>/<harness>-<agent>-<critic|nocritic>[-<strategy>[-<tags>]]/; relaunching the same id resumes (--mode).
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import json
import math
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
OPERA_ROOT = HERE.parent
sys.path.insert(0, str(OPERA_ROOT / "src"))
sys.path.insert(0, str(OPERA_ROOT))
from critics.config import resolve_reasoning_effort  # noqa: E402
from opera_env import load_dotenv, require_env  # noqa: E402
from plugins.harnesses import (  # noqa: E402
    HARBOR_IMPORT_PATHS, MINI_INSTANCE_PROMPTS, TURN_REMINDERS, TERMINUS2_AFFINITY_IMPORT_PATH, TERMINUS2_DEFAULT_MAX_OUTPUT_TOKENS, coerce_scalar, context_control_env,
    critic_routed_env, install_signal_handlers, kv_pairs, load_presets, resolve_endpoint, harness_config, terminus2_host_env,
    terminus2_model_info)
from plugins.benchmarks.common import MODES, RESULTS_ROOT, attempt_inventory, build_job_id, job_dir_for, trial_dirs, trial_task  # noqa: E402
from plugins.benchmarks.environments import (ENVIRONMENTS, cpu_pinning_for, environment_record, require_environment,
                                            validate_environment)  # noqa: E402
from plugins.harness_common import resolve_critic_run, start_critic_proxy  # noqa: E402


@dataclass(frozen=True)
class Benchmark:
    """One supported benchmark.

    name: CLI name and results/<name> dir; module: its plugins.benchmarks.<x>.sweep; runtime: harbor | pier;
    harnesses: allowed agent harnesses (first = default when only one); official_turns: turn budget (-1 = none);
    critic_config: default critic policy file; cache_env: env var naming the task cache.
    """
    name: str
    aliases: tuple[str, ...]
    module: str
    runtime: str
    harnesses: tuple[str, ...]
    selections: tuple[str, ...]
    default_selection: str
    official_turns: int
    critic_config: str
    cache_env: str


BENCHMARKS = {
    "tb21": Benchmark("tb21", ("terminal-bench-2.1", "terminal-bench-2-1", "tb2.1", "terminal_bench_2_1"),
                      "plugins.benchmarks.terminal_bench_2_1.sweep", "harbor", ("terminus-2",), ("green", "full", "smoke"), "green",
                      -1, "configs/critic/terminal_bench_2_1.yaml", "XRLENV_BENCHMARK_CACHE"),
    "swebench-pro": Benchmark("swebench-pro", ("swebench_pro", "pro", "swe-bench-pro"),
                              "plugins.benchmarks.swebench_pro.sweep", "harbor", ("openhands", "mini-swe-agent"),
                              ("full", "filtered", "subset-100", "rest", "rest-heldout", "ood", "smoke"), "filtered", -1, "configs/critic/swebench_pro.yaml",
                              "SWEBENCH_PRO_CACHE"),
    "deepswe": Benchmark("deepswe", ("deep-swe", "deep_swe"), "plugins.benchmarks.deepswe.sweep", "pier",
                         ("openhands", "mini-swe-agent"), ("all",), "all", -1, "configs/critic/deepswe.yaml", "XRLENV_BENCHMARK_CACHE"),
    "swe-rebench": Benchmark("swe-rebench", ("swe_rebench", "swerebench", "rebench"), "plugins.benchmarks.swe_rebench.sweep", "harbor",
                             ("openhands", "mini-swe-agent"), ("repos", "green", "smoke", "full"), "repos", -1,
                             "configs/critic/swe_rebench.yaml", "XRLENV_BENCHMARK_CACHE"),
}
CRITIC_PACKS = {"terminus-2": "terminus2", "openhands": "openhands", "mini-swe-agent": "mini_swe_agent"}   # harness -> critic harness pack
CONDENSERS = ("none", "llm-summarizing")


def resolve_benchmark(name: str) -> Benchmark:
    """Look up a benchmark by name or alias (case-insensitive); exit on an unknown name."""
    key = name.strip().lower()
    for b in BENCHMARKS.values():
        if key == b.name or key in b.aliases:
            return b
    raise SystemExit(f"unknown benchmark {name!r}; choose from {list(BENCHMARKS)}")


# spec section -> {spec key: argparse dest}
SPEC_KEYS = {
    "agent": {"harness": "harness", "model": "agent_model", "reasoning_effort": "reasoning_effort",
              "condenser": "condenser", "condenser_max_size": "condenser_max_size", "condenser_keep_first": "condenser_keep_first",
              "max_input_tokens": "max_input_tokens", "context_window": "context_window", "max_output_tokens": "max_output_tokens",
              "temperature": "temperature", "max_turns": "max_turns", "timeout_sec": "agent_timeout_sec", "observation_chars": "observation_chars", "mini_instance_prompt": "mini_instance_prompt", "turn_reminder": "turn_reminder",
              "sampling_max_tokens": "sampling_max_tokens",
              "reasoning_passback": "reasoning_passback", "reasoning_reinject": "reasoning_reinject", "parser": "parser", "session_affinity": "session_affinity",
              "no_recording": "no_recording", "kwargs": "agent_kwarg", "env": "agent_env", "pin": "pin"},
    "critic": {"model": "critic_model", "strategy": "critic_strategy", "reasoning_effort": "critic_reasoning_effort",
               "config": "critic_config", "policy": "critic_policy", "tag": "critic_tag",
               "record_prompts": "critic_record_prompts", "record_requests": "critic_record_requests", "proxy": "critic_proxy",
               "port": "critic_port"},
    "benchmark": {"name": "benchmark", "selection": "selection", "tasks": "tasks", "cache": "cache", "pass_at_k": "pass_at_k",
                  "override_cpus": "override_cpus", "override_memory_mb": "override_memory_mb", "cpu_pinning": "cpu_pinning"},
    "run": {"environment": "environment", "mode": "mode", "max_workers": "max_workers", "retries": "retries", "results_dir": "results_dir", "job_id": "job_id"},
}


def load_spec(path: str | Path) -> dict[str, Any]:
    """Read a run spec YAML (agent: / critic: / benchmark: / run: sections) into argparse defaults."""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    out: dict[str, Any] = {}
    for section, keys in SPEC_KEYS.items():
        block = data.get(section) or {}
        if not isinstance(block, dict):
            raise SystemExit(f"{path}: section {section!r} must be a mapping")
        for key, value in block.items():
            if key == "enabled" and section == "critic":
                if not value:
                    out["critic_model"] = None
                continue
            if key not in keys:
                raise SystemExit(f"{path}: unknown key {section}.{key} (known: {', '.join(keys)})")
            dest = keys[key]
            if dest in ("agent_kwarg", "agent_env") and isinstance(value, dict):
                value = [f"{k}={v}" for k, v in value.items()]
            if dest == "critic_policy" and isinstance(value, dict):
                value = [f"{k}={json.dumps(v)}" for k, v in _flatten_policy(value).items()]
            out[dest] = value
    unknown = set(data) - set(SPEC_KEYS)
    if unknown:
        raise SystemExit(f"{path}: unknown section(s) {sorted(unknown)} (known: {list(SPEC_KEYS)})")
    return out


def _flatten_policy(d: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Flatten a nested mapping to dotted keys ({"a": {"b": 1}} -> {"a.b": 1})."""
    out: dict[str, Any] = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out.update(_flatten_policy(v, f"{prefix}{k}."))
        else:
            out[f"{prefix}{k}"] = v
    return out


def policy_overrides(pairs: list[str]) -> dict[str, Any]:
    """Turn ``key.path=value`` pairs into a nested mapping (values parsed as YAML scalars)."""
    out: dict[str, Any] = {}
    for kv in pairs:
        key, sep, raw = kv.partition("=")
        if not sep or not key.strip():
            raise SystemExit(f"--critic-policy expects key.path=value, got {kv!r}")
        node = out
        parts = key.strip().split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = yaml.safe_load(raw)
    return out


def positive_seconds(value: Any) -> float:
    """argparse type: a finite number of seconds > 0."""
    try:
        seconds = float(value)
    except (ValueError, TypeError):
        raise argparse.ArgumentTypeError("must be a finite positive number of seconds") from None
    if isinstance(value, bool) or not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number of seconds")
    return seconds


def build_parser() -> argparse.ArgumentParser:
    """The CLI, grouped as agent / critic / benchmark / run."""
    p = argparse.ArgumentParser(prog="launch", allow_abbrev=False, description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("benchmark", nargs="?", default=None, help=f"{' | '.join(BENCHMARKS)} (or benchmark.name in --spec)")
    p.add_argument("--spec", default=None, metavar="YAML", help="a run spec with agent: / critic: / benchmark: / run: sections; flags override it")
    p.add_argument("--dry-run", action="store_true", help="print the resolved launch (keys redacted), job id and pass@k progress; start nothing")

    a = p.add_argument_group("agent")
    a.add_argument("--harness", default=None, help="terminus-2 (tb21) | openhands | mini-swe-agent (swebench-pro, deepswe, swe-rebench)")
    a.add_argument("--agent-model", "--policy-model", dest="agent_model", default=None, metavar="PRESET", help="agent preset: a key of configs/policy-models.yaml")
    a.add_argument("--reasoning-effort", default=None, metavar="EFFORT",
                   help="agent reasoning_effort, validated against the preset ('none' = not sent)")
    a.add_argument("--condenser", choices=CONDENSERS, default="none",
                   help="OpenHands only: history condenser (default none; a deviation)")
    a.add_argument("--condenser-max-size", type=int, default=None, metavar="N", help="condense once history exceeds N events (SDK default 240)")
    a.add_argument("--condenser-keep-first", type=int, default=None, metavar="N", help="never condense the first N events (SDK default 2)")
    a.add_argument("--max-input-tokens", type=int, default=None, metavar="N", help="OpenHands: cap on prompt tokens sent by the SDK")
    a.add_argument("--context-window", type=int, default=None, metavar="N", help="Terminus 2: served context window (default: probed from <base_url>/models)")
    a.add_argument("--max-output-tokens", type=int, default=TERMINUS2_DEFAULT_MAX_OUTPUT_TOKENS, metavar="N", help="Terminus 2: output tokens reserved from the context window")
    a.add_argument("--sampling-max-tokens", default=None, metavar="N|none",
                   help="override the preset's per-call output cap sampling.max_tokens (positive int, or 'none' = no cap; a deviation)")
    a.add_argument("--temperature", type=float, default=None, help="sampling temperature (default: the preset's; a deviation)")
    a.add_argument("--max-turns", type=int, default=None, metavar="N",
                   help="agent turn cap (-1 = none; default: the benchmark's, none for all; a cap is a deviation)")
    a.add_argument("--reasoning-passback", dest="reasoning_passback", action="store_const", const=True, default=None,
                   help="mini-swe-agent / terminus-2 on vLLM chat presets (default on): the proxy resends earlier reasoning_content "
                        "as `reasoning` so the model sees its own past thinking")
    a.add_argument("--no-reasoning-passback", dest="reasoning_passback", action="store_const", const=False)
    a.add_argument("--reasoning-reinject", dest="reasoning_reinject", action="store_const", const=True, default=None,
                   help="openhands on vLLM chat presets (default on unless the preset sets reasoning_reinject: false): the proxy "
                        "re-inserts each reply's reasoning into later requests")
    a.add_argument("--no-reasoning-reinject", dest="reasoning_reinject", action="store_const", const=False)
    a.add_argument("--observation-chars", type=int, default=None, metavar="N",
                   help="mini-swe-agent only: cap each tool observation at N characters (stock: 10000; a deviation)")
    a.add_argument("--mini-instance-prompt", choices=MINI_INSTANCE_PROMPTS, default=None,
                   help="mini-swe-agent only: task prompt variant (stock = mini's own; state-notes adds a STATE/NEXT reply header; a deviation)")
    a.add_argument("--turn-reminder", choices=sorted(TURN_REMINDERS), default=None,
                   help="proxy appends this one-line reminder to every newest observation (needs a launcher-started proxy; a deviation)")
    a.add_argument("--agent-timeout-sec", type=positive_seconds, default=None, metavar="SECONDS",
                   help="tb21 / swebench-pro / deepswe: per-task agent wall clock in seconds (default: task.toml; a deviation)")
    a.add_argument("--parser", choices=("json", "xml"), default="json", help="Terminus 2 response format (official: json)")
    a.add_argument("--session-affinity", action="store_true", help="Terminus 2: X-Session-ID = the trial id (session-affine routers)")
    a.add_argument("--no-recording", action="store_true", help="Terminus 2: skip the asciinema recording")
    a.add_argument("--agent-kwarg", action="append", default=[], metavar="K=V", help="extra agent ctor kwarg (repeatable)")
    a.add_argument("--agent-env", action="append", default=[], metavar="K=V", help="extra agent env var (repeatable)")
    a.add_argument("--no-pin", dest="pin", action="store_false", default=True, help="do not pin harness packages to plugins/harnesses.lock.yaml")

    c = p.add_argument_group("critic")
    c.add_argument("--critic-model", default=None, metavar="PRESET",
                   help="critic preset: a key of configs/critic-models.yaml; setting it enables the critic")
    c.add_argument("--critic-strategy", default=None, metavar="NAME",
                   help="operator (default) | passthrough | swe_prm | swe_search | llm_verifier | agentic_rubrics (configs/strategies/)")
    c.add_argument("--critic-reasoning-effort", default=None, metavar="EFFORT", help="critic reasoning_effort, validated against its preset")
    c.add_argument("--critic-config", default=None, metavar="YAML", help="critic policy file (default: configs/critic/<benchmark>.yaml)")
    c.add_argument("--critic-policy", action="append", default=[], metavar="KEY.PATH=VALUE",
                   help="override a key of the critic policy file (repeatable, e.g. schedule.interval=3); needs --critic-tag")
    c.add_argument("--critic-tag", default=None, metavar="TAG", help="label appended to the job id's strategy segment")
    c.add_argument("--critic-record-prompts", action="store_true", help="record the review prompt in every critic event")
    c.add_argument("--critic-record-requests", action="store_true",
                   help="record every proxied agent request and response to critic-logs/<conversation>/requests.jsonl")
    c.add_argument("--critic-proxy", default=None, metavar="URL", help="use an already-running critic proxy (http://host:port)")
    c.add_argument("--critic-port", type=int, default=None, help="port for the auto-started proxy (default: any free port)")

    b = p.add_argument_group("benchmark")
    b.add_argument("--selection", default=None, metavar="NAME", help="task set (tb21: green | full | smoke; swebench-pro: full | filtered | subset-100 | rest | rest-heldout | ood | smoke; deepswe: all; swe-rebench: repos | green | smoke | full)")
    b.add_argument("--tasks", default=None, help="comma-separated task ids or an id file (a subset of the selection)")
    b.add_argument("--cache", default=None, help="benchmark cache root (default: the benchmark's env var)")
    b.add_argument("--pass-at-k", "-k", dest="pass_at_k", type=int, default=1, metavar="K", help="finished attempts wanted per task")
    b.add_argument("--override-cpus", type=int, default=None, metavar="N", help="tb21: CPUs per trial container (default: the task's; a deviation)")
    b.add_argument("--override-memory-mb", type=int, default=None, metavar="MB", help="tb21: memory per trial container (default: the task's; a deviation)")
    b.add_argument("--cpu-pinning", dest="cpu_pinning", action="store_true", default=None, help="xrlenv only: pin each container's cpuset (default: on for harbor benchmarks)")
    b.add_argument("--no-cpu-pinning", dest="cpu_pinning", action="store_false")

    r = p.add_argument_group("run")
    r.add_argument("--environment", choices=ENVIRONMENTS, default="xrlenv", help="container backend (default: xrlenv)")
    r.add_argument("--mode", choices=MODES, default="resume", help="handling of an existing job dir: resume (drop unfinished) | resume-timeout (also rerun timeouts) | refresh (start over)")
    r.add_argument("--max-workers", type=int, default=8)
    r.add_argument("--retries", type=int, default=10, help="per-trial retries on infrastructure failures")
    r.add_argument("--results-dir", default=str(RESULTS_ROOT), help="results root (default: <repo>/results)")
    r.add_argument("--job-id", default=None, help="override the constructed job id")
    return p


def parse(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse argv; a --spec file supplies defaults that explicit flags override."""
    p = build_parser()
    pre, _ = p.parse_known_args(argv)
    if pre.spec:
        p.set_defaults(**load_spec(pre.spec))
    args = p.parse_args(argv)
    validate_environment(args.environment)
    return args


class Tee:
    """Write a stream to both its terminal and the job's launch.log."""

    def __init__(self, stream: Any, log: Any) -> None:
        self._s, self._l = stream, log

    def write(self, s: str) -> int:
        self._s.write(s); self._l.write(s); self._l.flush()
        return len(s)

    def flush(self) -> None:
        self._s.flush(); self._l.flush()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._s, name)


def check_xrlenv_sizes_exec_deadlines() -> None:
    """Exit unless the installed xrlenv harbor environment sizes its exec deadline from the task budget."""
    try:
        from xrlenv_plugins.harbor.environment import XrlenvHarborEnvironmentCluster
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"cannot import xrlenv_plugins.harbor: {exc}") from None
    if not hasattr(XrlenvHarborEnvironmentCluster, "_default_exec_timeout_s"):
        raise SystemExit("this xrlenv's harbor environment applies a flat 1800 s exec deadline (no _default_exec_timeout_s): "
                         "trials longer than 30 min would be transport-killed. Update xrlenv (>= 2026-09-02) before launching.")


def probe_context_window(endpoint: dict[str, str], preset: dict[str, Any]) -> int | None:
    """vLLM's served ``max_model_len`` from ``<base_url>/models``; None when unreadable."""
    from critics.engine import probe_max_model_len
    ep = SimpleNamespace(base_url=endpoint["base_url"], model=preset["model"], api_keys=[endpoint["api_key"]], auth="bearer")
    return probe_max_model_len(ep)


def resolve(a: argparse.Namespace) -> dict[str, Any]:
    """Resolve every launch decision without side effects: benchmark, agent config, critic config, job id/dir and
    protocol record. The returned dict is printed by --dry-run and executed by ``main``."""
    if not a.benchmark:
        raise SystemExit("which benchmark? pass it as the first argument or as benchmark.name in --spec")
    bench = resolve_benchmark(a.benchmark)
    if a.agent_timeout_sec is not None:
        if bench.name not in ("tb21", "swebench-pro", "deepswe"):
            raise SystemExit("--agent-timeout-sec currently supports tb21, swebench-pro and deepswe only")
        try:
            a.agent_timeout_sec = positive_seconds(a.agent_timeout_sec)  # YAML defaults also need validation
        except argparse.ArgumentTypeError as exc:
            raise SystemExit(f"--agent-timeout-sec: {exc}") from None
    sweep = __import__(bench.module, fromlist=["*"])
    selection = sweep.resolve_selection(a.selection) if a.selection else bench.default_selection
    if selection not in bench.selections:
        raise SystemExit(f"{bench.name}: selection {selection!r} is not one of {bench.selections}")
    harness = a.harness or (bench.harnesses[0] if len(bench.harnesses) == 1 else None)
    if not harness:
        raise SystemExit(f"{bench.name} needs --harness ({' | '.join(bench.harnesses)})")
    if harness not in bench.harnesses:
        raise SystemExit(f"{bench.name} runs {' | '.join(bench.harnesses)}, not {harness!r}")
    if not a.agent_model:
        raise SystemExit("--agent-model PRESET is required (a key of configs/policy-models.yaml)")
    presets = load_presets()
    if a.agent_model not in presets:
        raise SystemExit(f"unknown agent preset {a.agent_model!r}; choose from {sorted(presets)}")
    preset = presets[a.agent_model]
    if a.sampling_max_tokens is not None:
        sampling = dict(preset.get("sampling") or {})
        if str(a.sampling_max_tokens).strip().lower() in ("none", "off"):
            sampling.pop("max_tokens", None)
            a.sampling_max_tokens = "none"
        else:
            try:
                sampling["max_tokens"] = int(a.sampling_max_tokens)
            except ValueError:
                raise SystemExit("--sampling-max-tokens takes a positive integer or 'none'") from None
            if sampling["max_tokens"] <= 0:
                raise SystemExit("--sampling-max-tokens takes a positive integer or 'none'")
        preset = {**preset, "sampling": sampling}
    endpoint = resolve_endpoint(preset)
    try:
        effort = resolve_reasoning_effort(a.agent_model, preset, a.reasoning_effort)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    kind = preset.get("kind", "vllm")
    transport = preset.get("api", "chat")                          # chat | responses
    responses_api = transport == "responses"
    if a.condenser != "none" and harness != "openhands":
        raise SystemExit(f"--condenser {a.condenser}: only the OpenHands SDK takes a condenser ({harness}: Terminus 2 summarizes on its own, "
                         "mini-swe-agent has no condenser)")
    turns = bench.official_turns if a.max_turns is None else a.max_turns
    cap = turns if turns and turns > 0 else 0                      # 0 = no turn cap
    extra_kwargs = {k: coerce_scalar(v) for k, v in kv_pairs(a.agent_kwarg).items()}
    ctx_env = context_control_env(condense=a.condenser != "none", condenser_max_size=a.condenser_max_size,
                                  condenser_keep_first=a.condenser_keep_first, max_input_tokens=a.max_input_tokens)
    if ctx_env and harness != "openhands":
        raise SystemExit(f"--condenser-* / --max-input-tokens are OpenHands-SDK options; {harness} ignores them")
    # Default on for vLLM chat presets: passback (mini / Terminus 2) or re-injection (OpenHands) of earlier reasoning via the proxy.
    passback_ok = harness in ("mini-swe-agent", "terminus-2") and kind == "vllm" and not responses_api and not a.critic_proxy
    reinject_ok = harness == "openhands" and kind == "vllm" and not responses_api and not a.critic_proxy
    passback_default = passback_ok and preset.get("reasoning_passback", True) is not False
    reinject_default = reinject_ok and preset.get("reasoning_reinject", True) is not False
    if a.reasoning_passback and harness not in ("mini-swe-agent", "terminus-2"):
        raise SystemExit(f"--reasoning-passback is implemented for mini-swe-agent and terminus-2 ({harness}: the OpenHands SDK sends no "
                         "reasoning back for vLLM models — see --reasoning-reinject)")
    if a.reasoning_passback and (responses_api or kind != "vllm"):
        raise SystemExit("--reasoning-passback fixes a vLLM chat-completions field (reasoning_content -> reasoning); "
                         f"{a.agent_model} is not a vLLM chat model")
    if a.reasoning_passback and a.critic_proxy:
        raise SystemExit("--reasoning-passback needs a proxy this launcher starts; an external --critic-proxy carries its own settings")
    if a.reasoning_reinject and harness != "openhands":
        raise SystemExit(f"--reasoning-reinject is for openhands ({harness}: use --reasoning-passback, the harness itself keeps reasoning_content)")
    if a.reasoning_reinject and (responses_api or kind != "vllm"):
        raise SystemExit(f"--reasoning-reinject re-inserts a vLLM chat-completions field (`reasoning`); {a.agent_model} is not a vLLM chat model")
    if a.reasoning_reinject and a.critic_proxy:
        raise SystemExit("--reasoning-reinject needs a proxy this launcher starts; an external --critic-proxy carries its own settings")
    a.reasoning_passback = passback_default if a.reasoning_passback is None else bool(a.reasoning_passback)
    a.reasoning_reinject = reinject_default if a.reasoning_reinject is None else bool(a.reasoning_reinject)
    if a.turn_reminder and a.critic_proxy:
        raise SystemExit("--turn-reminder needs a proxy this launcher starts; an external --critic-proxy carries its own settings")
    if a.turn_reminder and responses_api:
        raise SystemExit("--turn-reminder rewrites chat-completions messages; "
                         f"{a.agent_model} runs on the Responses API")
    if a.observation_chars is not None and harness != "mini-swe-agent":
        raise SystemExit(f"--observation-chars is a mini-swe-agent option; {harness} has its own observation handling")
    if a.mini_instance_prompt is not None and harness != "mini-swe-agent":
        raise SystemExit(f"--mini-instance-prompt is a mini-swe-agent option; {harness} builds its own task prompt")
    window, probe_note, model_info, terminus = None, None, None, None
    if harness == "terminus-2":
        window = a.context_window or preset.get("context_window")
        if window is None and kind == "vllm":
            window = probe_context_window(endpoint, preset)
            if window is None:
                probe_note = f"could not read max_model_len from {endpoint['base_url']}/models — pass --context-window N"
                if not a.dry_run:
                    raise SystemExit(probe_note)
        model_info = terminus2_model_info(preset, context_window=window, max_output_tokens=a.max_output_tokens)
        if model_info is None and kind == "vllm":
            model_info = {"max_input_tokens": None, "max_output_tokens": a.max_output_tokens, "input_cost_per_token": 0.0, "output_cost_per_token": 0.0}
        terminus = {"model_info": model_info, "temperature": a.temperature, "parser_name": a.parser if a.parser != "json" else None,
                    "use_responses_api": True if responses_api else None, "record_terminal_session": False if a.no_recording else None}
    temp_env = {"LLM_TEMPERATURE": str(a.temperature)} if (a.temperature is not None and harness == "openhands") else {}
    agent, model_name, agent_kwargs, agent_env = harness_config(
        harness, preset, endpoint, step_limit=cap, extra_kwargs=extra_kwargs, extra_env={**kv_pairs(a.agent_env), **ctx_env},
        pin=a.pin, reasoning_effort=effort, preset_name=a.agent_model, runtime=bench.runtime, terminus=terminus,
        observation_chars=a.observation_chars, instance_prompt=a.mini_instance_prompt)
    agent_env = {**agent_env, **temp_env}
    if a.temperature is not None and harness == "mini-swe-agent":          # mini: temperature lives in its model config
        cfg_map = agent_kwargs.get("config")
        if isinstance(cfg_map, dict):
            cfg_map.setdefault("model", {}).setdefault("model_kwargs", {})["temperature"] = a.temperature
        elif "config_yaml" in agent_kwargs:
            cfg_map = yaml.safe_load(agent_kwargs["config_yaml"]) or {}
            cfg_map.setdefault("model", {}).setdefault("model_kwargs", {})["temperature"] = a.temperature
            agent_kwargs["config_yaml"] = yaml.safe_dump(cfg_map)
    critic_on = bool(a.critic_model or a.critic_proxy)
    critic_flags = (a.critic_strategy, a.critic_reasoning_effort, a.critic_policy, a.critic_tag,
                    a.critic_record_prompts, a.critic_config)
    # --critic-record-requests is allowed without a critic (review-off recording proxy).
    if any(critic_flags) and not a.critic_model:
        raise SystemExit("the critic flags need --critic-model PRESET (an already-running --critic-proxy carries its own settings)")
    if a.critic_policy and not a.critic_tag:
        raise SystemExit("--critic-policy needs --critic-tag TAG so the job id names the policy variant")
    critic_cfg, critic_tag, tags = None, None, []
    critic_config_path = Path(a.critic_config or bench.critic_config)
    if not critic_config_path.is_absolute():
        critic_config_path = OPERA_ROOT / critic_config_path
    policy = policy_overrides(a.critic_policy)
    if a.critic_model:
        try:
            critic_cfg, _ = resolve_critic_run(critic_config_path, critic_model=a.critic_model, strategy=a.critic_strategy,
                                               reasoning_effort=a.critic_reasoning_effort, harness=CRITIC_PACKS[harness],
                                               overrides=policy)
        except ValueError as exc:
            raise SystemExit(f"critic config {critic_config_path}: {exc}") from None
        critic_tag = critic_cfg.endpoint_preset or critic_cfg.model_tag.split("-")[0]
        if a.critic_tag:
            tags.append(a.critic_tag)
    elif a.critic_proxy:
        critic_tag = "proxy"
    # deviations from the benchmark's protocol: recorded in protocol.json, not in the job id
    deviations: list[str] = []
    if a.agent_timeout_sec is not None:
        deviations.append(f"agent_timeout_sec={a.agent_timeout_sec:g}")
    if turns != bench.official_turns:
        deviations.append(f"turns{cap}" if cap else "nocap")
    if a.condenser != "none":
        deviations.append("condense")
    if harness == "openhands" and preset.get("openhands_retry_malformed_calls"):
        deviations.append(f"retry-malformed-calls{int(preset['openhands_retry_malformed_calls'])}")
    if harness == "openhands" and preset.get("openhands_native_tool_calling") is False:
        deviations.append("prompt-tools")
        if preset.get("openhands_tool_text_repair"):
            deviations.append(f"tool-text-repair-{preset['openhands_tool_text_repair']}")
        if critic_on:
            raise SystemExit(f"{a.agent_model}: the preset runs OpenHands with prompt-described tools (openhands_native_tool_calling: false); "
                             "the critic proxy reads native tool calls to see actions and hold a finish, so a critic run is "
                             "not supported on this path yet")
    if a.temperature is not None:
        deviations.append(f"t{a.temperature:g}")
    if a.observation_chars is not None:
        deviations.append(f"obs{a.observation_chars}")
    if a.mini_instance_prompt not in (None, "stock"):
        deviations.append(f"prompt-{a.mini_instance_prompt}")
    if a.turn_reminder:
        deviations.append(f"turn-reminder-{a.turn_reminder}")
    if a.reasoning_passback != passback_default:
        deviations.append("reasoning-passback" if a.reasoning_passback else "no-reasoning-passback")
    if a.reasoning_reinject != reinject_default:
        deviations.append("reasoning-reinject" if a.reasoning_reinject else "no-reasoning-reinject")
    if a.sampling_max_tokens is not None:
        deviations.append(f"max-tokens-{a.sampling_max_tokens}")
    if a.parser == "xml":
        deviations.append("xml")
    if a.override_cpus:
        deviations.append(f"cpu{a.override_cpus}")
    if a.override_memory_mb:
        deviations.append(f"mem{a.override_memory_mb}m")
    agent_tag = a.agent_model
    strategy = critic_cfg.strategy if critic_cfg else ("external" if a.critic_proxy else None)
    job_id = a.job_id or build_job_id(harness=harness, agent=agent_tag, critic=critic_tag, strategy=strategy, tags=tags)
    job_dir = job_dir_for(bench.name, selection, job_id, Path(a.results_dir))
    if critic_on or a.reasoning_passback or a.reasoning_reinject or a.turn_reminder or a.critic_record_requests:
        # the agent goes through a proxy: strip endpoint vars from its env, or they would override the proxy URL
        if harness == "terminus-2":
            agent = HARBOR_IMPORT_PATHS[harness]
        elif bench.runtime == "harbor":
            agent_kwargs["step_limit" if harness == "mini-swe-agent" else "max_iterations"] = cap or -1
            agent_env = critic_routed_env(harness, agent_env)
        else:
            agent_kwargs["step_limit" if harness == "mini-swe-agent" else "max_iterations"] = cap or -1
        if a.critic_proxy:
            agent_kwargs["critic_proxy_url"] = a.critic_proxy.rstrip("/")
    elif harness == "terminus-2" and (a.session_affinity or responses_api):
        agent = TERMINUS2_AFFINITY_IMPORT_PATH
    if a.reasoning_passback and harness == "terminus-2":
        # Terminus 2 keeps reasoning_content in its history only with interleaved_thinking
        agent_kwargs["interleaved_thinking"] = True
    host_env = (terminus2_host_env(model_name, endpoint, critic=critic_on or a.reasoning_passback)
                if harness == "terminus-2" else {})
    cpu_pinning = cpu_pinning_for(a.environment, a.cpu_pinning, default=bench.runtime == "harbor")
    protocol = {
        "launcher": "src/launch.py", "launched_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "agent": {"harness": harness, "model": a.agent_model, "endpoint_base_url_var": endpoint["base_url_var"], "reasoning_effort": effort,
                  "transport": transport, "condenser": a.condenser, "context_window": window, "temperature": a.temperature,
                  "max_turns": cap if cap else -1, "observation_chars": a.observation_chars, "instance_prompt": a.mini_instance_prompt, "turn_reminder": a.turn_reminder, "reasoning_passback": bool(a.reasoning_passback), "reasoning_reinject": bool(a.reasoning_reinject),
                  "sampling": preset.get("sampling") or None,
                  "session_affinity": bool(a.session_affinity or critic_on)},
        "critic": ({"model": a.critic_model, "strategy": strategy, "config": str(critic_config_path), "policy_overrides": policy or None,
                    "proxy": a.critic_proxy,
                    "resolved": ({"strategy": critic_cfg.strategy, "model": critic_cfg.endpoint.model, "preset": critic_cfg.endpoint_preset,
                                  "contract": critic_cfg.contract, "reasoning_effort": critic_cfg.endpoint.reasoning_effort,
                                  "policy": {"upstream_responses_stream": critic_cfg.proxy.upstream_responses_stream,
                                             "upstream_responses_timeout_s": critic_cfg.proxy.upstream_responses_timeout_s,
                                             "schedule": critic_cfg.schedule.model_dump(),
                                             "intervention_audit": critic_cfg.applicability.intervention_audit}}
                                 if critic_cfg else None)}
                   if critic_on else None),
        "benchmark": {"name": bench.name, "selection": selection, "tasks": a.tasks, "pass_at_k": a.pass_at_k,
                      "wall_clock": (f"agent.override_timeout_sec={a.agent_timeout_sec:g}, enforced by harbor"
                                     if a.agent_timeout_sec is not None else
                                     "task.toml [agent] timeout_sec (harbor / pier), no multiplier or override"),
                      "override_cpus": a.override_cpus, "override_memory_mb": a.override_memory_mb, "cpu_pinning": cpu_pinning},
        "environment": environment_record(bench.runtime, a.environment, cpu_pinning=cpu_pinning),
        "run": {"environment": a.environment, "mode": a.mode, "max_workers": a.max_workers, "retries": a.retries},
        "job_id": job_id, "official_protocol": not deviations, "deviations": deviations,
    }
    if a.agent_timeout_sec is not None:
        protocol["agent"]["timeout_sec"] = a.agent_timeout_sec
        protocol["agent_timeout_sec"] = a.agent_timeout_sec
    return {"bench": bench, "sweep": sweep, "selection": selection, "harness": harness, "preset": preset, "endpoint": endpoint,
            "effort": effort, "agent": agent, "model_name": model_name, "agent_kwargs": agent_kwargs, "agent_env": agent_env,
            "host_env": host_env, "cap": cap, "critic_on": critic_on, "critic_cfg": critic_cfg, "critic_config_path": critic_config_path,
            "policy": policy, "job_id": job_id, "job_dir": job_dir, "protocol": protocol, "probe_note": probe_note, "cpu_pinning": cpu_pinning}


def write_critic_config(job_dir: Path, base: Path, policy: dict[str, Any]) -> Path:
    """Write <job>/critic.yaml: extends *base* and applies the --critic-policy overrides last."""
    job_dir.mkdir(parents=True, exist_ok=True)
    out = job_dir / "critic.yaml"
    out.write_text("# written by src/launch.py — the benchmark's critic policy plus this run's --critic-policy overrides "
                   "(launch_overrides: applied last, above the file's policy_by_critic rules)\n"
                   + yaml.safe_dump({"extends": str(base.resolve()), "launch_overrides": policy}, sort_keys=False), encoding="utf-8")
    return out


def dry_run_view(r: dict[str, Any], a: argparse.Namespace) -> dict[str, Any]:
    """The --dry-run JSON: resolved launch with keys redacted, plus the job's pass@k progress."""
    red_env = {k: ("<redacted>" if "KEY" in k.upper() else v) for k, v in r["agent_env"].items()}
    inv = None
    if r["job_dir"].is_dir():
        try:
            tasks = sorted({trial_task(d) for d in trial_dirs(r["job_dir"])})       # tasks with attempts so far
            inv = attempt_inventory(r["job_dir"], tasks, a.pass_at_k, mode=a.mode, trial_passes=r["sweep"].trial_passes).render()
        except Exception as exc:  # noqa: BLE001
            inv = f"<inventory failed: {exc}>"
    return {"job_id": r["job_id"], "job_dir": str(r["job_dir"]), "mode": a.mode, "progress": inv,
            "agent": {"ref": r["agent"], "model_name": r["model_name"], "kwargs": r["agent_kwargs"], "env": red_env,
                      "host_env": {k: "<redacted>" for k in r["host_env"]},
                      "endpoint_from": {"base_url": r["endpoint"]["base_url_var"], "api_key": r["endpoint"]["api_key_var"]},
                      "probe_note": r["probe_note"]},
            "critic": ({"config": str(r["critic_config_path"]), "policy_overrides": r["policy"] or None,
                        "resolved": ({"strategy": r["critic_cfg"].strategy, "model": r["critic_cfg"].endpoint.model,
                                      "preset": r["critic_cfg"].endpoint_preset, "base_url": r["critic_cfg"].endpoint.base_url,
                                      "contract": r["critic_cfg"].contract, "reasoning_effort": r["critic_cfg"].endpoint.reasoning_effort,
                                      "policy": r["protocol"]["critic"]["resolved"]["policy"]}
                                     if r["critic_cfg"] else None),
                        "proxy": a.critic_proxy or "<auto-start: upstream " + r["endpoint"]["base_url"] + ">"} if r["critic_on"] else None),
            "passthrough_proxy": ("<auto-start, review off, " + ("reasoning passback" if a.reasoning_passback else "reasoning re-injection")
                                  + ": upstream " + r["endpoint"]["base_url"] + ">"
                                  if (a.reasoning_passback or a.reasoning_reinject) and not r["critic_on"] else None),
            "protocol": r["protocol"]}


def main(argv: list[str] | None = None) -> int:
    """Resolve, preflight, start the critic or passthrough proxy if needed, and run the sweep. Returns the exit code."""
    a = parse(argv)
    os.environ.setdefault("XRLENV_DOTENV", "off")
    load_dotenv()
    r = resolve(a)
    if a.dry_run:
        print(json.dumps(dry_run_view(r, a), indent=1, default=str))
        return 0
    bench, sweep, job_dir = r["bench"], r["sweep"], r["job_dir"]
    needs_cache_env = bench.cache_env == "XRLENV_BENCHMARK_CACHE" and not a.cache
    require_environment(a.environment, require_token=True)
    if needs_cache_env:
        require_env("XRLENV_BENCHMARK_CACHE")
    if a.environment == "xrlenv" and bench.runtime == "harbor":
        check_xrlenv_sizes_exec_deadlines()
    job_dir.mkdir(parents=True, exist_ok=True)
    log = open(job_dir / "launch.log", "a", encoding="utf-8")
    log.write(f"\n==== {_dt.datetime.now().isoformat(timespec='seconds')}  {' '.join(sys.argv)}\n")
    sys.stdout, sys.stderr = Tee(sys.stdout, log), Tee(sys.stderr, log)
    install_signal_handlers()
    os.environ.update(r["host_env"])          # Terminus 2: LiteLLM reads the provider key from this process's env
    proc = None
    agent_kwargs = dict(r["agent_kwargs"])
    endpoint = r["endpoint"]
    key_env = endpoint["api_key_var"] if endpoint["api_key_var"] not in ("", "<default>") else None
    if a.critic_model:
        config = write_critic_config(job_dir, r["critic_config_path"], r["policy"]) if r["policy"] else r["critic_config_path"]
        proc, proxy_url = start_critic_proxy(config, upstream=endpoint["base_url"], upstream_key_env=key_env,
                                             log_dir=job_dir / "critic-logs", port=a.critic_port, critic_model=a.critic_model,
                                             strategy=a.critic_strategy, proxy_workers=2 * a.max_workers + 16,
                                             critic_reasoning_effort=a.critic_reasoning_effort, record_prompts=a.critic_record_prompts,
                                             record_requests=a.critic_record_requests,
                                             harness=CRITIC_PACKS[r["harness"]], reasoning_passback=a.reasoning_passback,
                                             reasoning_reinject=a.reasoning_reinject, turn_reminder=TURN_REMINDERS.get(a.turn_reminder))
        agent_kwargs["critic_proxy_url"] = proxy_url
        cfg = r["critic_cfg"]
        print(f"critic proxy {proxy_url} -> {endpoint['base_url']} (config {config}; strategy {cfg.strategy}; critic model {cfg.model_tag}; "
              f"logs {job_dir / 'critic-logs'}{'; reasoning passback on' if a.reasoning_passback else ''}"
              f"{'; reasoning re-injection on' if a.reasoning_reinject else ''})", file=sys.stderr)
    elif a.reasoning_passback or a.reasoning_reinject or a.turn_reminder or a.critic_record_requests:
        # no critic: a review-off passthrough proxy (reasoning passback / re-injection, turn reminder, request recording)
        proc, proxy_url = start_critic_proxy(r["critic_config_path"], upstream=endpoint["base_url"], upstream_key_env=key_env,
                                             log_dir=job_dir / "passback-proxy", port=a.critic_port, proxy_workers=2 * a.max_workers + 16,
                                             harness=CRITIC_PACKS[r["harness"]], review=False, reasoning_passback=a.reasoning_passback,
                                             reasoning_reinject=a.reasoning_reinject,
                                             record_requests=a.critic_record_requests,
                                             turn_reminder=TURN_REMINDERS.get(a.turn_reminder))
        agent_kwargs["critic_proxy_url"] = proxy_url
        print(f"passthrough proxy {proxy_url} -> {endpoint['base_url']} (review off"
              f"{'; reasoning passback on' if a.reasoning_passback else ''}{'; reasoning re-injection on' if a.reasoning_reinject else ''}"
              f"{'; turn reminder ' + a.turn_reminder if a.turn_reminder else ''}; logs {job_dir / 'passback-proxy'})",
              file=sys.stderr)
    if a.environment == "xrlenv":
        try:
            from xrlenv.observability.logging import configure_logging
            configure_logging()
        except Exception:
            pass
    common = dict(agent=r["agent"], model=r["model_name"], agent_kwargs=agent_kwargs, agent_env=r["agent_env"], tasks=a.tasks,
                  cache_root=a.cache, max_workers=a.max_workers, results_root=Path(a.results_dir), job_id=r["job_id"], retries=a.retries,
                  attempts=a.pass_at_k, mode=a.mode, environment=a.environment, cpu_pinning=r["cpu_pinning"], protocol=r["protocol"])
    if bench.name == "tb21":
        opts = sweep.SweepOptions(selection=r["selection"], override_cpus=a.override_cpus, override_memory_mb=a.override_memory_mb,
                                  agent_timeout_sec=a.agent_timeout_sec, **common)
    elif bench.name == "swebench-pro":
        opts = sweep.SweepOptions(selection=r["selection"], agent_timeout_sec=a.agent_timeout_sec, **common)
    elif bench.name == "swe-rebench":
        opts = sweep.SweepOptions(selection=r["selection"], **common)
    elif bench.name == "deepswe":
        opts = sweep.SweepOptions(agent_timeout_sec=a.agent_timeout_sec, **common)
    else:
        opts = sweep.SweepOptions(**common)
    real_out, real_err = sys.stdout, sys.stderr
    try:
        return asyncio.run(sweep.run_sweep(opts))
    except KeyboardInterrupt as exc:                     # Ctrl-C or SIGTERM (install_signal_handlers)
        print(f"\ninterrupted ({exc}): in-flight trials cancelled and their containers released; "
              f"partial attempts stay under {job_dir} (a relaunch removes and re-plans them)", file=sys.stderr)
        return 130
    except BaseException:                                # log the traceback to launch.log before closing it
        import traceback
        traceback.print_exc(file=sys.stderr)
        raise
    finally:
        if proc is not None and proc.poll() is None:     # stop the proxy
            proc.terminate()
        sys.stdout, sys.stderr = real_out._s, real_err._s
        log.close()


if __name__ == "__main__":
    raise SystemExit(main())
