"""Terminal-Bench 2.1 sweep driver: one harbor ``Job`` on xrlenv or native Docker with a pluggable agent.

Tasks are the task dirs the xrlenv kit materializes under ``$XRLENV_BENCHMARK_CACHE/terminal-bench-2-1``; the
task's own ``tests/test.sh`` is the verifier. Selections (prefix-matched): ``green`` (present tasks minus the kit's
EXCLUDE list), ``full`` (every task in the shard), ``smoke`` (``SMOKE_TASKS``).

Budget: each task's ``[agent] timeout_sec`` unless ``agent_timeout_sec`` overrides it (recorded as a deviation).
Grading follows the leaderboard: pass ⇔ ``reward > 0``, and an agent timeout is still graded by its reward.
"""
from __future__ import annotations

import datetime as _dt
import math
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from plugins.benchmarks.common import (INFRA_RETRY_EXCEPTIONS, RESULTS_ROOT, RETRY_POLICY, RunPlan, agent_exception, job_dir_for, reward_value,  # noqa: F401
                                       run_attempts, task_key, trial_passes_default, write_protocol)
from plugins.benchmarks.environments import build_environment_config, environment_label, environment_record, require_environment

BENCHMARK = "tb21"
SHARD = "terminal-bench-2-1"                      # the kit's dataset dir under the cache root
CACHE_ENV = "XRLENV_BENCHMARK_CACHE"              # the kit's cache ROOT variable
ENV_IMPORT_PATH = "xrlenv_plugins.harbor:XrlenvHarborEnvironmentCluster"
TERMINUS2 = "terminus-2"                          # harbor's agent NAME for the official leaderboard agent
EXPECTED_PRESENT = 89                             # pinned task counts: a partial populate must fail loud
EXPECTED_GREEN = 88
DEFAULT_EXCLUDE = frozenset({"caffe-cifar-10"})   # the kit's EXCLUDE; used only when the kit is not importable
SMOKE_TASKS = ("fix-git", "cobol-modernization", "build-cython-ext")   # short tasks for endpoint / plumbing checks
SELECTIONS = ("green", "full", "smoke")
# harbor records these two on the trial and still runs the verifier; every other exception aborts first
AGENT_PHASE_EXCEPTIONS = frozenset({"AgentTimeoutError", "NonZeroAgentExitCodeError"})
LEADERBOARD_ATTEMPTS = 5                          # attempts per task the leaderboard requires


@dataclass
class SweepOptions:
    """One Terminal-Bench 2.1 sweep. ``agent``: ``oracle`` | ``terminus-2`` (harbor's agent by name) | a
    ``module:Class`` import path."""
    agent: str = "oracle"
    model: str | None = None
    agent_kwargs: dict[str, Any] = field(default_factory=dict)
    agent_env: dict[str, str] = field(default_factory=dict)
    selection: str = "green"                # one of SELECTIONS (or a unique prefix)
    tasks: str | None = None                # comma list or id file — overrides ``selection``
    cache_root: str | None = None
    max_workers: int = 4
    results_root: Path = RESULTS_ROOT
    job_id: str | None = None
    retries: int = 6                        # infra retries per trial
    attempts: int = 1                       # pass@k / the leaderboard's -k 5
    mode: str = "resume"                    # resume | resume-timeout | refresh
    override_cpus: int | None = None        # resource overrides — recorded as a deviation
    override_memory_mb: int | None = None
    cpu_pinning: bool | None = None         # automatic: xrlenv pins; native Docker uses CPU quotas
    protocol: dict[str, Any] = field(default_factory=dict)   # launcher-side facts recorded in <job>/protocol.json
    environment: str = "xrlenv"
    agent_timeout_sec: float | None = None   # per-attempt execution budget in seconds; setup/verifier retain their defaults

    def __post_init__(self) -> None:
        if self.agent_timeout_sec is not None and (
            isinstance(self.agent_timeout_sec, bool) or not math.isfinite(self.agent_timeout_sec) or self.agent_timeout_sec <= 0
        ):
            raise ValueError("agent_timeout_sec must be finite and positive")


# ── selection ─────────────────────────────────────────────────────────────────

def resolve_selection(value: str) -> str:
    """Canonical selection name (one of ``SELECTIONS``) from a name or a unique prefix."""
    v = str(value).strip().lower()
    if v in SELECTIONS:
        return v
    hits = [s for s in SELECTIONS if s.startswith(v)] if v else []
    if len(hits) == 1:
        return hits[0]
    raise SystemExit(f"--selection {value!r} is not one of {list(SELECTIONS)}" + (f" (ambiguous prefix: {hits})" if hits else ""))


def kit_dir() -> Path | None:
    """The xrlenv tb2.1 kit directory (a namespace package next to xrlenv_plugins/harbor), if installed."""
    import importlib.util
    try:
        spec = importlib.util.find_spec("xrlenv_plugins.benchmarks.terminal_bench_2_1")
    except (ImportError, ValueError):
        return None
    if spec is None or not spec.submodule_search_locations:
        return None
    return Path(list(spec.submodule_search_locations)[0])


_EXCLUDE_BLOCK = re.compile(r"^EXCLUDE=\((.*?)^\)", re.MULTILINE | re.DOTALL)


def parse_exclude(script_text: str) -> frozenset[str]:
    """The task ids of the ``EXCLUDE=( … )`` bash array in the kit's ``run_full_sweep.sh`` (comments stripped)."""
    m = _EXCLUDE_BLOCK.search(script_text)
    if not m:
        raise ValueError("no EXCLUDE=( … ) block found in run_full_sweep.sh")
    ids: set[str] = set()
    for line in m.group(1).splitlines():
        ids.update(tok for tok in line.split("#", 1)[0].split() if tok)
    return frozenset(ids)


def kit_exclude(script: Path | None = None) -> frozenset[str]:
    """The kit's operational EXCLUDE list (green set = present − EXCLUDE), read from ``run_full_sweep.sh`` so opera
    and the kit cannot drift; the pinned default when the kit is not importable."""
    if script is None:
        d = kit_dir()
        script = d / "run_full_sweep.sh" if d else None
    if script is None or not script.is_file():
        print(f"warning: the xrlenv tb2.1 kit's run_full_sweep.sh was not found; using the pinned exclude list "
              f"{sorted(DEFAULT_EXCLUDE)}", file=sys.stderr)
        return DEFAULT_EXCLUDE
    return parse_exclude(script.read_text(encoding="utf-8"))


def shard_root(cache_root: str | None = None) -> Path:
    """``<cache root>/terminal-bench-2-1`` — the shard the kit's ``build_cache.py`` writes. The root is
    ``cache_root`` (``--cache``) else ``$XRLENV_BENCHMARK_CACHE``."""
    root = cache_root or os.environ.get(CACHE_ENV)
    if not root:
        raise SystemExit(f"{CACHE_ENV} is not set — the tb2.1 shard lives under the xrlenv kit's cache ROOT (opera/.env), "
                         "or pass --cache <root>")
    shard = Path(root).expanduser() / SHARD
    if not shard.is_dir():
        raise SystemExit(f"{shard} not found — populate + patch it with the xrlenv kit first:\n"
                         "  .venv/bin/python <xrlenv>/xrlenv_plugins/benchmarks/terminal_bench_2_1/build_cache.py --stage all")
    return shard


def present_tasks(shard: Path) -> list[str]:
    """A tb2.1 task is a dir with ``solution/solve.sh`` — the kit's discovery rule."""
    return sorted(p.parent.parent.name for p in shard.glob("*/solution/solve.sh"))


def ids_from_arg(arg: str) -> list[str]:
    """A file of ids (one per line, ``#`` comments) or a comma list."""
    if "," not in arg:
        p = Path(arg).expanduser()
        if p.is_file():
            return [ln.strip() for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip() and not ln.startswith("#")]
    return [t.strip() for t in arg.split(",") if t.strip()]


def selection_ids(selection: str, shard: Path, *, exclude: frozenset[str] | None = None) -> list[str]:
    """The task ids of ``selection`` (one of ``SELECTIONS``). ``exclude`` replaces the kit's EXCLUDE list for
    ``green``; ``green`` / ``full`` refuse a shard whose task count is not the pinned one."""
    if selection == "smoke":
        return list(SMOKE_TASKS)
    present = present_tasks(shard)
    if len(present) != EXPECTED_PRESENT:
        raise SystemExit(f"expected {EXPECTED_PRESENT} present tasks under {shard}, found {len(present)} — a partial populate; "
                         f"refusing to define {selection!r} from it (use --tasks for an explicit subset)")
    if selection == "full":
        return present
    ex = kit_exclude() if exclude is None else frozenset(exclude)
    green = [t for t in present if t not in ex]
    if len(green) != EXPECTED_GREEN:
        raise SystemExit(f"expected {EXPECTED_GREEN} green tasks, got {len(green)} (exclude={sorted(ex)}) — shard/EXCLUDE drift; "
                         "check the kit's run_full_sweep.sh and re-pin EXPECTED_GREEN")
    return green


def resolve_tasks(shard: Path, selection: str, tasks_arg: str | None = None, *, exclude: frozenset[str] | None = None) -> list[str]:
    """``tasks_arg`` (comma list / id file) wins, else the selection. Every id must be materialized in the shard."""
    want = ids_from_arg(tasks_arg) if tasks_arg else selection_ids(selection, shard, exclude=exclude)
    if not want:
        raise SystemExit(f"selection {selection!r} / --tasks selected no tasks")
    missing = [t for t in want if not (shard / t / "solution" / "solve.sh").is_file()]
    if missing:
        raise SystemExit(f"{len(missing)} selected task(s) are not materialized under {shard}: {missing[:5]}")
    return want


# ── harbor job ────────────────────────────────────────────────────────────────

def build_agent_config(agent: str, *, model: str | None = None, kwargs: dict[str, Any] | None = None,
                       env: dict[str, str] | None = None, timeout_sec: float | None = None) -> Any:
    """``oracle`` → harbor's OracleAgent; ``module:Class`` → an import path (opera's Terminus 2 subclasses); any other
    name → harbor's own agent by NAME (``terminus-2``). ``timeout_sec`` overrides the task's agent budget (seconds)."""
    from harbor.models.trial.config import AgentConfig
    kwargs, env = dict(kwargs or {}), dict(env or {})
    if agent == "oracle":
        return AgentConfig(override_timeout_sec=timeout_sec)
    if ":" in agent:
        return AgentConfig(import_path=agent, model_name=model, kwargs=kwargs, env=env, override_timeout_sec=timeout_sec)
    return AgentConfig(name=agent, model_name=model, kwargs=kwargs, env=env, override_timeout_sec=timeout_sec)


def build_job_config(opts: SweepOptions, *, task_ids: list[str], shard: Path, job_id: str, jobs_dir: Path) -> Any:
    """One harbor JobConfig; the task's own ``tests/test.sh`` is the verifier (no ``verifier.import_path``)."""
    from harbor.models.job.config import JobConfig, RetryConfig
    from harbor.models.trial.config import TaskConfig
    return JobConfig(
        job_name=job_id, jobs_dir=jobs_dir, n_concurrent_trials=opts.max_workers, n_attempts=max(1, int(opts.attempts)),
        retry=RetryConfig(max_retries=opts.retries, include_exceptions=set(INFRA_RETRY_EXCEPTIONS),
                          min_wait_sec=RETRY_POLICY["min_wait_sec"], wait_multiplier=RETRY_POLICY["wait_multiplier"], max_wait_sec=RETRY_POLICY["max_wait_sec"]),
        environment=build_environment_config("harbor", opts.environment, cpu_pinning=opts.cpu_pinning,
                                             override_cpus=opts.override_cpus, override_memory_mb=opts.override_memory_mb),
        agents=[build_agent_config(opts.agent, model=opts.model, kwargs=opts.agent_kwargs, env=opts.agent_env,
                                   timeout_sec=opts.agent_timeout_sec)],
        tasks=[TaskConfig(path=shard / tid) for tid in task_ids],
    )


def turn_cap(value: Any) -> int | None:
    """A Terminus 2 turn cap from a config / flag value: a positive int, or ``None`` for no cap (``None``, ``-1``
    and ``0`` all mean no cap)."""
    try:
        n = int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    return n if n and n > 0 else None


# ── grading (reward > 0; agent timeouts are graded, like the leaderboard) ──────

def trial_passes(tr: Any) -> tuple[bool, str | None]:
    """``(passed, reason-if-not)``: pass ⇔ ``reward > 0``; agent timeouts / non-zero exits are still graded."""
    return trial_passes_default(tr, graded_exceptions=AGENT_PHASE_EXCEPTIONS)


def mean_reward(results: list[Any]) -> float:
    """Mean reward over ``results`` (a missing reward counts as 0)."""
    return (sum((reward_value(tr) or 0.0) for tr in results) / len(results)) if results else 0.0


def count_timeouts(results: list[Any]) -> int:
    """Number of results that ended in an ``AgentTimeoutError``."""
    return sum(1 for tr in results if agent_exception(tr) == "AgentTimeoutError")


def attempt_accuracies(report: Any) -> list[float]:
    """Per-attempt accuracy: the i-th recorded attempt of every task (each index is one draw per task)."""
    k = max((t.n for t in report.tasks.values()), default=0)
    out: list[float] = []
    for i in range(k):
        vals = [t.outcomes[i]["passed"] for t in report.tasks.values() if i < t.n]
        if vals:
            out.append(sum(vals) / len(vals))
    return out


def leaderboard_line(report: Any) -> str:
    """The leaderboard metric — mean reward over all trials (= mean pass@1 over k attempts) — with a 95% CI over
    the per-attempt accuracies when k >= 2."""
    accs = attempt_accuracies(report)
    mean = report.summary()["mean_pass_at_1"]
    if len(accs) >= 2:
        import statistics
        ci = 1.96 * statistics.stdev(accs) / len(accs) ** 0.5
        return f"leaderboard accuracy (mean reward over all trials) = {mean * 100:.1f}% ± {ci * 100:.1f} (95% CI over {len(accs)} attempts)"
    return f"leaderboard accuracy (mean reward over all trials) = {mean * 100:.1f}%"


# ── reporting on the FULL task set (89), not the green set (88) ────────────────

# The green set's exclude: reporting counts it as k FAILED attempts so every number is over the full task set.
ASSUMED_FAILED = frozenset({"caffe-cifar-10"})


def full_set_summary(report: Any, *, assumed_failed: frozenset[str] = ASSUMED_FAILED) -> dict[str, Any]:
    """``report``'s numbers restated over the full task set: each task in ``assumed_failed`` that the run did not
    execute contributes ``k`` failed attempts (0 passes). Keys mirror ``PassKReport.summary()`` plus ``assumed_failed``."""
    s = dict(report.summary())
    missing = sorted(t for t in assumed_failed if t not in report.tasks)
    if not missing:
        return {**s, "assumed_failed": []}
    k = report.k
    n_tasks = s["n_tasks"] + len(missing)
    solved_trials = sum(t.c for t in report.tasks.values())
    return {**s, "n_tasks": n_tasks, "n_attempts": s["n_attempts"] + k * len(missing),
            "tasks_solved_any": s["tasks_solved_any"], "any_at_k": round(s["tasks_solved_any"] / n_tasks, 4),
            "pass_at_k": {kk: round(v * s["n_tasks"] / n_tasks, 4) for kk, v in s["pass_at_k"].items()},
            "mean_pass_at_1": round(solved_trials / (s["n_attempts"] + k * len(missing)), 4) if n_tasks else 0.0,
            "assumed_failed": missing}


def full_set_line(report: Any, *, assumed_failed: frozenset[str] = ASSUMED_FAILED) -> str:
    """The full-set accuracy line (``""`` when no task had to be assumed failed)."""
    s = full_set_summary(report, assumed_failed=assumed_failed)
    if not s["assumed_failed"]:
        return ""
    accs = [a * report.n_tasks / s["n_tasks"] for a in attempt_accuracies(report)]      # each attempt gains the failed task(s)
    ci = ""
    if len(accs) >= 2:
        import statistics
        ci = f" ± {1.96 * statistics.stdev(accs) / len(accs) ** 0.5 * 100:.1f}"
    return (f"full-set accuracy (over {s['n_tasks']} tasks; {', '.join(s['assumed_failed'])} assumed 0/{report.k}) = "
            f"{s['mean_pass_at_1'] * 100:.1f}%{ci}  |  tasks solved (any of {report.k}): {s['tasks_solved_any']}/{s['n_tasks']}")


# ── protocol manifest ─────────────────────────────────────────────────────────

def protocol_record(opts: SweepOptions, *, task_ids: list[str], selection: str, job_id: str) -> dict[str, Any]:
    """The protocol.json record of a run: the agent reference and kwargs, the environment, the knobs the
    leaderboard validates (resources, attempts) and the explicit deviations from the task protocol."""
    kwargs = dict(opts.agent_kwargs)
    deviations: list[str] = []
    if opts.agent_timeout_sec is not None:
        deviations.append(f"agent_timeout_sec={opts.agent_timeout_sec:g}")
    if opts.override_cpus or opts.override_memory_mb:
        deviations.append("resource overrides")
    if turn_cap(kwargs.get("max_turns")):
        deviations.append(f"max_turns={turn_cap(kwargs['max_turns'])}")
    if kwargs.get("parser_name", "json") != "json":
        deviations.append(f"parser_name={kwargs['parser_name']}")
    if kwargs.get("enable_summarize") is False or kwargs.get("proactive_summarization_threshold") not in (None, 8000):
        deviations.append("summarization settings")
    if opts.agent not in (TERMINUS2, "oracle") and not opts.agent.startswith("plugins.terminus2:"):
        deviations.append(f"agent {opts.agent}")
    rec: dict[str, Any] = {
        "written_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "benchmark": SHARD, "selection": selection, "n_tasks": len(task_ids), "job_id": job_id,
        "agent": {"ref": opts.agent, "model": opts.model, "kwargs": kwargs,
                  "env": {k: ("<redacted>" if "KEY" in k.upper() else v) for k, v in opts.agent_env.items()}},
        "environment": {**environment_record("harbor", opts.environment, cpu_pinning=opts.cpu_pinning), "override_cpus": opts.override_cpus,
                        "override_memory_mb": opts.override_memory_mb},
        "wall_clock": (f"agent.override_timeout_sec={opts.agent_timeout_sec:g}, enforced by harbor"
                       if opts.agent_timeout_sec is not None else
                       "task.toml [agent] timeout_sec, enforced by harbor (no multiplier, no override)"),
        "attempts": opts.attempts, "leaderboard_attempts": opts.attempts >= LEADERBOARD_ATTEMPTS,
        "retries": {"infra": opts.retries},
        "official_protocol": not deviations, "deviations": deviations,
    }
    if opts.agent_timeout_sec is not None:
        rec["agent_timeout_sec"] = opts.agent_timeout_sec
    try:
        from plugins.terminus2 import protocol_record as _terminus_record
        rec["terminus_2"] = _terminus_record()
    except Exception as exc:  # noqa: BLE001 — the manifest must never block a run
        rec["terminus_2"] = {"error": f"{type(exc).__name__}: {exc}"}
    rec.update(opts.protocol)
    return rec


# ── run ───────────────────────────────────────────────────────────────────────

async def run_sweep(opts: SweepOptions) -> int:
    """Run the sweep described by ``opts``; returns the exit code of ``run_attempts``."""
    from harbor.job import Job
    require_environment(opts.environment)
    selection = resolve_selection(opts.selection)
    shard = shard_root(opts.cache_root)
    task_ids = resolve_tasks(shard, selection, opts.tasks)
    label = opts.agent if opts.agent == "oracle" else f"{opts.agent}/{opts.model}"
    job_id = opts.job_id or f"oracle-{BENCHMARK}"
    job_dir = job_dir_for(BENCHMARK, selection, job_id, opts.results_root)
    rec = protocol_record(opts, task_ids=task_ids, selection=selection, job_id=job_id)
    print(f"running {len(task_ids)} {label} task(s) [{selection}] × {opts.attempts} attempt(s) from {shard} on "
          f"{environment_label(opts.environment)} (concurrency={opts.max_workers})\n"
          f"protocol: {'official' if rec['official_protocol'] else 'deviating: ' + ', '.join(rec['deviations'])}"
          + (f"; harbor drift: {rec['terminus_2'].get('drift')}" if rec.get('terminus_2', {}).get('drift') else "")
          + f"\nartifacts: {job_dir}", file=sys.stderr)

    def report_lines(report: Any, results: list[Any]) -> list[str]:
        lines = [leaderboard_line(report) + f"  |  agent timeouts (still graded): {count_timeouts(results)}"]
        if selection == "green" and (full := full_set_line(report)):
            lines.append(full)
        return lines

    config = build_job_config(opts, task_ids=task_ids, shard=shard, job_id=job_id, jobs_dir=job_dir.parent)
    plan = RunPlan(job_cls=Job, config=config, job_dir=job_dir, task_ids=task_ids, k=opts.attempts, mode=opts.mode,
                   label=f"terminal-bench-2-1 {selection} {label}", trial_passes=trial_passes, protocol=rec, report_lines=report_lines)
    return await run_attempts(plan)
