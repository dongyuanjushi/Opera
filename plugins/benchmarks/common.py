"""What every benchmark sweep shares: the results layout and job id, the pass@k attempt inventory, the same-id
launch modes (resume / resume-timeout / refresh), the resume preparation, the grading helpers and the run loop.

A job lives in ``results/<benchmark>/<selection>/<job id>/``: harbor / pier's job files, one ``<task>__<id>/``
directory per attempt, ``pass_at_k.json``, ``protocol.json``, ``critic-logs/`` and ``launch.log``.
"""
from __future__ import annotations

import json
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterable, Sequence

OPERA_ROOT = Path(__file__).resolve().parents[2]
RESULTS_ROOT = OPERA_ROOT / "results"
REWARD_KEY = "reward"
MODES = ("resume", "resume-timeout", "refresh")
NO_CRITIC = "nocritic"
TIMEOUT_EXCEPTIONS = frozenset({"AgentTimeoutError"})
INTERRUPTED_EXCEPTIONS = frozenset({"CancelledError"})     # what a SIGTERM / Ctrl-C stop writes into in-flight trials
# Exception TYPE NAMES the trial queue may retry: environment / transport failures the agent cannot cause.
# pier / harbor match the OUTERMOST exception type name, so wrapper classes must be listed too.
INFRA_RETRY_EXCEPTIONS = frozenset({
    # xrlenv cluster
    "CapacityExhausted", "ControlPlaneLost", "NodeLost", "NodeCommandTimeout", "SessionReaped",
    "SessionExpired", "SessionDegraded", "FleetOverBudget", "XRLEnvError",
    "ImagePullFailed", "ImageMissingOnNode", "AssetFetchFailed",
    # pier / harbor trial plumbing (moving dirs in and out of the container, starting it)
    "AddTestsDirError", "DownloadVerifierDirError", "EnvironmentStartTimeoutError",
})
# An attempt that ended in one of these (or was killed from outside, see is_infra_failure) is never graded:
# every resume removes it and the next launch plans it again.
INFRA_FAILURE_EXCEPTIONS = INFRA_RETRY_EXCEPTIONS
KILLED_EXIT_CODES = ("exit 137", "exit 143", "exit -9", "exit -15")
NON_TRIAL_DIRS = frozenset({"critic-logs"})     # job subfolders opera owns
PLACEHOLDER_KEY = "opera_non_trial_dir"         # marks the result.json that shields such a folder (see protect_non_trial_dirs)
TRIAL_MARKERS = ("config.json", "trial.log", "agent", "verifier", "artifacts")


# ── the job id ─────────────────────────────────────────────────────────────────

def build_job_id(*, harness: str, agent: str, critic: str | None, strategy: str | None, tags: Sequence[str] = ()) -> str:
    """``<harness>-<agent>-<critic>-<strategy[-tags]>``, or ``<harness>-<agent>-nocritic[-tags]`` without a critic.

    ``agent`` / ``critic`` are preset names; ``strategy`` defaults to ``operator``; ``tags`` name non-default critic
    policy switches. Protocol settings (effort, turn cap, …) are not in the id: they live in protocol.json."""
    parts = [harness, agent]
    if critic:
        parts += [critic, "-".join([strategy or "operator", *tags])]
    else:
        parts.append("-".join([NO_CRITIC, *tags]) if tags else NO_CRITIC)
    return "-".join(p for p in parts if p)


def job_dir_for(benchmark: str, selection: str, job_id: str, results_root: Path | None = None) -> Path:
    """``<results root>/<benchmark>/<selection>/<job id>`` (``RESULTS_ROOT`` by default)."""
    return Path(results_root or RESULTS_ROOT).expanduser() / benchmark / selection / job_id


# ── trial directories ──────────────────────────────────────────────────────────

def is_placeholder_result(path: Path) -> bool:
    """``path`` is the shield file `protect_non_trial_dirs` writes, not a trial's result.json."""
    try:
        return PLACEHOLDER_KEY in json.loads(path.read_text())
    except Exception:
        return False


def framework_of(job_cls: Any) -> str:
    """``"pier"`` or ``"harbor"`` from the Job class the sweep runs (the two frameworks scan a job dir differently)."""
    return "pier" if (getattr(job_cls, "__module__", "") or "").split(".")[0] == "pier" else "harbor"


def protect_non_trial_dirs(job_dir: Path, framework: str = "harbor") -> list[Path]:
    """Shield opera's non-trial subfolders of a job dir from the framework's resume scan; returns the folders touched.

    ``framework="harbor"``: harbor deletes every subfolder without a result.json but skips one whose result.json it
    cannot parse, so each non-trial folder gets a placeholder result.json.
    ``framework="pier"``: pier loads any folder WITH a result.json as a finished trial, so placeholders are removed."""
    out: list[Path] = []
    if not job_dir.is_dir():
        return out
    if framework == "pier":
        for d in sorted(job_dir.iterdir()):
            res = d / "result.json"
            if d.is_dir() and res.is_file() and is_placeholder_result(res):
                res.unlink(); out.append(d)
        return out
    for d in sorted(job_dir.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        res = d / "result.json"
        if res.exists():
            continue
        if d.name in NON_TRIAL_DIRS or not any((d / f).exists() for f in TRIAL_MARKERS):
            res.write_text(json.dumps({PLACEHOLDER_KEY: d.name, "note": "not a trial: this file keeps harbor's resume "
                                       "cleanup (which removes every job subfolder without a result.json) from deleting "
                                       "the folder; grading and resume preparation skip it"}, indent=2))
            out.append(d)
    return out


def trial_dirs(job_dir: Path) -> list[Path]:
    """Every attempt directory of ``job_dir`` (finished or not); never the shielded non-trial folders."""
    if not job_dir.is_dir():
        return []
    out = []
    for d in sorted(job_dir.iterdir()):
        if not d.is_dir() or d.name.startswith(".") or d.name in NON_TRIAL_DIRS:
            continue
        res = d / "result.json"
        if res.is_file() and is_placeholder_result(res):
            continue
        if res.is_file() or any((d / f).exists() for f in TRIAL_MARKERS):
            out.append(d)
    return out


def trial_task(d: Path) -> str:
    """The requested task id of an attempt directory: the task path in its config.json (the directory name may be
    truncated), else the ``<task>__<id>`` name."""
    try:
        path = (json.loads((d / "config.json").read_text()).get("task") or {}).get("path")
        if path:
            return Path(path).name
    except Exception:
        pass
    return d.name.rsplit("__", 1)[0] if "__" in d.name else d.name


def is_infra_failure(d: Path) -> bool:
    """The attempt's result is a cluster / transport failure, not something the agent did: an infra exception type, or an
    agent process killed from outside (``exit 137`` / ``143``) that left no trajectory behind."""
    res = d / "result.json"
    try:
        r = json.loads(res.read_text())
    except Exception:
        return False
    if not isinstance(r, dict) or PLACEHOLDER_KEY in r:
        return False
    ei = r.get("exception_info") or {}
    exc, msg = ei.get("exception_type"), str(ei.get("exception_message") or "")
    if exc in INFRA_FAILURE_EXCEPTIONS:
        return True
    if exc == "NonZeroAgentExitCodeError" and any(code in msg for code in KILLED_EXIT_CODES):
        return not (d / "agent" / "trajectory.json").is_file()
    return False


def result_exception(d: Path) -> str | None | bool:
    """The exception type recorded in an attempt's result.json; ``None`` for a clean result, ``False`` when the
    attempt has no readable result at all (unfinished)."""
    res = d / "result.json"
    if not res.is_file():
        return False
    try:
        r = json.loads(res.read_text())
    except Exception:
        return False
    if not isinstance(r, dict) or PLACEHOLDER_KEY in r:
        return False
    return (r.get("exception_info") or {}).get("exception_type")


class ResultView:
    """The slice of a trial's result.json the grading helpers read (``task_key`` / ``reward_value`` / ``trial_passes``),
    built from the parsed JSON instead of harbor's / pier's full ``TrialResult`` model."""

    def __init__(self, d: dict[str, Any]) -> None:
        self.trial_name = d.get("trial_name")
        self.task_name = d.get("task_name")
        task_path = ((d.get("config") or {}).get("task") or {}).get("path") or (d.get("task_name") or "")
        self.config = SimpleNamespace(task=SimpleNamespace(path=task_path))
        vr = d.get("verifier_result")
        self.verifier_result = SimpleNamespace(rewards=(vr or {}).get("rewards")) if vr else None
        ei = d.get("exception_info")
        self.exception_info = (SimpleNamespace(exception_type=ei.get("exception_type"), exception_message=ei.get("exception_message"))
                               if ei else None)
        self.agent_execution = d.get("agent_execution")


def load_trial_results(job_dir: Path) -> list[Any]:
    """A ``ResultView`` for every finished, graded attempt of ``job_dir`` (infra failures excluded)."""
    out = []
    for d in trial_dirs(job_dir):
        res = d / "result.json"
        if not res.is_file():
            continue
        try:
            r = json.loads(res.read_text())
        except Exception:
            continue
        if isinstance(r, dict) and PLACEHOLDER_KEY not in r and not is_infra_failure(d):
            view = ResultView(r)
            if not view.config.task.path:                      # a result without config: fall back to the dir name
                view.config.task.path = trial_task(d)
            out.append(view)
    return out


# ── pass@k progress ────────────────────────────────────────────────────────────

@dataclass
class TaskProgress:
    """Attempt counts of one task in a job directory."""
    task: str
    done: int = 0           # finished attempts that count (a real agent / verifier outcome)
    passed: int = 0
    timeouts: int = 0       # among ``done``: attempts that ended in an AgentTimeoutError
    interrupted: int = 0    # attempts a stop left behind (CancelledError, or no result.json)
    infra: int = 0          # attempts the cluster failed: never counted, re-planned


@dataclass
class Inventory:
    """How far a job directory is from ``k`` finished attempts per task, under a launch ``mode`` (one of ``MODES``)."""
    k: int
    tasks: dict[str, TaskProgress]
    mode: str = "resume"

    @property
    def done(self) -> int:
        return sum(t.done for t in self.tasks.values())

    @property
    def passed(self) -> int:
        return sum(t.passed for t in self.tasks.values())

    @property
    def timeouts(self) -> int:
        return sum(t.timeouts for t in self.tasks.values())

    @property
    def interrupted(self) -> int:
        return sum(t.interrupted for t in self.tasks.values())

    @property
    def infra(self) -> int:
        return sum(t.infra for t in self.tasks.values())

    def kept(self, task: TaskProgress) -> int:
        """Attempts of ``task`` that survive the mode (refresh keeps none, resume-timeout drops the timeouts)."""
        if self.mode == "refresh":
            return 0
        return task.done - (task.timeouts if self.mode == "resume-timeout" else 0)

    @property
    def planned(self) -> int:
        """Attempts still to run to reach ``k`` per task."""
        return sum(max(0, self.k - self.kept(t)) for t in self.tasks.values())

    @property
    def complete(self) -> bool:
        return all(self.kept(t) >= self.k for t in self.tasks.values())

    def render(self) -> str:
        """The one-line progress summary printed before a launch."""
        n = len(self.tasks)
        want = n * self.k
        line = (f"pass@{self.k} progress: {self.done}/{want} attempt(s) complete over {n} task(s) "
                f"({self.passed} passed, {self.timeouts} timed out")
        if self.interrupted:
            line += f", {self.interrupted} unfinished"
        if self.infra:
            line += f", {self.infra} cluster failures to re-run"
        line += ")"
        if self.mode == "refresh" and self.done:
            line += f"; refresh: every attempt is removed and {want} are planned"
        elif self.mode == "resume-timeout" and self.timeouts:
            line += f"; resume-timeout: the {self.timeouts} timed-out attempt(s) are removed"
        line += f"; planning {self.planned} attempt(s)" if self.planned else "; nothing to run — the directory already meets pass@" + str(self.k)
        return line


def attempt_inventory(job_dir: Path, task_ids: Sequence[str], k: int, *, mode: str = "resume",
                      trial_passes: Callable[[Any], tuple[bool, str | None]] | None = None) -> Inventory:
    """How far ``job_dir`` is from ``k`` finished attempts per task in ``task_ids`` — read before a launch so only the
    missing attempts are planned. ``mode`` is one of ``MODES``; ``trial_passes`` is the benchmark's own gate
    (default: ``reward > 0``)."""
    passes = trial_passes or (lambda tr: trial_passes_default(tr))
    inv = Inventory(k=k, tasks={t: TaskProgress(t) for t in task_ids}, mode=mode)
    for d in trial_dirs(job_dir):
        task = trial_task(d)
        prog = inv.tasks.get(task)
        if prog is None:                # an attempt of a task outside today's selection: not counted, not touched
            continue
        exc = result_exception(d)
        if exc is False or exc in INTERRUPTED_EXCEPTIONS:
            prog.interrupted += 1
            continue
        if is_infra_failure(d):
            prog.infra += 1
            continue
        prog.done += 1
        if exc in TIMEOUT_EXCEPTIONS:
            prog.timeouts += 1
        try:
            view = ResultView(json.loads((d / "result.json").read_text()))
            if not view.config.task.path:
                view.config.task.path = task
            if passes(view)[0]:
                prog.passed += 1
        except Exception:
            pass
    return inv


# ── the three same-id modes ────────────────────────────────────────────────────

def remove_attempts(job_dir: Path, *, exceptions: Iterable[str] = (), unfinished: bool = True, every: bool = False) -> list[Path]:
    """Delete attempt directories: ``every`` one; else the ``unfinished`` ones (no readable result, an interruption,
    an infra failure) and those whose recorded exception is in ``exceptions``. Returns the removed directories."""
    removed = []
    wanted = frozenset(exceptions)
    for d in trial_dirs(job_dir):
        exc = result_exception(d)
        drop = (every or (unfinished and (exc is False or exc in INTERRUPTED_EXCEPTIONS or is_infra_failure(d)))
                or (exc and exc in wanted))
        if drop:
            shutil.rmtree(d)
            removed.append(d)
    return removed


def refresh_job_dir(job_dir: Path) -> int:
    """``refresh``: remove every attempt and harbor's / pier's job files, keep the directory (launch.log stays).
    Returns the number of attempts removed."""
    n = len(remove_attempts(job_dir, every=True))
    for name in ("config.json", "lock.json", "result.json", "pass_at_k.json"):
        p = job_dir / name
        if p.exists():
            p.unlink()
    logs = job_dir / "critic-logs"
    if logs.is_dir():
        # this run's critic proxy has already written its manifest and proxy.log here: drop only the old
        # per-conversation log folders
        for child in logs.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
    return n


def prepare_job_dir(job_dir: Path, mode: str, config: Any) -> dict[str, Any]:
    """Apply ``mode`` (one of ``MODES``) to an existing job directory and make it resumable under ``config``, the
    launch's JobConfig (a no-op for a new directory). Returns a note for :func:`describe_prepare`."""
    if mode not in MODES:
        raise SystemExit(f"unknown mode {mode!r} (choose from {MODES})")
    note: dict[str, Any] = {"mode": mode, "removed_timeouts": 0, "refreshed": 0}
    if not job_dir.is_dir():
        return note
    if mode == "refresh":
        note["refreshed"] = refresh_job_dir(job_dir)
        return note
    if mode == "resume-timeout":
        note["removed_timeouts"] = len(remove_attempts(job_dir, exceptions=TIMEOUT_EXCEPTIONS, unfinished=False))
    resumed = prepare_resume(job_dir, config)
    if resumed:
        note.update(resumed)
    return note


def describe_prepare(job_id: str, note: dict[str, Any]) -> str:
    """One line saying what :func:`prepare_job_dir` did to the directory."""
    if note["mode"] == "refresh":
        return f"refresh {job_id}: removed {note['refreshed']} attempt(s); starting over"
    if "kept" not in note:
        return f"{job_id}: new job directory"
    s = (f"{note['mode']} {job_id}: {note['kept']} finished attempt(s) kept; removed {note['removed_interrupted']} interrupted + "
         f"{note.get('removed_infra', 0)} cluster-failed")
    if note["mode"] == "resume-timeout":
        s += f" + {note['removed_timeouts']} timed-out"
    s += " attempt dir(s); " + (f"config updated: {', '.join(note['updated'])}" if note["updated"] else "config unchanged")
    return s


# ── resume preparation (identical-config rule + volatile fields) ───────────────

# Fields that legitimately differ between two launches of the SAME run: the model endpoint / key, the critic proxy
# URL, the served context window, the concurrency, the retry policy and the number of attempts.
VOLATILE_AGENT_ENV = frozenset({"LLM_BASE_URL", "LLM_API_KEY", "OPENAI_BASE_URL", "OPENAI_API_BASE", "OPENAI_API_KEY", "MSWEA_API_KEY"})
VOLATILE_AGENT_KWARGS = frozenset({"critic_proxy_url", "api_base", "model_info"})
VOLATILE_JOB_FIELDS = frozenset({"n_concurrent_trials", "n_attempts"})     # + every retry.* field (see _is_volatile)
# The infra retry backoff (seconds) for a trial whose exception is in INFRA_RETRY_EXCEPTIONS: about 12 minutes of
# waiting in total, enough to survive a short cluster outage.
RETRY_POLICY = {"max_retries": 10, "min_wait_sec": 5.0, "wait_multiplier": 2.0, "max_wait_sec": 120.0}


def _flatten(o: Any, prefix: str = "") -> dict[str, Any]:
    """dotted-path → value; a list of scalars compares order-free (pier serialises its exception SETS as lists)."""
    if isinstance(o, dict):
        out: dict[str, Any] = {}
        for k, v in o.items():
            out.update(_flatten(v, f"{prefix}{k}."))
        return out
    if isinstance(o, list):
        if all(not isinstance(v, (dict, list)) for v in o):
            return {prefix[:-1]: sorted(o, key=str)}
        out = {}
        for i, v in enumerate(o):
            out.update(_flatten(v, f"{prefix}{i}."))
        return out
    return {prefix[:-1]: o}


_ABSENT = object()
_REDACTED = re.compile(r"^(\*{4}|.{4}\*{4}.{3})$")     # how harbor / pier mask secret-looking env values in config.json


def is_redacted(v: Any) -> bool:
    """``v`` is a value harbor / pier masked when writing config.json (e.g. ``****`` for ``LLM_MAX_INPUT_TOKENS``,
    whose key matches their secret-key pattern); a relaunch re-provides the real value."""
    return isinstance(v, str) and bool(_REDACTED.match(v))


def _defaulted(v: Any) -> bool:
    """A value a serializer that prunes defaults would have omitted (``None`` / ``False`` / empty container)."""
    return v is None or v is False or (isinstance(v, (list, dict, str)) and len(v) == 0)


def _config_diff(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """Dotted paths that differ, ignoring a field present on one side only at a default-ish value (harbor writes
    config.json without defaulted fields, pier writes every field)."""
    out = []
    for path in set(old) | set(new):
        a, b = old.get(path, _ABSENT), new.get(path, _ABSENT)
        if a is _ABSENT and b is _ABSENT or a == b:
            continue
        if (a is _ABSENT and _defaulted(b)) or (b is _ABSENT and _defaulted(a)):
            continue
        out.append(path)
    return sorted(out)


def _is_volatile(path: str) -> bool:
    """``path`` (a dotted JobConfig path) may change between launches of the same run."""
    parts = path.split(".")
    return (path in VOLATILE_JOB_FIELDS or path.startswith("retry.")
            or (len(parts) == 4 and parts[0] == "agents" and parts[2] == "env" and parts[3] in VOLATILE_AGENT_ENV)
            or (len(parts) >= 4 and parts[0] == "agents" and parts[2] == "kwargs" and parts[3] in VOLATILE_AGENT_KWARGS))   # nested (model_info.*) too


def _match_agent(agent: dict[str, Any], agents: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The new launch's agent with the same import path and model as ``agent`` (else the first one)."""
    return next((a for a in agents if a.get("import_path") == agent.get("import_path") and a.get("model_name") == agent.get("model_name")),
                agents[0] if agents else None)


def _patch_agent_volatile(agent: dict[str, Any], match: dict[str, Any]) -> None:
    """Copy the volatile env / kwargs of ``match`` into ``agent`` (removing those ``match`` lacks)."""
    env, kw = agent.setdefault("env", {}), agent.setdefault("kwargs", {})
    for k in VOLATILE_AGENT_ENV:
        if k in (match.get("env") or {}):
            env[k] = match["env"][k]
        else:
            env.pop(k, None)
    for k in VOLATILE_AGENT_KWARGS:
        if k in (match.get("kwargs") or {}):
            kw[k] = match["kwargs"][k]
        else:
            kw.pop(k, None)


def _patch_trial_config(path: Path, agents: list[dict[str, Any]]) -> None:
    """Rewrite the volatile agent env/kwargs of a kept trial's config.json to the new launch's values — pier
    matches kept trials to planned ones by TrialConfig equality, agent env and kwargs included."""
    tc = json.loads(path.read_text())
    agent = tc.get("agent") or {}
    match = _match_agent(agent, agents)
    if match is None:
        return
    _patch_agent_volatile(agent, match)
    tc["agent"] = agent
    path.write_text(json.dumps(tc, indent=4))


def _patch_lock(path: Path, new: dict[str, Any]) -> None:
    """Patch the volatile fields of lock.json, which records the resolved run a second time and is compared on
    resume like config.json."""
    lock = json.loads(path.read_text())
    lock["n_concurrent_trials"] = new.get("n_concurrent_trials", lock.get("n_concurrent_trials"))
    if isinstance(new.get("retry"), dict):
        lock["retry"] = new["retry"]
    agents = new.get("agents") or []
    for trial in lock.get("trials") or []:
        agent = trial.get("agent") or {}
        match = _match_agent(agent, agents)
        if match is not None:
            _patch_agent_volatile(agent, match)
            trial["agent"] = agent
    path.write_text(json.dumps(lock, indent=4))


def prepare_resume(job_dir: Path, config: Any) -> dict[str, Any] | None:
    """Make an interrupted job dir resumable under ``config`` (the launch's JobConfig); ``None`` when the job is new.

    pier and harbor resume a job dir only under an IDENTICAL JobConfig and treat every trial dir with a result.json
    as a completed attempt. So, before ``Job.create``:

    1. interrupted, result-less and infra-failed trial dirs are removed (planned again); completed ones are kept;
    2. the volatile fields are rewritten into config.json, lock.json and every kept trial's config.json; any OTHER
       difference raises ``SystemExit`` listing the paths. A changed ``n_attempts`` drops the lock (it is rebuilt).

    Returns ``{"kept", "removed_interrupted", "removed_infra", "removed_partial", "updated"}``."""
    cfg_path = job_dir / "config.json"
    if not cfg_path.is_file():
        return None
    new = json.loads(config.model_dump_json())
    text = cfg_path.read_text()
    try:      # harbor prunes defaults out of config.json; round-tripping through the model restores them
        old = json.loads(type(config).model_validate_json(text).model_dump_json())
    except Exception:                       # a config the current models no longer accept: compare the raw file
        old = json.loads(text)
    fo, fn = _flatten(old), _flatten(new)
    diff = _config_diff(fo, fn)
    hard = [p for p in diff if not _is_volatile(p) and not is_redacted(fo.get(p))]   # a masked value is re-provided, not a change
    if hard:
        raise SystemExit(f"{job_dir} holds a run with a different config ({', '.join(hard)}): restore those settings to resume it, "
                         "pass --mode refresh to start over, or use another --job-id")
    removed_infra = sum(1 for d in trial_dirs(job_dir) if is_infra_failure(d))
    removed_interrupted = len(remove_attempts(job_dir, unfinished=True)) - removed_infra
    kept = 0
    for d in trial_dirs(job_dir):
        kept += 1
        if (d / "config.json").is_file():
            _patch_trial_config(d / "config.json", new.get("agents") or [])
    if diff:
        cfg_path.write_text(config.model_dump_json(indent=4))
    lock = job_dir / "lock.json"
    if lock.is_file():
        if "n_attempts" in diff:
            lock.unlink()                        # the lock lists every planned trial: rebuilt for the new attempt count
        else:
            _patch_lock(lock, new)
    return {"kept": kept, "removed_interrupted": removed_interrupted, "removed_infra": removed_infra, "removed_partial": 0, "updated": diff}


# ── grading (reward > 0) ───────────────────────────────────────────────────────

def task_key(tr: Any) -> str:
    """The requested task id of a trial result (the task dir name, not task.toml's namespaced name)."""
    return Path(tr.config.task.path).name


def reward_value(tr: Any) -> float | None:
    """The verifier's ``reward`` as a float; ``None`` when missing or not numeric."""
    vr = tr.verifier_result
    if vr is None or vr.rewards is None or vr.rewards.get(REWARD_KEY) is None:
        return None
    try:
        return float(vr.rewards[REWARD_KEY])
    except (TypeError, ValueError):
        return None


def agent_exception(tr: Any) -> str | None:
    """The exception type recorded on a trial result, or ``None``."""
    return tr.exception_info.exception_type if tr.exception_info is not None else None


def trial_passes_default(tr: Any, *, graded_exceptions: frozenset[str] = frozenset()) -> tuple[bool, str | None]:
    """``(passed, reason-if-not)``: pass ⇔ the verifier recorded ``reward > 0``. An exception fails the trial unless
    its type is in ``graded_exceptions`` (agent-phase exceptions after which the verifier still runs)."""
    exc = agent_exception(tr)
    if exc is not None and exc not in graded_exceptions:
        return False, f"exception: {exc}"
    vr = tr.verifier_result
    if vr is None or not vr.rewards:
        return False, "no verifier rewards recorded" + (f" (exception: {exc})" if exc else "")
    r = reward_value(tr)
    if r is None:
        return False, f"no {REWARD_KEY!r} key in rewards={list(vr.rewards)}"
    if r > 0:
        return True, None
    return False, f"reward={r}" + (f" ({exc})" if exc else "")


# ── the shared run loop ────────────────────────────────────────────────────────

@dataclass
class RunPlan:
    """What a benchmark sweep hands to :func:`run_attempts`."""
    job_cls: Any                       # harbor.job.Job or pier.job.Job
    config: Any                        # its JobConfig (job_name = the job id, jobs_dir = results/<bench>/<selection>)
    job_dir: Path
    task_ids: list[str]
    k: int                             # attempts wanted per task
    mode: str                          # one of MODES
    label: str
    trial_passes: Callable[[Any], tuple[bool, str | None]]
    protocol: dict[str, Any] = field(default_factory=dict)      # written to <job>/protocol.json before the run
    report_lines: Callable[[Any, list[Any]], list[str]] | None = None   # benchmark-specific summary lines (report, results)


def write_protocol(job_dir: Path, rec: dict[str, Any]) -> Path:
    """Write ``rec`` to ``<job_dir>/protocol.json`` and return the path."""
    job_dir.mkdir(parents=True, exist_ok=True)
    path = job_dir / "protocol.json"
    path.write_text(json.dumps(rec, indent=1, default=str), encoding="utf-8")
    return path


async def run_attempts(plan: RunPlan) -> int:
    """Take stock, apply the mode, run the missing attempts, aggregate pass@k over EVERY attempt in the directory.

    Exit code: 0 when every task has a passing attempt, 1 when some task has none, 2 when tasks are still short of
    ``k`` attempts."""
    from plugins.benchmarks.passk import aggregate
    inv = attempt_inventory(plan.job_dir, plan.task_ids, plan.k, mode=plan.mode, trial_passes=plan.trial_passes)
    print(inv.render(), file=sys.stderr)
    if plan.mode != "refresh":
        extra = sorted({trial_task(d) for d in trial_dirs(plan.job_dir)} - set(plan.task_ids))
        if extra:
            raise SystemExit(f"{plan.job_dir} holds attempts of {len(extra)} task(s) outside today's selection (e.g. {extra[0]}); harbor "
                             "resumes a directory only under the same task set — run the same selection, --mode refresh, or another --job-id")
    over = [t for t, prog in inv.tasks.items() if inv.kept(prog) > plan.k]
    if over:
        raise SystemExit(f"{plan.job_dir} already holds more than {plan.k} finished attempt(s) for {len(over)} task(s) "
                         f"(e.g. {over[0]}: {inv.kept(inv.tasks[over[0]])}); pass a --pass-at-k of at least that, or --mode refresh")
    note = prepare_job_dir(plan.job_dir, plan.mode, plan.config)
    print(describe_prepare(plan.config.job_name, note), file=sys.stderr)
    if plan.protocol:
        write_protocol(plan.job_dir, plan.protocol)
    protect_non_trial_dirs(plan.job_dir, framework_of(plan.job_cls))
    if inv.planned or plan.mode == "refresh":
        job = await plan.job_cls.create(plan.config)
        await job.run()
    results = load_trial_results(plan.job_dir)
    report = aggregate(results, k=plan.k, task_key=task_key, trial_passes=plan.trial_passes, reward_value=reward_value,
                       expected_tasks=plan.task_ids)
    report.write(plan.job_dir / "pass_at_k.json")
    print(report.render(plan.label))
    for line in (plan.report_lines(report, results) if plan.report_lines else []):
        print(line)
    print(f"per-attempt outcomes: {plan.job_dir / 'pass_at_k.json'}")
    short = [t for t in plan.task_ids if report.tasks[t].n < plan.k]
    if short:
        print(f"{len(short)} task(s) still short of {plan.k} attempt(s) (e.g. {short[0]}) — relaunch to continue", file=sys.stderr)
        return 2
    return 0 if all(t.any for t in report.tasks.values()) else 1
