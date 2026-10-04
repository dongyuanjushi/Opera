"""SWE-rebench sweep driver: one harbor ``Job`` on xrlenv or native Docker with a pluggable agent.

Tasks are the harbor task dirs the xrlenv kit materializes under ``$XRLENV_BENCHMARK_CACHE/swe-rebench``; the
task's own ``tests/test.sh`` is the verifier and pass ⇔ ``reward > 0``. Budget: each task's ``[agent] timeout_sec``.

Selections (prefix-matched): ``repos`` (``selections/repos.txt``: the newest task of every repository), ``green``
(the kit's split index minus ``EXCLUDED``), ``smoke`` (the kit's ``scripts/smoke_30tasks.txt``), ``full`` (every
task in the shard). ``--tasks split:<YYYY_MM>`` runs one monthly split.
"""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from plugins.benchmarks.common import (INFRA_RETRY_EXCEPTIONS, RESULTS_ROOT, RETRY_POLICY, RunPlan, job_dir_for, reward_value, run_attempts,  # noqa: F401
                                       task_key, trial_passes_default)
from plugins.benchmarks.environments import build_environment_config, environment_label, environment_record, require_environment

HERE = Path(__file__).resolve().parent
BENCHMARK = "swe-rebench"
SHARD = "swe-rebench"                      # the kit's build_cache.SHARD
SELECTIONS = ("repos", "green", "smoke", "full")
REPOS_SELECTION = HERE / "selections" / "repos.txt"
# The kit's EXCLUDE (run_full_sweep.sh): non-hermetic verifiers and ungateable upstream content. Mirrored here
# because the kit keeps the list in a shell array.
EXCLUDED = frozenset({"canonical__charmcraft-2084", "sigma67__ytmusicapi-909_interface",
                      "bluesky__ophyd-async-1165", "modin-project__modin-7434"})
_ID_SUFFIX = re.compile(r"-(\d+)(_[a-z]+)?$")     # <owner>__<repo>-<pr>[_interface]


def _has_task_dirs(d: Path) -> bool:
    return d.is_dir() and next(d.glob("*/task.toml"), None) is not None


@dataclass
class SweepOptions:
    """One SWE-rebench sweep. ``agent``: ``oracle`` | a ``module:Class`` import path | a harbor agent name."""
    agent: str = "oracle"
    model: str | None = None
    agent_kwargs: dict[str, Any] = field(default_factory=dict)
    agent_env: dict[str, str] = field(default_factory=dict)
    selection: str = "repos"                # one of SELECTIONS (or a unique prefix)
    tasks: str | None = None                # comma list, id file or ``split:<YYYY_MM>`` — overrides ``selection``
    cache_root: str | None = None
    max_workers: int = 4
    results_root: Path = RESULTS_ROOT
    job_id: str | None = None
    retries: int = 6                        # infra retries per trial
    attempts: int = 1                       # pass@k: k finished attempts per task in the job directory
    mode: str = "resume"                    # resume | resume-timeout | refresh
    cpu_pinning: bool | None = None         # None = automatic: xrlenv pins; native Docker uses CPU quotas
    protocol: dict[str, Any] = field(default_factory=dict)
    environment: str = "xrlenv"


# ── the corpus ────────────────────────────────────────────────────────────────

def repo_of(instance_id: str) -> str:
    """``<owner>__<repo>`` — the instance id without its trailing PR number (and ``_interface`` marker)."""
    return _ID_SUFFIX.sub("", instance_id)


def pr_of(instance_id: str) -> int:
    """The trailing PR number of an instance id (0 when it has none)."""
    m = _ID_SUFFIX.search(instance_id)
    return int(m.group(1)) if m else 0


def kit_dir() -> Path:
    """The xrlenv SWE-rebench kit directory — the single source of the split index and the smoke set."""
    try:
        import xrlenv_plugins.benchmarks.swe_rebench as k
    except ImportError as exc:  # pragma: no cover - environment problem, not logic
        raise SystemExit("cannot import the xrlenv swe-rebench kit (xrlenv_plugins.benchmarks.swe_rebench): "
                         f"{exc}. Install xrlenv into this venv or add it to PYTHONPATH.") from None
    # the kit is a namespace package (no __init__.py): locate it by its __path__
    location = getattr(k, "__file__", None) or next(iter(k.__path__), None)
    if not location:
        raise SystemExit("the xrlenv swe-rebench kit has no filesystem location")
    d = Path(location).resolve()
    return d.parent if d.is_file() else d


def split_index(kit: Path | None = None) -> dict[str, list[str]]:
    """``{split: [instance_id, …]}`` from the kit's ``scripts/monthly_splits.json``."""
    path = (kit or kit_dir()) / "scripts" / "monthly_splits.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    splits = data.get("splits", data)
    if not isinstance(splits, dict) or not all(isinstance(v, list) for v in splits.values()):
        raise SystemExit(f"unexpected split index shape in {path}")
    return {k: list(v) for k, v in sorted(splits.items())}


def green_ids(kit: Path | None = None) -> list[str]:
    """The gateable corpus: every indexed id minus the kit's excluded ones, in split order."""
    return [i for ids in split_index(kit).values() for i in ids if i not in EXCLUDED]


def read_ids_file(path: Path) -> list[str]:
    """One id per line; ``#`` starts a comment (whole line or trailing)."""
    out = []
    for ln in Path(path).read_text(encoding="utf-8").splitlines():
        s = ln.split("#", 1)[0].strip()
        if s:
            out.append(s)
    return out


def repos_selection(index: dict[str, list[str]] | None = None) -> list[str]:
    """Regenerate the ``repos`` selection from the split index: per repository the newest task (latest split,
    then the highest PR number), excluded tasks dropped, sorted by repository."""
    index = index or split_index()
    best: dict[str, tuple[str, int, str]] = {}
    for split, ids in index.items():
        for iid in ids:
            if iid in EXCLUDED:
                continue
            key = (split, pr_of(iid), iid)
            r = repo_of(iid)
            if r not in best or key > best[r]:
                best[r] = key
    return [best[r][2] for r in sorted(best)]


# ── selection ─────────────────────────────────────────────────────────────────

def resolve_selection(value: str) -> str:
    """Canonical selection name from a unique PREFIX (``rep`` | ``gr`` | ``sm`` | ``fu``)."""
    v = str(value).strip().lower().replace("_", "-")
    if v in SELECTIONS:
        return v
    hits = [s for s in SELECTIONS if s.startswith(v)]
    if len(hits) == 1:
        return hits[0]
    raise SystemExit(f"--selection {value!r} is not one of {list(SELECTIONS)}"
                     + (f" (ambiguous prefix: {hits})" if hits else ""))


def shard_root(cache_root: str | None = None) -> Path:
    """``<cache>/swe-rebench`` — the task dirs written by the kit's ``build_cache.py``. Cache ROOT: ``cache_root``,
    else ``$XRLENV_BENCHMARK_CACHE`` (where the kit writes)."""
    root = cache_root or os.environ.get("XRLENV_BENCHMARK_CACHE")
    if not root:
        raise SystemExit("no cache root — pass --cache <root> or export XRLENV_BENCHMARK_CACHE (the kit's shard root)")
    shard = Path(root).expanduser() / SHARD
    if not _has_task_dirs(shard):
        raise SystemExit(f"{shard} has no task dirs — materialize the corpus first (no images are pulled by this step):\n"
                         "  .venv/bin/python <xrlenv>/xrlenv_plugins/benchmarks/swe_rebench/build_cache.py --stage all\n"
                         "then warm the images on the cluster from the kit's build plan.")
    return shard


def _ids_from_arg(arg: str) -> list[str]:
    """``split:<YYYY_MM>``, a file of ids, or a comma list."""
    if arg.startswith("split:"):
        name = arg.split(":", 1)[1].replace("-", "_")
        index = split_index()
        if name not in index:
            raise SystemExit(f"unknown monthly split {name!r}; known: {list(index)}")
        return [i for i in index[name] if i not in EXCLUDED]
    if "," not in arg:
        try:
            p = Path(arg).expanduser()
            if p.is_file():
                return read_ids_file(p)
        except OSError:
            pass
    return [t.strip() for t in arg.split(",") if t.strip()]


def selection_ids(selection: str, shard: Path) -> list[str]:
    """The instance ids of ``selection`` (one of ``SELECTIONS``); ``full`` = whatever is materialized in ``shard``."""
    if selection == "full":
        found = sorted(p.parent.parent.name for p in shard.glob("*/solution/solve.sh"))
        if not found:
            raise SystemExit(f"no task dirs under {shard} — run the kit's build_cache.py first")
        return found
    if selection == "repos":
        return read_ids_file(REPOS_SELECTION)
    if selection == "green":
        return green_ids()
    if selection == "smoke":
        return read_ids_file(kit_dir() / "scripts" / "smoke_30tasks.txt")
    raise SystemExit(f"unknown selection {selection!r}")


def resolve_tasks(shard: Path, selection: str, tasks_arg: str | None = None) -> list[str]:
    """The task ids to run: ``tasks_arg`` wins, else the selection. Every id must be materialized in the shard —
    a partial populate must never silently shrink a run."""
    want = _ids_from_arg(tasks_arg) if tasks_arg else selection_ids(selection, shard)
    if not want:
        raise SystemExit(f"selection {selection!r} / --tasks selected no tasks")
    missing = [t for t in want if not (shard / t / "solution" / "solve.sh").is_file()]
    if missing:
        raise SystemExit(f"{len(missing)} selected instance(s) are not materialized under {shard} "
                         f"(e.g. {missing[0]}) — run the kit's build_cache.py first")
    return want


# ── harbor job ────────────────────────────────────────────────────────────────

def build_agent_config(agent: str, *, model: str | None = None, kwargs: dict[str, Any] | None = None,
                       env: dict[str, str] | None = None) -> Any:
    """``oracle`` → harbor's OracleAgent; ``module:Class`` → an import path; any other name → harbor's own agent by name."""
    from harbor.models.trial.config import AgentConfig
    kwargs, env = dict(kwargs or {}), dict(env or {})
    if agent == "oracle":
        return AgentConfig()
    if ":" in agent:
        return AgentConfig(import_path=agent, model_name=model, kwargs=kwargs, env=env)
    return AgentConfig(name=agent, model_name=model, kwargs=kwargs, env=env)


def build_job_config(opts: SweepOptions, *, task_ids: list[str], shard: Path, job_id: str, jobs_dir: Path) -> Any:
    """One harbor JobConfig. No ``verifier.import_path``: the task's own ``tests/test.sh`` is the verifier."""
    from harbor.models.job.config import JobConfig, RetryConfig
    from harbor.models.trial.config import TaskConfig
    return JobConfig(
        job_name=job_id, jobs_dir=jobs_dir, n_concurrent_trials=opts.max_workers, n_attempts=max(1, int(opts.attempts)),
        retry=RetryConfig(max_retries=opts.retries, include_exceptions=set(INFRA_RETRY_EXCEPTIONS),
                          min_wait_sec=RETRY_POLICY["min_wait_sec"], wait_multiplier=RETRY_POLICY["wait_multiplier"], max_wait_sec=RETRY_POLICY["max_wait_sec"]),
        environment=build_environment_config("harbor", opts.environment, cpu_pinning=opts.cpu_pinning),
        agents=[build_agent_config(opts.agent, model=opts.model, kwargs=opts.agent_kwargs, env=opts.agent_env)],
        tasks=[TaskConfig(path=shard / tid) for tid in task_ids],
    )


# ── grading (reward.json["reward"] > 0) ───────────────────────────────────────

def trial_passes(tr: Any) -> tuple[bool, str | None]:
    """Pass ⇔ the task's verifier recorded ``reward > 0`` (upstream's resolved rule)."""
    return trial_passes_default(tr)


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


if __name__ == "__main__":                   # maintenance: regenerate selections/repos.txt from the kit's index
    if "--regenerate-repos" in sys.argv:
        import collections, datetime
        index = split_index()
        ids = repos_selection(index)
        where = {i: s for s, v in index.items() for i in v}
        per_split = collections.Counter(where[i] for i in ids)
        head = (f"# swe-rebench — the `repos` selection: ONE task per repository, every repository covered.\n#\n"
                f"# Generated {datetime.date.today().isoformat()} from the xrlenv kit's monthly-split index\n"
                "# (xrlenv_plugins/benchmarks/swe_rebench/scripts/monthly_splits.json: 860 tasks, 15 monthly splits\n"
                "# 2025_01..2026_03, all Python) minus the kit's 4 excluded ungateable tasks. Repository = the instance id\n"
                "# without its trailing PR number. Per repository the NEWEST task is chosen (latest monthly split, then the\n"
                "# highest PR number) — the least likely to sit in a model's pretraining data.\n#\n"
                f"# {len(ids)} tasks / {len(ids)} repositories. Per split: {dict(sorted(per_split.items()))}\n"
                "# Regenerate with: PYTHONPATH=. .venv/bin/python plugins/benchmarks/swe_rebench/sweep.py --regenerate-repos\n#\n"
                "# instance_id  (split, repo, tasks-in-repo are comments)\n")
        n_in_repo = collections.Counter(repo_of(i) for v in index.values() for i in v if i not in EXCLUDED)
        REPOS_SELECTION.write_text(head + "".join(f"{i}    # {where[i]} {repo_of(i)} n={n_in_repo[repo_of(i)]}\n" for i in ids), encoding="utf-8")
        print(f"wrote {REPOS_SELECTION} ({len(ids)} tasks)")
    else:
        print(__doc__)
