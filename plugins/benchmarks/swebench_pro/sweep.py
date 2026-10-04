"""SWE-bench Pro sweep driver: one harbor ``Job`` on xrlenv or native Docker with a pluggable agent.

Tasks are the harbor task dirs the xrlenv kit materializes under ``<cache>/swebench-pro``; the task's own
``tests/test.sh`` is the verifier and pass ⇔ ``reward > 0``. Budget: each task's ``[agent] timeout_sec`` unless
``agent_timeout_sec`` overrides it.

Selections (prefix-matched, read from the kit's manifests):

* ``full`` — every instance materialized in the shard
* ``filtered`` — the kit's ``filtered_instance_ids.txt``
* ``subset-100`` — the kit's ``subset_100_instance_ids.txt`` (the eval set)
* ``rest`` — ``filtered`` minus ``subset-100`` (the training pool)
* ``rest-heldout`` — ``rest`` minus every task of the ``HELDOUT_REPOS``
* ``ood`` — ``rest`` restricted to the ``HELDOUT_REPOS`` (the repo-level out-of-distribution check)
* ``smoke`` — the first 8 dataset rows (needs ``$SWEBENCH_PRO_PARQUET``)
"""
from __future__ import annotations

import math
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from plugins.benchmarks.common import (INFRA_RETRY_EXCEPTIONS, RESULTS_ROOT, RETRY_POLICY, RunPlan, job_dir_for, reward_value, run_attempts,  # noqa: F401
                                       task_key, trial_passes_default)

OPERA_ROOT = Path(__file__).resolve().parents[3]
from plugins.benchmarks.environments import build_environment_config, environment_label, environment_record, require_environment

BENCHMARK = "swebench-pro"
SHARD = "swebench-pro"
GOLDEN_SUBDIR = "golden_patches"   # second supported shard layout; mirrors the kit's build_cache.shard_dir()
SELECTIONS = ("full", "filtered", "subset-100", "rest", "rest-heldout", "ood", "smoke")
_SELECTION_ALIASES = {"subset100": "subset-100", "subset_100": "subset-100", "100": "subset-100", "restheldout": "rest-heldout", "ood4": "ood", "ood-4repo": "ood", "ood3": "ood"}
# the repositories withheld from ``rest-heldout``: their subset-100 tasks form the out-of-repo check
HELDOUT_REPOS = ("navidrome__navidrome", "element", "tutao__tutanota", "internetarchive__openlibrary")


def _has_task_dirs(d: Path) -> bool:
    return d.is_dir() and next(d.glob("*/task.toml"), None) is not None


@dataclass
class SweepOptions:
    """One SWE-bench Pro sweep. ``agent``: ``oracle`` | a ``module:Class`` import path | a harbor agent name."""
    agent: str = "oracle"
    model: str | None = None
    agent_kwargs: dict[str, Any] = field(default_factory=dict)
    agent_env: dict[str, str] = field(default_factory=dict)
    selection: str = "filtered"             # one of SELECTIONS (or a unique prefix / alias)
    tasks: str | None = None                # comma list or id file — overrides ``selection``
    cache_root: str | None = None
    max_workers: int = 4
    results_root: Path = RESULTS_ROOT
    job_id: str | None = None
    retries: int = 6                        # infra retries per trial
    attempts: int = 1                       # pass@k: k finished attempts per task in the job directory
    mode: str = "resume"                    # resume | resume-timeout | refresh
    cpu_pinning: bool | None = None         # automatic: xrlenv pins; native Docker uses CPU quotas
    protocol: dict[str, Any] = field(default_factory=dict)
    environment: str = "xrlenv"
    agent_timeout_sec: float | None = None   # per-attempt execution budget override in seconds; setup/verifier keep their defaults

    def __post_init__(self) -> None:
        if self.agent_timeout_sec is not None and (
            isinstance(self.agent_timeout_sec, bool) or not math.isfinite(self.agent_timeout_sec) or self.agent_timeout_sec <= 0
        ):
            raise ValueError("agent_timeout_sec must be finite and positive")


# ── selection ─────────────────────────────────────────────────────────────────

def resolve_selection(value: str) -> str:
    """Canonical selection name (one of ``SELECTIONS``) from a name, a unique prefix (``filt``, ``sub``) or an alias."""
    v = str(value).strip().lower().replace("_", "-")
    v = _SELECTION_ALIASES.get(v.replace("-", ""), _SELECTION_ALIASES.get(v, v))
    if v in SELECTIONS:
        return v
    hits = [s for s in SELECTIONS if s.startswith(v)]
    if len(hits) == 1:
        return hits[0]
    raise SystemExit(f"--selection {value!r} is not one of {list(SELECTIONS)}"
                     + (f" (ambiguous prefix: {hits})" if hits else ""))


def kit() -> Any:
    """The xrlenv SWE-bench Pro kit's ``build_cache`` module — the single source of the id manifests."""
    try:
        from xrlenv_plugins.benchmarks.swebench_pro import build_cache
    except ImportError as exc:  # pragma: no cover - environment problem, not logic
        raise SystemExit("cannot import the xrlenv swebench-pro kit (xrlenv_plugins.benchmarks.swebench_pro): "
                         f"{exc}. Install xrlenv into this venv or add it to PYTHONPATH.") from None
    return build_cache


def shard_root(cache_root: str | None = None) -> Path:
    """``<cache>/swebench-pro`` — the task dirs written by the kit's ``build_cache.py``.

    Cache ROOT: ``cache_root``, else ``$SWEBENCH_PRO_CACHE``, else ``$OPERA_BENCHMARK_CACHE``, else
    ``opera/cache``. Either shard layout resolves: ``<root>/swebench-pro/<id>/`` or
    ``<root>/swebench-pro/golden_patches/<id>/``."""
    root = Path(cache_root or os.environ.get("SWEBENCH_PRO_CACHE") or os.environ.get("OPERA_BENCHMARK_CACHE")
                or (OPERA_ROOT / "cache")).expanduser()
    shard = root / SHARD
    if not _has_task_dirs(shard) and _has_task_dirs(shard / GOLDEN_SUBDIR):
        shard = shard / GOLDEN_SUBDIR
    if not shard.is_dir():
        raise SystemExit(f"{shard} not found — materialize the task dirs first:\n"
                         "  .venv/bin/python <xrlenv>/xrlenv_plugins/benchmarks/swebench_pro/build_cache.py --all\n"
                         "(or pass --cache <root> / export SWEBENCH_PRO_CACHE)")
    return shard


def repo_of(instance_id: str) -> str:
    """``<owner>__<repo>`` (or the bare repo for ids like ``instance_element-…``) — the kit's id shape
    ``instance_<owner>__<repo>-<base sha>-v<…>``."""
    return instance_id.removeprefix("instance_").split("-", 1)[0]


def rest_ids(bc: Any) -> list[str]:
    """``filtered`` minus ``subset-100``, in manifest order."""
    sub = set(bc.read_ids_file(bc.SUBSET_100_IDS))
    return [i for i in bc.read_ids_file(bc.FILTERED_IDS) if i not in sub]


def _ids_from_arg(arg: str) -> list[str]:
    """A file of ids (one per line, ``#`` comments) or a comma list. A comma list of long ids exceeds
    NAME_MAX, so a string that contains a comma is never ``stat()``-ed."""
    if "," not in arg:
        try:
            p = Path(arg).expanduser()
            if p.is_file():
                return [ln.strip() for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip() and not ln.startswith("#")]
        except OSError:
            pass
    return [t.strip() for t in arg.split(",") if t.strip()]


def selection_ids(selection: str, shard: Path) -> list[str]:
    """The instance ids of a selection, from the kit's manifests (``full`` = whatever is materialized)."""
    if selection == "full":
        found = sorted(p.parent.parent.name for p in shard.glob("*/solution/solve.sh"))
        if not found:
            raise SystemExit(f"no task dirs under {shard} — run the kit's build_cache.py first")
        return found
    bc = kit()
    if selection == "filtered":
        return bc.read_ids_file(bc.FILTERED_IDS)
    if selection == "subset-100":
        return bc.read_ids_file(bc.SUBSET_100_IDS)
    if selection == "rest":
        return rest_ids(bc)
    if selection == "rest-heldout":
        return [i for i in rest_ids(bc) if repo_of(i) not in HELDOUT_REPOS]
    if selection == "ood":
        return [i for i in rest_ids(bc) if repo_of(i) in HELDOUT_REPOS]
    if selection == "smoke":
        if not os.environ.get("SWEBENCH_PRO_PARQUET"):
            raise SystemExit("--selection smoke needs $SWEBENCH_PRO_PARQUET (the dataset parquet or a snapshot dir) "
                             "to read the first 8 rows in dataset order; the other selections use the kit's manifests.")
        rows = bc.load_rows(bc.parquet_path())
        return [r["instance_id"] for r in bc.select_rows(rows, all_=False, smoke=True, ids_file=None, instances=None)]
    raise SystemExit(f"unknown selection {selection!r}")


def resolve_tasks(shard: Path, selection: str, tasks_arg: str | None = None) -> list[str]:
    """The task ids to run: ``tasks_arg`` (comma list / id file) wins, else the selection's manifest.
    Every id must be materialized in the shard — a partial populate must never silently shrink a run."""
    want = _ids_from_arg(tasks_arg) if tasks_arg else selection_ids(selection, shard)
    if not want:
        raise SystemExit(f"selection {selection!r} / --tasks selected no tasks")
    missing = [t for t in want if not (shard / t / "solution" / "solve.sh").is_file()]
    if missing:
        raise SystemExit(f"{len(missing)} selected instance(s) are not materialized under {shard} "
                         f"(e.g. {missing[0]}) — run the kit's build_cache.py for this selection first")
    return want


# ── harbor job ────────────────────────────────────────────────────────────────

def build_agent_config(agent: str, *, model: str | None = None, kwargs: dict[str, Any] | None = None,
                       env: dict[str, str] | None = None, timeout_sec: float | None = None) -> Any:
    """``oracle`` → harbor's OracleAgent; ``module:Class`` → an import path; any other name → harbor's own agent by name.
    ``timeout_sec`` overrides the task's agent budget (seconds); ``None`` keeps harbor's defaults."""
    from harbor.models.trial.config import AgentConfig
    kwargs, env = dict(kwargs or {}), dict(env or {})
    if agent == "oracle":
        return AgentConfig(override_timeout_sec=timeout_sec)
    if ":" in agent:
        return AgentConfig(import_path=agent, model_name=model, kwargs=kwargs, env=env, override_timeout_sec=timeout_sec)
    return AgentConfig(name=agent, model_name=model, kwargs=kwargs, env=env, override_timeout_sec=timeout_sec)


def build_job_config(opts: SweepOptions, *, task_ids: list[str], shard: Path, job_id: str, jobs_dir: Path) -> Any:
    """One harbor JobConfig. No ``verifier.import_path``: the task's own ``tests/test.sh`` is the verifier."""
    from harbor.models.job.config import JobConfig, RetryConfig
    from harbor.models.trial.config import TaskConfig
    return JobConfig(
        job_name=job_id, jobs_dir=jobs_dir, n_concurrent_trials=opts.max_workers, n_attempts=max(1, int(opts.attempts)),
        retry=RetryConfig(max_retries=opts.retries, include_exceptions=set(INFRA_RETRY_EXCEPTIONS),
                          min_wait_sec=RETRY_POLICY["min_wait_sec"], wait_multiplier=RETRY_POLICY["wait_multiplier"], max_wait_sec=RETRY_POLICY["max_wait_sec"]),
        environment=build_environment_config("harbor", opts.environment, cpu_pinning=opts.cpu_pinning),
        agents=[build_agent_config(opts.agent, model=opts.model, kwargs=opts.agent_kwargs, env=opts.agent_env,
                                   timeout_sec=opts.agent_timeout_sec)],
        tasks=[TaskConfig(path=shard / tid) for tid in task_ids],
    )


# ── grading (reward.json["reward"] > 0) ───────────────────────────────────────

def trial_passes(tr: Any) -> tuple[bool, str | None]:
    """``(passed, reason-if-not)``: pass ⇔ the verifier recorded ``reward > 0``. The f2p / p2p counts are
    diagnostics appended to the failure reason."""
    ok, reason = trial_passes_default(tr)
    if ok or tr.verifier_result is None or not tr.verifier_result.rewards or tr.exception_info is not None:
        return ok, reason
    detail = {k: v for k, v in tr.verifier_result.rewards.items() if k in ("f2p_passed", "f2p_total", "p2p_passed", "p2p_total")}
    return False, f"{reason} {detail}" if detail else reason


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
    print(f"running {len(task_ids)} {label} task(s) [{selection}] × {opts.attempts} attempt(s) from {shard} on "
          f"{environment_label(opts.environment)} (concurrency={opts.max_workers})\n"
          f"artifacts: {job_dir}", file=sys.stderr)
    config = build_job_config(opts, task_ids=task_ids, shard=shard, job_id=job_id, jobs_dir=job_dir.parent)
    plan = RunPlan(job_cls=Job, config=config, job_dir=job_dir, task_ids=task_ids, k=opts.attempts, mode=opts.mode,
                   label=f"{BENCHMARK} {selection} {label}", trial_passes=trial_passes,
                   protocol={**opts.protocol, "environment": environment_record("harbor", opts.environment, cpu_pinning=opts.cpu_pinning)})
    return await run_attempts(plan)
