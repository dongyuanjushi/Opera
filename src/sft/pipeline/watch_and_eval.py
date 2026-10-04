#!/usr/bin/env python3
"""Watch a training run and evaluate each new checkpoint: serve it, run eval_checkpoint.py per --modes, stop the server.

One GPU set is reused for every checkpoint; steps already in --history (for this --job-prefix) are skipped.

    python src/sft/pipeline/watch_and_eval.py --run-dir src/sft/runs/<run> --gpus 0,1 [-- <eval_checkpoint.py flags>]
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

OPERA_ROOT = Path(__file__).resolve().parents[3]
STEP = re.compile(r"checkpoint-(\d+)$")


def checkpoints(run_dir: Path) -> list[tuple[int, Path]]:
    """Sorted (step, path) of finished checkpoints in run_dir or one level below (ms-swift's v<n>-<timestamp>/)."""
    out = []
    for p in sorted([*run_dir.glob("checkpoint-*"), *run_dir.glob("*/checkpoint-*")]):
        m = STEP.search(p.name)
        if m and (p / "config.json").exists() and any(p.glob("*.safetensors")):
            out.append((int(m.group(1)), p))
    return sorted(out)


def wait_healthy(url: str, timeout_s: float) -> bool:
    """Poll <url>/models (with VLLM_API_KEY) until the server answers or timeout_s passes; True if it came up."""
    import os
    key = os.environ.get("VLLM_API_KEY", "dummy")
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            req = urllib.request.Request(url + "/models", headers={"Authorization": f"Bearer {key}"})
            urllib.request.urlopen(req, timeout=5)
            return True
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):        # up, but wants a different key
                return True
            time.sleep(10)
        except Exception:
            time.sleep(10)
    return False


def served_root(url: str) -> str | None:
    """Path the endpoint is serving (the first model's ``root``), or None if unreachable."""
    import os
    key = os.environ.get("VLLM_API_KEY", "dummy")
    try:
        req = urllib.request.Request(url + "/models", headers={"Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return ((json.load(r).get("data") or [{}])[0]).get("root")
    except Exception:
        return None


def free_port(port: int) -> None:
    """Kill any vLLM process (and its group) still listening on `port`, e.g. an orphan of an earlier watcher."""
    import os
    import signal
    out = subprocess.run(["ss", "-ltnp"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if f":{port} " not in line or "vllm" not in line:
            continue
        for pid in {int(x) for x in re.findall(r"pid=(\d+)", line)}:
            print(f"[port] killing leftover vLLM pid {pid} on :{port}", flush=True)
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(os.getpgid(pid), sig)
                except Exception:
                    try:
                        os.kill(pid, sig)
                    except Exception:
                        pass
                for _ in range(30):
                    try:
                        os.kill(pid, 0); time.sleep(2)
                    except Exception:
                        break
                else:
                    continue
                break


def stop_server(server: subprocess.Popen) -> None:
    """Stop the server's process group (serve_checkpoint.sh and its vLLM): SIGTERM, then SIGKILL."""
    import os
    import signal
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(os.getpgid(server.pid), sig)
        except Exception:
            try:
                server.send_signal(sig)
            except Exception:
                return
        try:
            server.wait(timeout=120)
            return
        except subprocess.TimeoutExpired:
            continue


# Trainer state files not needed to serve a checkpoint (serving only needs the HF model + tokenizer).
NON_HF = ("global_step*", "zero_pp_rank_*", "*optim_states*", "optimizer*", "scheduler*", "rng_state*",
          "latest", "trainer_state.json.tmp", "*.pth")


def strip_non_hf(ckpt: Path) -> int:
    """Delete the NON_HF files in a checkpoint directory; returns the bytes freed."""
    freed = 0
    for pattern in NON_HF:
        for path in ckpt.glob(pattern):
            try:
                size = sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) if path.is_dir() else path.stat().st_size
                shutil.rmtree(path, ignore_errors=True) if path.is_dir() else path.unlink(missing_ok=True)
                freed += size
            except Exception:
                pass
    if freed:
        print(f"[strip] {ckpt.name}: freed {freed / 2**30:.1f} GiB of non-HF checkpoint files", flush=True)
    return freed


def scores(history: Path, job_prefix: str | None = None) -> dict[int, float]:
    """step -> tasks passed summed over evaluated modes (later rows win); job_prefix keeps only that run's rows."""
    per: dict[int, dict[str, float]] = {}
    if not history.exists():
        return {}
    for line in history.read_text().splitlines():
        try:
            row = json.loads(line)
            step = int(row["step"])
        except Exception:
            continue
        if job_prefix and not str(row.get("job_id", "")).startswith(job_prefix + "-"):
            continue
        per.setdefault(step, {})[row.get("mode", "?")] = row["overall"]["passed"]
    return {step: sum(modes.values()) for step, modes in per.items()}


def prune(run_dir: Path, history: Path, keep: int, protect: set[int], job_prefix: str | None = None) -> None:
    """Delete evaluated checkpoints outside the `keep` best scores; unscored steps and `protect` are kept."""
    ranked = sorted(scores(history, job_prefix).items(), key=lambda kv: (-kv[1], -kv[0]))
    survivors = {step for step, _ in ranked[:keep]} | protect
    for step, path in checkpoints(run_dir):
        if step in survivors or step not in dict(ranked):
            continue
        print(f"[prune] removing checkpoint-{step} (kept: {sorted(survivors)})", flush=True)
        shutil.rmtree(path, ignore_errors=True)


def main() -> None:
    """Poll --run-dir every --poll-s seconds and evaluate pending checkpoints (once with --once)."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-dir", type=Path, required=True, help="the trainer's --output_dir")
    ap.add_argument("--gpus", default="0,1,2,3", help="GPUs for the checkpoint server")
    ap.add_argument("--port", type=int, default=30900, help="checkpoint server port")
    ap.add_argument("--host-ip", default=None, help="IP the containers reach (default: this host's routable IP)")
    ap.add_argument("--serve-timeout-s", type=float, default=1800, help="seconds to wait for the server to come up")
    ap.add_argument("--skip-stale", action="store_true", help="when several checkpoints are waiting, evaluate only the newest")
    ap.add_argument("--newest-first", action="store_true", help="when several checkpoints are waiting, evaluate the newest one first (all are still scored)")
    ap.add_argument("--modes", default="agent,self-critic",
                    help="evaluation modes per checkpoint (eval_checkpoint.py --mode), comma separated")
    ap.add_argument("--keep-best", type=int, default=0,
                    help="after each evaluation keep only the N best checkpoints on disk (0 = keep everything); "
                         "the score is the total tasks passed summed over --modes")
    ap.add_argument("--history", type=Path, default=OPERA_ROOT / "src/sft/data/eval-history.jsonl",
                    help="eval_checkpoint.py history JSONL (read to skip and rank steps)")
    ap.add_argument("--poll-s", type=float, default=120, help="seconds between scans of --run-dir")
    ap.add_argument("--once", action="store_true", help="evaluate what exists now and exit")
    ap.add_argument("extra", nargs="*", help="extra flags for eval_checkpoint.py")
    a = ap.parse_args()

    if not a.host_ip:
        sys.path.insert(0, str(OPERA_ROOT / "src"))
        from critics.proxy import routable_ip
        a.host_ip = routable_ip()
    base_url = f"http://{a.host_ip}:{a.port}/v1"
    job_prefix = a.extra[a.extra.index("--job-prefix") + 1] if "--job-prefix" in a.extra else None
    done: set[int] = set(scores(a.history, job_prefix))

    while True:
        done |= set(scores(a.history, job_prefix))
        pending = [(s, p) for s, p in checkpoints(a.run_dir) if s not in done]
        if a.skip_stale and len(pending) > 1:
            for step, path in pending[:-1]:
                done.add(step)
                if a.keep_best:
                    print(f"[skip] checkpoint-{step} (newer one available); removing it", flush=True)
                    shutil.rmtree(path, ignore_errors=True)
            pending = pending[-1:]
        if a.newest_first:
            pending = pending[::-1]
        for step, ckpt in pending:
            print(f"\n=== checkpoint-{step}: serving {ckpt}", flush=True)
            strip_non_hf(ckpt)
            free_port(a.port)
            server = subprocess.Popen(["bash", str(OPERA_ROOT / "src/sft/pipeline/serve_checkpoint.sh"), str(ckpt), str(a.port)],
                                      cwd=OPERA_ROOT, env={**__import__("os").environ, "GPUS": a.gpus}, start_new_session=True)
            try:
                if not wait_healthy(base_url, a.serve_timeout_s):
                    print(f"checkpoint-{step}: endpoint never came up; skipping", flush=True)
                    continue
                root = served_root(base_url) or ""
                if not root.rstrip("/").endswith(ckpt.name) or Path(root).name != ckpt.name:
                    print(f"checkpoint-{step}: the endpoint serves {root!r}, not this checkpoint; skipping", flush=True)
                    continue
                env = {**__import__("os").environ, "SFT_CRITIC_BASE_URL": base_url}
                for mode in [m.strip() for m in a.modes.split(",") if m.strip()]:
                    subprocess.run([str(OPERA_ROOT / ".venv/bin/python"), str(OPERA_ROOT / "src/sft/pipeline/eval_checkpoint.py"),
                                    "--step", str(step), "--mode", mode, *a.extra], cwd=OPERA_ROOT, env=env, check=False)
            finally:
                stop_server(server)
                done.add(step)
            if a.keep_best:
                prune(a.run_dir, a.history, a.keep_best, protect={step}, job_prefix=job_prefix)
        if a.once:
            return
        time.sleep(a.poll_s)


if __name__ == "__main__":
    main()
