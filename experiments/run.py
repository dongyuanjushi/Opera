#!/usr/bin/env python3
"""Launch one paper experiment (policy x critic, ablation or baseline strategy) via src/launch.py, using configs/experiments.yaml.

    python experiments/run.py --bench tb21 --policy qwen38-27b --critic gpt-5.6
    python experiments/run.py --bench tb21 --policy qwen38-27b --critic gpt-5.6 --ablation no-audit --dry-run
    python experiments/run.py --bench swebench-pro --policy qwen38-27b --critic gpt-5.6 --strategy swe_prm

Endpoints and keys come from .env; --agent-url / --critic-url override a preset's <PRESET>_BASE_URL for this run.
"""
from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from pathlib import Path

import yaml

OPERA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPERA / "src"))
from opera_env import load_dotenv  # noqa: E402

CONFIG = OPERA / "configs" / "experiments.yaml"
PY = OPERA / ".venv" / "bin" / "python"


def effort_for(cfg: dict, preset: str, bench: str) -> str | None:
    """The agent's reasoning effort for *bench* from experiments.yaml (per-bench map or a single value; None if unset)."""
    a = (cfg.get("agents") or {}).get(preset) or {}
    e = a.get("reasoning_effort")
    if isinstance(e, dict):
        return e.get(bench) or e.get("default")
    return e


def main() -> None:
    """Build the src/launch.py command line, print it and run it (with --dry-run appended if requested)."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bench", required=True, choices=("tb21", "swebench-pro", "deepswe"))
    ap.add_argument("--policy", required=True, help="agent preset in configs/policy-models.yaml (e.g. qwen35-9b | muse-glimmer-30b | qwen38-27b | deepseek-v4-flash)")
    ap.add_argument("--critic", default="gpt-5.6", help="critic: a key of experiments.yaml critics (gpt-5.6 | claude-opus-4-8 | self) or none")
    ap.add_argument("--ablation", default="none", help="none | edit-only | non-edit | no-audit (aliases: edit | noedit | noaudit)")
    ap.add_argument("--strategy", default=None, help="baseline critic strategy instead of operator: swe_prm | swe_search | llm_verifier | agentic_rubrics")
    ap.add_argument("--pass-at-k", type=int, default=1, help="attempts per task")
    ap.add_argument("--workers", type=int, default=None, help="concurrent trials (default: experiments.yaml, else 12)")
    ap.add_argument("--max-turns", type=int, default=None, help="override the benchmark's turn cap (-1 = none)")
    ap.add_argument("--agent-effort", default=None, help="override the agent's reasoning effort")
    ap.add_argument("--critic-effort", default=None, help="override the critic's reasoning effort")
    ap.add_argument("--agent-url", default=None, help="agent base URL (sets <PRESET>_BASE_URL for this run)")
    ap.add_argument("--critic-url", default=None, help="critic base URL for a self-hosted critic preset")
    ap.add_argument("--timeout-sec", type=float, default=None, help="per-task agent timeout in seconds (default: the task's own)")
    ap.add_argument("--selection", default=None, help="task selection (default: experiments.yaml)")
    ap.add_argument("--harness", default=None, help="agent harness (default: experiments.yaml)")
    ap.add_argument("--job-id", default=None, help="override the job id built by src/launch.py")
    ap.add_argument("--tag", default=None, help="extra suffix appended to --critic-tag")
    ap.add_argument("--resume", action="store_true", help="pass --mode resume")
    ap.add_argument("--no-record", action="store_true", help="do not record critic prompts / agent requests")
    ap.add_argument("--dry-run", action="store_true", help="print the launch line and src/launch.py's dry run; start nothing")
    ap.add_argument("extra", nargs="*", help="extra flags passed to src/launch.py (after --)")
    a = ap.parse_args()

    load_dotenv()
    cfg = yaml.safe_load(CONFIG.read_text())
    bench = cfg["benchmarks"][a.bench]
    harness = a.harness or bench["harness"]
    cmd = [str(PY), "src/launch.py", a.bench, "--harness", harness, "--agent-model", a.policy]
    if a.selection or bench.get("selection"):
        cmd += ["--selection", a.selection or bench["selection"]]
    eff = a.agent_effort or effort_for(cfg, a.policy, a.bench)
    if eff and eff != "default":
        cmd += ["--reasoning-effort", str(eff)]
    cmd += ["--pass-at-k", str(a.pass_at_k), "--max-workers", str(a.workers or bench.get("max_workers", 12))]
    mt = a.max_turns if a.max_turns is not None else bench.get("max_turns")
    if mt is not None:
        cmd += ["--max-turns", str(mt)]
    for flag in bench.get("extra_flags") or []:
        cmd += shlex.split(flag)
    if a.timeout_sec:
        cmd += ["--agent-timeout-sec", str(a.timeout_sec)]

    env = dict(os.environ)
    preset_env = a.policy.upper().replace("-", "_").replace(".", "_") + "_BASE_URL"
    if a.agent_url:
        env[preset_env] = a.agent_url
    ABL = {"edit": "edit-only", "noedit": "non-edit", "noaudit": "no-audit"}
    if a.critic != "none":
        crit = cfg["critics"][a.critic]
        critic_model = a.policy if a.critic == "self" else crit.get("model", a.critic)
        cmd += ["--critic-model", critic_model]
        ceff = a.critic_effort or crit.get("reasoning_effort")
        if ceff == "agent":
            ceff = eff
        if ceff and ceff != "default":
            cmd += ["--critic-reasoning-effort", str(ceff)]
        if a.critic_url:
            env[critic_model.upper().replace("-", "_").replace(".", "_") + "_BASE_URL"] = a.critic_url
        if not a.no_record:
            cmd += ["--critic-record-prompts", "--critic-record-requests"]
        if a.strategy:
            cmd += ["--critic-strategy", a.strategy]
        pol = cfg.get("critic_policy") or {}
        policy = dict(pol.get("overrides") or {}) if not a.strategy else {}
        tag = pol.get("tag") if not a.strategy else None
        if a.ablation != "none":
            abl = cfg["ablations"][ABL.get(a.ablation, a.ablation)]
            policy.update(abl.get("overrides") or {})
            tag = abl.get("tag") or f"{tag}-{a.ablation}"
        for k, v in policy.items():
            cmd += ["--critic-policy", f"{k}={str(v).lower() if isinstance(v, bool) else v}"]
        if a.tag:
            tag = f"{tag}-{a.tag}" if tag else a.tag
        if tag:
            cmd += ["--critic-tag", tag]
    elif a.ablation != "none" or a.strategy:
        sys.exit("--ablation / --strategy need a critic")
    if a.job_id:
        cmd += ["--job-id", a.job_id]
    if a.resume:
        cmd += ["--mode", "resume"]
    cmd += a.extra
    env.setdefault("VLLM_API_KEY", "dummy")
    print(" ".join(shlex.quote(c) for c in cmd), flush=True)
    if a.dry_run:
        cmd.append("--dry-run")
    os.chdir(OPERA)
    sys.exit(subprocess.call(cmd, env=env))


if __name__ == "__main__":
    main()
