#!/usr/bin/env python3
"""Evaluate one served checkpoint on a fixed task split by running src/launch.py (swebench-pro) and scoring the job.

Appends one JSON row per call to --history: per-cell and overall pass counts (pass@k means per cell when k > 1).
Needs the checkpoint served (serve_checkpoint.sh) and SFT_CRITIC_BASE_URL exported.

    python src/sft/pipeline/eval_checkpoint.py --step 150 --mode critic [-- <extra src/launch.py flags>]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

OPERA_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(OPERA_ROOT))
sys.path.insert(0, str(OPERA_ROOT / "src" / "sft" / "preprocess"))

from plugins.benchmarks.common import job_dir_for  # noqa: E402
from run_records import task_results  # noqa: E402


def load_dotenv(path: Path) -> None:
    """Export KEY=VALUE lines of a .env file without overriding variables already set."""
    if not path.exists():
        return
    import os
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip("'\""))


# mode -> (agent model, critic model); "base" = --base-model, "checkpoint" = --checkpoint-model, None = no critic
MODES = {
    "critic":        ("base", "checkpoint"),
    "self-critic":   ("checkpoint", "checkpoint"),
    "agent":         ("checkpoint", None),
    "baseline":      ("base", None),
}


def models_for(a) -> tuple[str, str | None]:
    """(agent preset, critic preset or None) for the selected --mode."""
    agent_key, critic_key = MODES[a.mode]
    pick = {"base": a.base_model, "checkpoint": a.checkpoint_model}
    return pick[agent_key], (pick[critic_key] if critic_key else None)


def run_launcher(a, tasks: list[str], job_id: str, agent_model: str, critic_model: str | None) -> Path:
    """Run src/launch.py swebench-pro on `tasks` as job `job_id`; returns the job directory."""
    cmd = [str(a.python), "src/launch.py", "swebench-pro", "--harness", a.harness, "--agent-model", agent_model,
           "--selection", a.selection, "--tasks", ",".join(tasks), "--pass-at-k", str(a.pass_at_k),
           "--max-workers", str(a.max_workers), "--max-turns", str(a.max_turns), "--job-id", job_id]
    if critic_model:
        cmd += ["--critic-model", critic_model]
    if a.critic_reasoning_effort and critic_model:
        cmd += ["--critic-reasoning-effort", a.critic_reasoning_effort]
    if a.record_prompts:
        cmd += ["--critic-record-prompts"]
    cmd += a.extra
    print("+", " ".join(cmd), flush=True)
    if a.dry_run:
        cmd += ["--dry-run"]
    # a non-zero exit also means "some task failed"; the graded trials are scored regardless
    rc = subprocess.run(cmd, cwd=OPERA_ROOT).returncode
    if rc:
        print(f"[warn] launcher exited {rc}; scoring the trials it produced", flush=True)
    return job_dir_for("swebench-pro", a.selection, job_id)


def attempt_results(job: Path) -> dict[str, list[bool]]:
    """task -> pass/fail of each graded attempt in `job`, oldest first."""
    import glob
    out: dict[str, list[tuple[str, bool]]] = {}
    for p in glob.glob(str(job / "*__*" / "result.json")):
        try:
            r = json.loads(Path(p).read_text())
        except (OSError, ValueError):
            continue
        t = (r.get("task_name") or Path(p).parent.name.rsplit("__", 1)[0]).split("/")[-1]
        rew = ((r.get("verifier_result") or {}).get("rewards") or {}).get("reward")
        out.setdefault(t, []).append((r.get("started_at") or "", rew == 1))
    return {t: [ok for _, ok in sorted(v)] for t, v in out.items()}


def score(job: Path, split: dict, k: int = 1) -> dict:
    """{"cells", "overall"} pass counts of `job` over split["cells"]; k > 1 reports per-cell means over attempts,
    per-attempt totals and any-of-k counts."""
    if k > 1:
        att = attempt_results(job)
        cells = {}
        for cell, tasks in split["cells"].items():
            done = [t for t in tasks if t in att]
            cells[cell] = {"n": len(tasks), "graded": len(done), "passed": round(sum(sum(att[t]) / len(att[t]) for t in done), 2),
                           "any": sum(any(att[t]) for t in done)}
        overall = {"graded": sum(c["graded"] for c in cells.values()), "passed": round(sum(c["passed"] for c in cells.values()), 2),
                   "k": k, "attempts": [sum(1 for t in att if len(att[t]) > i and att[t][i]) for i in range(k)],
                   "any": sum(any(v) for v in att.values())}
    else:
        got = task_results(job)
        cells = {}
        for cell, tasks in split["cells"].items():
            done = [t for t in tasks if t in got]
            cells[cell] = {"n": len(tasks), "graded": len(done), "passed": sum(got[t][0] for t in done)}
        overall = {"graded": sum(c["graded"] for c in cells.values()), "passed": sum(c["passed"] for c in cells.values())}
    # cell names combine a domain and a family in either order (iid/passed, stable_pass/iid, ...)
    for domain in ("iid", "ood"):
        keys = [k for k in cells if domain in k]
        overall[domain] = {"graded": sum(cells[k]["graded"] for k in keys),
                           "passed": sum(cells[k]["passed"] for k in keys)}
    base_keys = [k for k in cells if "passed" in k or "stable_pass" in k]
    overall["baseline_passed"] = sum(cells[k]["n"] for k in base_keys)
    for fam in ("stable_pass", "rescued", "flaky"):
        keys = [k for k in cells if k.startswith(fam)]
        if keys:
            overall[fam] = {"graded": sum(cells[k]["graded"] for k in keys),
                            "passed": sum(cells[k]["passed"] for k in keys)}
    return {"cells": cells, "overall": overall}


def main() -> None:
    """Parse flags, run (or with --score-only just score) the job, append the history row and print a summary."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--eval-split", type=Path, required=True,
                    help="split JSON with 'tasks' (list) and 'cells' (cell name -> tasks)")
    ap.add_argument("--step", required=True, help="checkpoint label (the training step, or 'base' for the untrained model)")
    ap.add_argument("--mode", choices=sorted(MODES), default="critic",
                    help="critic = base policy + trained critic; self-critic = the checkpoint as policy AND critic; "
                         "agent = the checkpoint alone; baseline = the untrained policy alone")
    ap.add_argument("--checkpoint-model", default="sft-critic",
                    help="preset name of the served checkpoint (policy- and critic-models.yaml both define sft-critic)")
    ap.add_argument("--base-model", default="qwen35-9b", help="the untrained policy preset")
    ap.add_argument("--critic-reasoning-effort", default=None, help="passed to src/launch.py when a critic runs")
    ap.add_argument("--harness", default="openhands", help="agent harness for src/launch.py")
    ap.add_argument("--selection", required=True, help="swebench-pro selection the split's tasks belong to")
    ap.add_argument("--pass-at-k", type=int, default=1, help="attempts per task; >1 reports per-attempt totals and the mean per cell")
    ap.add_argument("--max-turns", type=int, default=150, help="agent turn limit per trial")
    ap.add_argument("--max-workers", type=int, default=10, help="concurrent trials")
    ap.add_argument("--job-prefix", default="sft-eval",
                    help="job id = <prefix>-<mode>-<agent>-<critic|nocritic>-step<step>")
    ap.add_argument("--record-prompts", action="store_true", help="keep the critic's own calls (more training data)")
    ap.add_argument("--history", type=Path, default=OPERA_ROOT / "src/sft/data/eval-history.jsonl",
                    help="JSONL that every evaluation appends one row to")
    ap.add_argument("--python", type=Path, default=OPERA_ROOT / ".venv/bin/python", help="interpreter for src/launch.py")
    ap.add_argument("--dry-run", action="store_true", help="pass --dry-run to src/launch.py and skip scoring")
    ap.add_argument("--score-only", action="store_true", help="skip the run and score an existing job directory")
    ap.add_argument("extra", nargs="*", help="extra flags passed through to src/launch.py")
    a = ap.parse_args()

    load_dotenv(OPERA_ROOT / ".env")
    split = json.loads(a.eval_split.read_text())
    agent_model, critic_model = models_for(a)
    job_id = f"{a.job_prefix}-{a.mode}-{agent_model}-{critic_model or 'nocritic'}-step{a.step}"
    started = time.time()
    job = (job_dir_for("swebench-pro", a.selection, job_id) if a.score_only
           else run_launcher(a, split["tasks"], job_id, agent_model, critic_model))
    if a.dry_run:
        return
    row = {"step": a.step, "mode": a.mode, "job_id": job_id, "job_dir": str(job), "critic_model": critic_model,
           "agent_model": agent_model, "eval_split": str(a.eval_split), "minutes": round((time.time() - started) / 60, 1),
           **score(job, split, a.pass_at_k)}
    a.history.parent.mkdir(parents=True, exist_ok=True)
    with a.history.open("a") as fh:
        fh.write(json.dumps(row) + "\n")
    o = row["overall"]
    print(json.dumps(row["cells"], indent=1))
    if a.pass_at_k > 1:
        print(f"\nstep {a.step} [{a.mode}]: pass@{a.pass_at_k} attempts {o['attempts']} (mean {o['passed']}/{o['graded']}, any {o['any']}) -> {a.history}")
        return
    print(f"\nstep {a.step} [{a.mode}]: {o['passed']}/{o['graded']} passed "
          f"(IID {o['iid']['passed']}/{o['iid']['graded']}, OOD {o['ood']['passed']}/{o['ood']['graded']}) "
          f"| the agent alone passes {o['baseline_passed']}/20 of these -> {a.history}")


if __name__ == "__main__":
    main()
