"""pass@k over a benchmark sweep: k attempts per task, graded by the benchmark's own pass gate.

Per task: ``n`` attempts, ``c`` passes, ``any`` (≥1 pass). Job level: mean pass@1, the unbiased pass@k estimator
and the fraction of tasks with at least one passing attempt. ``pass_at_k.json`` records the per-attempt outcomes.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence


def pass_at_k_estimate(n: int, c: int, k: int) -> float:
    """Unbiased pass@k, ``1 - C(n-c, k)/C(n, k)``, for a task with ``c`` passes out of ``n`` attempts
    (``k`` is clipped to ``n``)."""
    if n <= 0 or k <= 0:
        return 0.0
    k = min(k, n)
    if n - c < k:
        return 1.0
    prod = 1.0
    for i in range(k):
        prod *= (n - c - i) / (n - i)
    return 1.0 - prod


def k_grid(k_max: int) -> list[int]:
    """The k values reported: 1, the powers of two up to ``k_max``, and ``k_max``."""
    ks = {1, k_max}
    k = 2
    while k <= k_max:
        ks.add(k); k *= 2
    return sorted(ks)


@dataclass
class TaskAttempts:
    """The recorded attempts of one task."""
    task: str
    outcomes: list[dict[str, Any]] = field(default_factory=list)   # {"trial": name, "passed": bool, "reward": float|None, "reason": str|None}

    @property
    def n(self) -> int:
        return len(self.outcomes)

    @property
    def c(self) -> int:
        return sum(1 for o in self.outcomes if o["passed"])

    @property
    def any(self) -> bool:
        return self.c > 0


@dataclass
class PassKReport:
    """The per-task attempts of a job and the pass@k numbers over them (``k`` = attempts wanted per task)."""
    k: int
    tasks: dict[str, TaskAttempts]

    @property
    def n_tasks(self) -> int:
        return len(self.tasks)

    @property
    def any_at_k(self) -> float:
        """Fraction of tasks with at least one passing attempt."""
        return (sum(t.any for t in self.tasks.values()) / self.n_tasks) if self.tasks else 0.0

    def pass_at(self, k: int) -> float:
        """Mean over tasks of the unbiased pass@``k`` estimate."""
        if not self.tasks:
            return 0.0
        return sum(pass_at_k_estimate(t.n, t.c, k) for t in self.tasks.values()) / self.n_tasks

    def summary(self) -> dict[str, Any]:
        """The job-level numbers (the ``summary`` block of pass_at_k.json)."""
        return {
            "k": self.k, "n_tasks": self.n_tasks,
            "n_attempts": sum(t.n for t in self.tasks.values()),
            "tasks_solved_any": sum(t.any for t in self.tasks.values()),
            "any_at_k": round(self.any_at_k, 4),
            "pass_at_k": {str(k): round(self.pass_at(k), 4) for k in k_grid(self.k)},
            "mean_pass_at_1": round(sum((t.c / t.n) if t.n else 0.0 for t in self.tasks.values()) / self.n_tasks, 4) if self.tasks else 0.0,
        }

    def to_dict(self) -> dict[str, Any]:
        return {"summary": self.summary(),
                "tasks": {name: {"n": t.n, "c": t.c, "any": t.any, "attempts": t.outcomes} for name, t in sorted(self.tasks.items())}}

    def write(self, path: Path) -> None:
        """Write the report as JSON to ``path``."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=1), encoding="utf-8")

    def render(self, label: str) -> str:
        """The printed report: one line per task and the job-level numbers."""
        s = self.summary()
        lines = [f"\n=== {label}: pass@{self.k} over {s['n_tasks']} task(s) × {self.k} attempt(s) ==="]
        for name, t in sorted(self.tasks.items()):
            marks = "".join("✓" if o["passed"] else "✗" for o in t.outcomes)
            lines.append(f"  [{'PASS' if t.any else 'FAIL'}] {name:44s} {t.c}/{t.n}  {marks}")
        lines.append(f"\ntasks solved (any of {self.k}): {s['tasks_solved_any']} / {s['n_tasks']}  "
                     f"| mean pass@1 = {s['mean_pass_at_1']:.3f}  | " + "  ".join(f"pass@{k} = {v:.3f}" for k, v in s["pass_at_k"].items()))
        return "\n".join(lines)


def aggregate(trial_results: Sequence[Any], *, k: int, task_key: Callable[[Any], str],
              trial_passes: Callable[[Any], tuple[bool, str | None]], reward_value: Callable[[Any], float | None],
              expected_tasks: Sequence[str] | None = None) -> PassKReport:
    """Group ``trial_results`` by ``task_key`` and grade each with ``trial_passes`` (→ ``(passed, reason)``).
    ``expected_tasks`` are listed even when they have no attempt yet."""
    tasks: dict[str, TaskAttempts] = {t: TaskAttempts(t) for t in (expected_tasks or [])}
    for tr in trial_results:
        name = task_key(tr)
        ok, reason = trial_passes(tr)
        tasks.setdefault(name, TaskAttempts(name)).outcomes.append(
            {"trial": getattr(tr, "trial_name", None), "passed": bool(ok), "reward": reward_value(tr), "reason": reason})
    return PassKReport(k=k, tasks=tasks)
