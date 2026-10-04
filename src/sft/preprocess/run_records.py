"""Readers for recorded runs shared by the SFT scripts: per-task results and the critic's decision logs."""
from __future__ import annotations

import json
from pathlib import Path


def task_results(job: Path) -> dict[str, tuple[int, str]]:
    """Map task id -> (passed 0/1, trial dir name) for one job."""
    out: dict[str, tuple[int, str]] = {}
    for f in job.glob("*/result.json"):
        if f.parent.name == "critic-logs":
            continue
        try:
            r = json.loads(f.read_text())
        except Exception:
            continue
        if r.get("task_name"):
            reward = ((r.get("verifier_result") or {}).get("rewards") or {}).get("reward") or 0
            out[r["task_name"].split("/")[-1]] = (int(reward > 0), f.parent.name)
    return out


def rows_of(path: Path) -> list[dict]:
    """Rows of a decisions.jsonl where a review was called without error and its prompt was recorded."""
    rows = []
    for line in path.open():
        try:
            x = json.loads(line)
        except Exception:
            continue
        if x.get("review_called") and not x.get("error") and x.get("prompt"):
            rows.append(x)
    return rows


def repair_windows(job: Path, trial: str, n_turns: int) -> set[int]:
    """1-based turns from each delivered note up to the audited close that resolved it (the repair episode)."""
    d = job / "critic-logs" / trial / "decisions.jsonl"
    if not d.exists():
        return set()
    rows = rows_of(d)
    notes = [x["turn_index"] for x in rows
             if (x.get("delivery") or {}).get("mode") in ("intervention", "update") and x.get("turn_index")]
    closes = sorted(x["turn_index"] for x in rows
                    if (x.get("audit") or {}).get("finding_closed") and x.get("turn_index"))
    out: set[int] = set()
    for t0 in notes:
        end = next((c for c in closes if c >= t0), None)
        if end:
            out |= {k for k in range(t0, min(end, n_turns) + 1)}
    return out
