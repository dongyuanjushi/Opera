"""DeepSWE sweep driver: one pier ``Job`` on xrlenv or native Docker with a pluggable agent.

Tasks are the ``task.toml`` dirs under ``$XRLENV_BENCHMARK_CACHE/deep-swe``; pass ⇔ the verifier's ``reward > 0``.
The budget is each task's own ``[agent] timeout_sec`` unless ``agent_timeout_sec`` overrides it.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from plugins.benchmarks.common import (INFRA_RETRY_EXCEPTIONS, RESULTS_ROOT, RETRY_POLICY, RunPlan, job_dir_for, reward_value, run_attempts,  # noqa: F401
                                       task_key, trial_passes_default)
from plugins.benchmarks.environments import build_environment_config, environment_label, environment_record, require_environment

BENCHMARK = "deepswe"
SHARD = "deep-swe"
OPENHANDS_IMPORT_PATH = "plugins.openhands.pier_agent:OpenHandsPier"
MINI_IMPORT_PATH = "plugins.mini_sweagent.pier_agent:MiniSweAgentPier"
SELECTIONS = ("all",)                       # the corpus has one set


@dataclass
class SweepOptions:
    """One DeepSWE sweep. ``agent``: ``oracle`` | ``openhands`` | ``mini-swe-agent`` | a ``module:Class`` import path."""
    agent: str = "oracle"
    model: str | None = None
    agent_kwargs: dict[str, Any] = field(default_factory=dict)
    agent_env: dict[str, str] = field(default_factory=dict)
    tasks: str | None = None               # comma list or id file; default: every task in the shard
    cache_root: str | None = None
    max_workers: int = 4
    results_root: Path = RESULTS_ROOT
    job_id: str | None = None
    retries: int = 6                       # infra retries per trial
    attempts: int = 1                      # pass@k: k finished attempts per task in the job directory
    mode: str = "resume"                   # resume | resume-timeout | refresh
    cpu_pinning: bool = False
    protocol: dict[str, Any] = field(default_factory=dict)   # launcher-side facts recorded in <job>/protocol.json
    environment: str = "xrlenv"
    agent_timeout_sec: float | None = None   # per-attempt execution budget override in seconds (task.toml default otherwise)


# ── tasks ─────────────────────────────────────────────────────────────────────

def shard_root(cache_root: str | None = None) -> Path:
    """``<cache root>/deep-swe``; the root is ``cache_root`` else ``$XRLENV_BENCHMARK_CACHE``."""
    root = cache_root or os.environ.get("XRLENV_BENCHMARK_CACHE")
    if not root:
        raise SystemExit("XRLENV_BENCHMARK_CACHE is not set (opera/.env)")
    shard = Path(root).expanduser() / SHARD
    if not shard.is_dir():
        raise SystemExit(f"{shard} not found — deep-swe shard missing from the cache")
    return shard


def resolve_tasks(shard: Path, tasks_arg: str | None) -> list[str]:
    """The task ids to run: ``tasks_arg`` (a comma list or an id file) when given, else every task in the shard."""
    if tasks_arg is not None:
        # a long comma list makes Path(...).is_file() raise OSError instead of returning False
        try:
            is_file = Path(tasks_arg).is_file()
        except OSError:
            is_file = False
        want = [ln.strip() for ln in Path(tasks_arg).read_text().splitlines() if ln.strip() and not ln.startswith("#")] if is_file \
            else [t.strip() for t in tasks_arg.split(",") if t.strip()]
        if not want:
            raise SystemExit(f"--tasks {tasks_arg!r} selected no tasks")
        missing = [t for t in want if not (shard / t / "task.toml").is_file()]
        if missing:
            raise SystemExit(f"unknown task(s) under {shard}: {missing[:5]}")
        return want
    found = sorted(p.parent.name for p in shard.glob("*/task.toml"))
    if not found:
        raise SystemExit(f"no tasks with task.toml under {shard}")
    return found


# ── pier job ──────────────────────────────────────────────────────────────────

def build_agent_config(agent: str, *, model: str | None = None, kwargs: dict[str, Any] | None = None,
                       env: dict[str, str] | None = None, timeout_sec: float | None = None) -> Any:
    """pier's AgentConfig for ``agent``: ``oracle`` | ``openhands`` | ``mini-swe-agent`` | a ``module:Class`` import
    path | any other pier agent name. ``timeout_sec`` overrides the task's agent budget (seconds)."""
    from pier.models.trial.config import AgentConfig
    kwargs, env = dict(kwargs or {}), dict(env or {})
    if agent == "oracle":
        return AgentConfig(override_timeout_sec=timeout_sec)
    if agent == "openhands":
        return AgentConfig(import_path=OPENHANDS_IMPORT_PATH, model_name=model, kwargs=kwargs, env=env, override_timeout_sec=timeout_sec)
    if agent == "mini-swe-agent":
        return AgentConfig(import_path=MINI_IMPORT_PATH, model_name=model, kwargs=kwargs, env=env, override_timeout_sec=timeout_sec)
    if ":" in agent:
        return AgentConfig(import_path=agent, model_name=model, kwargs=kwargs, env=env, override_timeout_sec=timeout_sec)
    return AgentConfig(name=agent, model_name=model, kwargs=kwargs, env=env, override_timeout_sec=timeout_sec)


def build_job_config(opts: SweepOptions, *, task_ids: list[str], shard: Path, job_id: str, jobs_dir: Path) -> Any:
    """One pier JobConfig. No timeout fields: the task's own ``[agent] timeout_sec`` is the budget."""
    from pier.models.job.config import JobConfig, RetryConfig
    from pier.models.trial.config import TaskConfig
    return JobConfig(
        job_name=job_id, jobs_dir=jobs_dir, n_concurrent_trials=opts.max_workers, n_attempts=max(1, int(opts.attempts)),
        retry=RetryConfig(max_retries=opts.retries, include_exceptions=set(INFRA_RETRY_EXCEPTIONS),
                          min_wait_sec=RETRY_POLICY["min_wait_sec"], wait_multiplier=RETRY_POLICY["wait_multiplier"], max_wait_sec=RETRY_POLICY["max_wait_sec"]),
        environment=build_environment_config("pier", opts.environment, cpu_pinning=opts.cpu_pinning),
        agents=[build_agent_config(opts.agent, model=opts.model, kwargs=opts.agent_kwargs, env=opts.agent_env,
                                   timeout_sec=opts.agent_timeout_sec)],
        tasks=[TaskConfig(path=shard / tid) for tid in task_ids],
    )


# ── grading (reward.json["reward"] > 0) ───────────────────────────────────────

def trial_passes(tr: Any) -> tuple[bool, str | None]:
    """``(passed, reason-if-not)``: pass ⇔ the verifier recorded ``reward > 0``."""
    return trial_passes_default(tr)


# ── run ───────────────────────────────────────────────────────────────────────

async def run_sweep(opts: SweepOptions) -> int:
    """Run the sweep described by ``opts``; returns the exit code of ``run_attempts``."""
    from pier.job import Job
    require_environment(opts.environment)
    shard = shard_root(opts.cache_root)
    task_ids = resolve_tasks(shard, opts.tasks)
    label = opts.agent if opts.agent == "oracle" else f"{opts.agent}/{opts.model}"
    job_id = opts.job_id or f"oracle-{BENCHMARK}"
    job_dir = job_dir_for(BENCHMARK, SELECTIONS[0], job_id, opts.results_root)
    print(f"running {len(task_ids)} {label} task(s) × {opts.attempts} attempt(s) from {shard} on "
          f"{environment_label(opts.environment)} (concurrency={opts.max_workers})\n"
          f"artifacts: {job_dir}", file=sys.stderr)
    config = build_job_config(opts, task_ids=task_ids, shard=shard, job_id=job_id, jobs_dir=job_dir.parent)
    plan = RunPlan(job_cls=Job, config=config, job_dir=job_dir, task_ids=task_ids, k=opts.attempts, mode=opts.mode,
                   label=f"{BENCHMARK} {label}", trial_passes=trial_passes,
                   protocol={**opts.protocol, "environment": environment_record("pier", opts.environment, cpu_pinning=opts.cpu_pinning)})
    return await run_attempts(plan)
