#!/usr/bin/env python3
"""The SFT launcher: build a dataset, train with ms-swift, serve / evaluate checkpoints, measure policy drift.

    python src/sft/launch.py data  --arm A --name <dataset> --tasks <task list> --source-job <run dir> [...] --exclude-tasks <eval split>
    python src/sft/launch.py train --data <dataset> --run <run> --gpus 0,1,2,3,4,5,6,7
    python src/sft/launch.py watch --run <run> --job-prefix <prefix> --split <eval split> --selection <selection> --gpus 0,1
    python src/sft/launch.py eval  --run <run> --step <step> --job-prefix <prefix> --split <eval split> --selection <selection>
    python src/sft/launch.py drift <label>=<checkpoint dir> [...] --base --probe <probe.jsonl>

Paths: datasets in src/sft/data/<dataset>/, runs in src/sft/runs/<run>/, logs in experiments/logs/. Hosts, model paths and
venvs come from .env (SFT_BASE_MODEL, VLLM_VENV). `--dry-run` prints the commands without running them.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

OPERA = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(OPERA / "src"))
from opera_env import load_dotenv  # noqa: E402

SFT = OPERA / "src" / "sft"
DATA = SFT / "data"
RUNS = SFT / "runs"
LOGS = OPERA / "experiments" / "logs"
PY = OPERA / ".venv" / "bin" / "python"
SFT_PY = OPERA / ".venv" / "sft" / "bin" / "python"

# export flags per arm; every arm keeps passing trajectories only
ARMS = {
    "A": ["--select", "passing", "--think-from", "reasoning", "--note-into-think"],   # own runs under a critic, note in the thought
    "B": ["--select", "passing", "--think-from", "reasoning"],                        # another model's runs, its reasoning as thought
    "C": ["--select", "passing", "--think-from", "message"],                          # runs without reasoning, visible message as thought
}


def show(cmd: list, env: dict | None = None, log: Path | None = None) -> None:
    """Print a command (with the env overrides it gets and where its output goes)."""
    prefix = " ".join(f"{k}={shlex.quote(str(v))}" for k, v in (env or {}).items())
    print("+", (prefix + " " if prefix else "") + shlex.join(map(str, cmd)) + (f" > {log}" if log else ""), flush=True)


def run(cmd: list, a, env: dict | None = None) -> None:
    """Run a command in the foreground (skipped under --dry-run); exit on failure."""
    show(cmd, env)
    if not a.dry_run:
        subprocess.run(list(map(str, cmd)), cwd=OPERA, env={**os.environ, **(env or {})}, check=True)


def spawn(cmd: list, a, log: Path, env: dict | None = None) -> subprocess.Popen | None:
    """Start a command detached in its own process group, output to *log* (None under --dry-run)."""
    show(cmd, env, log)
    if a.dry_run:
        return None
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "ab") as fh:
        return subprocess.Popen(list(map(str, cmd)), cwd=OPERA, env={**os.environ, **(env or {})}, stdout=fh,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)


def n_gpus(gpus: str) -> int:
    """Number of devices in a comma-separated CUDA_VISIBLE_DEVICES string."""
    return len([g for g in gpus.split(",") if g.strip()])


def host_ip() -> str:
    """This host's first routable IP (what the task containers dial to reach a served checkpoint)."""
    return subprocess.run(["hostname", "-I"], capture_output=True, text=True).stdout.split()[0]


def task_list_json(path: Path, name: str) -> Path:
    """The task list as a JSON list: a JSON file is used as is, a plain one-id-per-line file is converted."""
    try:
        json.loads(path.read_text())
        return path
    except ValueError:
        ids = [l.strip() for l in path.read_text().splitlines() if l.strip() and not l.startswith("#")]
        out = DATA / f"{name}-tasks.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(ids, indent=1) + "\n")
        return out


def checkpoint(run_name: str, step: int) -> Path:
    """src/sft/runs/<run>/<version>/checkpoint-<step>."""
    hits = sorted(RUNS.glob(f"{run_name}/*/checkpoint-{step}"))
    if not hits:
        raise SystemExit(f"checkpoint {step} of {run_name} not found under {RUNS / run_name}")
    return hits[0]


def cmd_data(a) -> None:
    """Export the passing trajectories of the source runs, then keep the per-turn samples of <= --max-tokens."""
    tasks = task_list_json(a.tasks, a.name) if not a.dry_run else a.tasks
    src = DATA / f"{a.name}-src"
    srcs = [x for j in a.source_job for x in ("--source-job", j)]
    run([PY, SFT / "preprocess/export_policy_sft.py", *srcs, *ARMS[a.arm], "--tasks-file", tasks,
         "--exclude-tasks-file", a.exclude_tasks, "--max-tokens", 100000000, "--out", src], a)
    run([PY, SFT / "preprocess/build_cleaned_perturn_sft.py", src, "--all-trajectories", "--max-tokens", a.max_tokens,
         "--out", DATA / a.name], a)
    if not a.dry_run:
        m = json.loads((DATA / a.name / "manifest.json").read_text())
        print({k: m[k] for k in ("samples", "tasks", "trajectories", "supervised_tokens_est") if k in m})


def cmd_train(a) -> None:
    """Full-parameter SFT through src/sft/pipeline/train_critic.sh (ms-swift); detached unless --foreground."""
    data = Path(a.data)
    data = data if data.suffix == ".jsonl" else (data if data.is_dir() else DATA / a.data) / "train.jsonl"
    model = a.base_model or os.environ.get("SFT_BASE_MODEL")
    if not model:
        raise SystemExit("set SFT_BASE_MODEL in .env or pass --base-model")
    env = {"CUDA_VISIBLE_DEVICES": a.gpus, "MASTER_PORT": a.master_port, "MODEL": model, "DATA": data, "OUT": RUNS / a.run,
           "NPROC": n_gpus(a.gpus), "MAX_LENGTH": a.max_length, "SAVE_STEPS": a.save_steps, "EPOCHS": a.epochs, "BATCH": 1,
           "ACCUM": a.accum, "LR": a.lr, "TUNER_TYPE": a.tuner, "TRUNCATION": "delete", "LOSS_SCALE": "default"}
    env = {k: str(v) for k, v in env.items()}
    cmd = ["bash", SFT / "pipeline/train_critic.sh"]
    if a.foreground:
        return run(cmd, a, env)
    p = spawn(cmd, a, LOGS / f"train_{a.run}.log", env)
    if p:
        print(f"training {a.run} on GPUs {a.gpus} (pid {p.pid}) -> {LOGS / f'train_{a.run}.log'}")


def serve_env(a) -> dict:
    """Env for serve_checkpoint.sh."""
    return {"GPUS": a.gpus, "MAX_LEN": str(a.max_len)}


def cmd_serve(a) -> None:
    """Serve one checkpoint with vLLM (foreground) as the `sft-critic` preset."""
    run(["bash", SFT / "pipeline/serve_checkpoint.sh", a.checkpoint, a.port], a, serve_env(a))


def eval_flags(a) -> list:
    """eval_checkpoint.py flags shared by `eval` and `watch`."""
    return ["--max-workers", a.max_workers, "--eval-split", a.split, "--selection", a.selection, "--pass-at-k", a.k,
            "--job-prefix", a.job_prefix]


def cmd_eval(a) -> None:
    """Serve one checkpoint, run it as the agent on the eval split (resumable), then stop the server."""
    ck = checkpoint(a.run, a.step) if not a.dry_run else RUNS / a.run / "*" / f"checkpoint-{a.step}"
    server = spawn(["bash", SFT / "pipeline/serve_checkpoint.sh", ck, a.port], a,
                   LOGS / f"serve_{a.job_prefix}_{a.step}.log", serve_env(a))
    try:
        if server:
            url = f"http://127.0.0.1:{a.port}/v1/models"
            for _ in range(90):
                try:
                    urllib.request.urlopen(url, timeout=5)
                    break
                except OSError:
                    if server.poll() is not None:
                        raise SystemExit(f"server exited, see {LOGS / f'serve_{a.job_prefix}_{a.step}.log'}")
                    time.sleep(20)
        base = f"http://{host_ip() if not a.dry_run else '<host ip>'}:{a.port}/v1"
        run([PY, SFT / "pipeline/eval_checkpoint.py", "--step", a.step, "--mode", "agent", *eval_flags(a), "--",
             "--mode", "resume"], a, {"SFT_CRITIC_BASE_URL": base})
    finally:
        if server and server.poll() is None:
            os.killpg(server.pid, signal.SIGTERM)
            server.wait(timeout=120)


def cmd_watch(a) -> None:
    """Evaluate every new checkpoint of a run as it appears (watch_and_eval.py); detached unless --foreground."""
    cmd = [PY, SFT / "pipeline/watch_and_eval.py", "--run-dir", RUNS / a.run, "--gpus", a.gpus, "--port", a.port,
           "--modes", "agent", "--poll-s", a.poll_s, "--newest-first", "--", *eval_flags(a)]
    env = {"MAX_LEN": str(a.max_len)}
    if a.foreground:
        return run(cmd, a, env)
    p = spawn(cmd, a, LOGS / f"watch_{a.run}.log", env)
    if p:
        print(f"watcher for {a.run} on GPUs {a.gpus} (pid {p.pid}) -> {LOGS / f'watch_{a.run}.log'}")


def cmd_drift(a) -> None:
    """KL / NLL / entropy vs the base policy on held-out turns, one GPU per checkpoint (results/sft/drift/<label>.json)."""
    out = OPERA / "results" / "sft" / "drift"
    if not a.probe.exists():
        if not (a.probe_source_job and a.probe_train_tasks and a.probe_exclude_tasks):
            raise SystemExit(f"{a.probe} does not exist: pass --probe-source-job, --probe-train-tasks and --probe-exclude-tasks to build it")
        run([SFT_PY, SFT / "scripts/policy_drift.py", "--build", "--source-job", a.probe_source_job, "--train-tasks",
             a.probe_train_tasks, "--exclude-tasks", a.probe_exclude_tasks, "--out", a.probe], a)
    jobs = ([("base", None)] if a.base else []) + [tuple(s.split("=", 1)) for s in a.checkpoints]
    gpus = [g for g in a.gpus.split(",") if g.strip()]
    if len(jobs) > len(gpus):
        raise SystemExit(f"{len(jobs)} probes need {len(jobs)} GPUs, --gpus has {len(gpus)}")
    procs = []
    for (label, ckpt), gpu in zip(jobs, gpus):
        cmd = [SFT_PY, SFT / "scripts/policy_drift.py", "--probe", a.probe, "--label", label,
               *(["--ckpt", ckpt] if ckpt else []), "--out", out / f"{label}.json"]
        if not a.dry_run:
            out.mkdir(parents=True, exist_ok=True)
        procs.append(spawn(cmd, a, LOGS / f"drift_{label}.log", {"CUDA_VISIBLE_DEVICES": gpu}))
    for p in procs:
        if p:
            p.wait()
    print(f"done: {out}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dry-run", action="store_true", help="print the commands only")
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("data", parents=[common], help="build a per-turn SFT dataset from recorded runs")
    d.add_argument("--arm", choices=sorted(ARMS), required=True,
                   help="A = the policy's own runs under a critic (note in the thought); B = another model's runs; "
                        "C = runs without a reasoning trace")
    d.add_argument("--name", required=True, help="dataset name: src/sft/data/<name> (and <name>-src, the raw export)")
    d.add_argument("--tasks", type=Path, required=True, help="task ids to export: a JSON list or one id per line")
    d.add_argument("--source-job", action="append", required=True, help="a recorded job dir (repeatable)")
    d.add_argument("--exclude-tasks", type=Path, required=True, help="eval split JSON whose tasks never enter training")
    d.add_argument("--max-tokens", type=int, default=128000, help="drop samples whose context exceeds this (est. tokens)")
    d.set_defaults(fn=cmd_data)

    t = sub.add_parser("train", parents=[common], help="full-parameter SFT with ms-swift")
    t.add_argument("--data", required=True, help="dataset name under src/sft/data, a dataset dir, or a train.jsonl")
    t.add_argument("--run", required=True, help="run name: checkpoints in src/sft/runs/<run>/")
    t.add_argument("--base-model", default=None, help="weights to fine-tune, local path or hub id (default: $SFT_BASE_MODEL)")
    t.add_argument("--gpus", default="0,1,2,3", help="CUDA devices; NPROC = their count")
    t.add_argument("--accum", type=int, default=8, help="gradient accumulation; effective batch = #GPUs x accum")
    t.add_argument("--epochs", type=int, default=3)
    t.add_argument("--lr", default="2e-6")
    t.add_argument("--max-length", type=int, default=143360, help="longer examples are dropped, never truncated")
    t.add_argument("--save-steps", type=int, default=100, help="checkpoint interval in optimizer steps")
    t.add_argument("--tuner", choices=("full", "lora"), default="full")
    t.add_argument("--master-port", type=int, default=29815, help="torch.distributed port (unique per concurrent run)")
    t.add_argument("--foreground", action="store_true", help="block instead of detaching")
    t.set_defaults(fn=cmd_train)

    def serving(p, port: int, gpus: str = "0,1") -> None:
        p.add_argument("--gpus", default=gpus, help="GPUs of the vLLM server (tensor parallel over all)")
        p.add_argument("--port", type=int, default=port)
        p.add_argument("--max-len", type=int, default=262144, help="vLLM --max-model-len")

    def evaluating(p) -> None:
        p.add_argument("--run", required=True, help="run name under src/sft/runs")
        p.add_argument("--job-prefix", required=True, help="prefix of the eval job ids (one per step)")
        p.add_argument("--k", type=int, default=1, help="attempts per task (pass@k)")
        p.add_argument("--split", type=Path, required=True, help="eval split JSON ('tasks' and 'cells')")
        p.add_argument("--selection", required=True, help="swebench-pro selection the split's tasks belong to")
        p.add_argument("--max-workers", type=int, default=12, help="concurrent trials")

    s = sub.add_parser("serve", parents=[common], help="serve one checkpoint with vLLM (foreground)")
    s.add_argument("checkpoint", type=Path)
    serving(s, 30900, "0,1,2,3")
    s.set_defaults(fn=cmd_serve)

    e = sub.add_parser("eval", parents=[common], help="serve + evaluate one checkpoint, then stop the server")
    evaluating(e)
    e.add_argument("--step", type=int, required=True, help="checkpoint step")
    serving(e, 30903)
    e.set_defaults(fn=cmd_eval)

    w = sub.add_parser("watch", parents=[common], help="evaluate each new checkpoint of a run as it is saved")
    evaluating(w)
    serving(w, 30900)
    w.add_argument("--poll-s", type=int, default=120, help="seconds between scans of the run dir")
    w.add_argument("--foreground", action="store_true", help="block instead of detaching")
    w.set_defaults(fn=cmd_watch)

    r = sub.add_parser("drift", parents=[common], help="policy drift of checkpoints vs the base model")
    r.add_argument("checkpoints", nargs="*", metavar="LABEL=CKPT", help="checkpoint dirs to probe, one GPU each")
    r.add_argument("--base", action="store_true", help="also probe the base model (label 'base')")
    r.add_argument("--probe", type=Path, required=True, help="probe JSONL (built if missing, from the --probe-* inputs)")
    r.add_argument("--probe-source-job", type=Path, default=None, help="to build the probe: a recorded run of the base model")
    r.add_argument("--probe-train-tasks", type=Path, default=None, help="to build the probe: training task ids to skip")
    r.add_argument("--probe-exclude-tasks", type=Path, default=None, help="to build the probe: eval split JSON to skip")
    r.add_argument("--gpus", default="0,1,2,3,4,5,6,7", help="GPUs handed out in order, one per probe")
    r.set_defaults(fn=cmd_drift)

    a = ap.parse_args()
    load_dotenv()
    a.fn(a)


if __name__ == "__main__":
    main()
