#!/usr/bin/env python3
"""Measure how far a checkpoint has drifted from the base policy, on the base policy's own held-out turns.

``--build`` samples a probe of recorded base-model turns (reasoning + tool call) from one recorded run, skipping the
training and eval tasks. Otherwise each probe turn is teacher-forced and, over its target tokens (overall / thought / call span):
  nll      -log p_ckpt(base tokens)
  kl       KL(p_base || p_ckpt)   (0 for the base itself)
  entropy  H(p_ckpt)

    python src/sft/scripts/policy_drift.py --build --source-job <base-model run dir> --train-tasks <task list> \
        --exclude-tasks <eval split json> --out <probe.jsonl>
    python src/sft/scripts/policy_drift.py --probe <probe.jsonl> --ckpt <checkpoint dir or glob> --label <name> --out <result.json>
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import random
from pathlib import Path

BASE = os.environ.get("SFT_BASE_MODEL")               # base model path; --base overrides


def build_probe(out: Path, source_job: Path, train_tasks: Path, exclude_tasks: Path, per_trajectory: int = 6, seed: int = 0) -> None:
    """Write up to ``per_trajectory`` recorded turns per trial of ``source_job`` to ``out``, skipping the training tasks
    (one id per line in ``train_tasks``) and every task id found in ``exclude_tasks`` (an eval split JSON)."""
    train = set(l.strip() for l in open(train_tasks) if l.strip())
    ood: set[str] = set()

    def walk(o):
        if isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
        elif isinstance(o, str) and o.startswith("instance_"):
            ood.add(o)
    walk(json.load(open(exclude_tasks)))
    R = source_job
    rng = random.Random(seed)
    rows = []
    for f in sorted(glob.glob(str(R / "*__*/result.json"))):
        r = json.load(open(f)); task = r["task_name"].split("/", 1)[1]; trial = os.path.basename(os.path.dirname(f))
        if task in train or task in ood:
            continue
        p = R / "passback-proxy" / trial / "requests.jsonl"
        if not p.exists():
            continue
        turns = []
        for line in open(p):
            try:
                x = json.loads(line)
            except ValueError:
                continue
            if x.get("response_rewritten") or x.get("note"):
                continue
            req, resp = x.get("request") or {}, x.get("response") or {}
            msg = ((resp.get("choices") or [{}])[0]).get("message") or {}
            think = (msg.get("reasoning_content") or msg.get("reasoning") or "").strip()
            calls = msg.get("tool_calls") or []
            if not think or not calls:
                continue
            turns.append({"task": task, "trial": trial, "turn": x.get("turn_index"), "messages": req.get("messages"), "tools": req.get("tools"),
                          "reasoning": think, "content": (msg.get("content") or "").strip() if isinstance(msg.get("content"), str) else "",
                          "tool_calls": [{"name": (c.get("function") or {}).get("name"), "arguments": (c.get("function") or {}).get("arguments")} for c in calls]})
        if not turns:
            continue
        idx = sorted(rng.sample(range(len(turns)), min(per_trajectory, len(turns))))
        rows += [turns[i] for i in idx]
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"probe: {len(rows)} turns from {len(set(r['task'] for r in rows))} held-out tasks -> {out}")


def _args_dict(a):
    """Tool-call arguments as a dict (JSON-decoded; undecodable -> {"_raw": a})."""
    if isinstance(a, dict):
        return a
    try:
        return json.loads(a) if isinstance(a, str) else {}
    except ValueError:
        return {"_raw": a}


def render(tok, row: dict, max_ctx: int) -> tuple[list[int], list[int], int]:
    """(context ids, target ids, index in target where the tool-call span starts); context keeps the last ``max_ctx`` tokens."""
    msgs = []
    for m in row["messages"]:
        m = dict(m)
        if m.get("role") == "assistant" and m.get("tool_calls"):
            m["tool_calls"] = [{"type": "function", "function": {"name": (c.get("function") or {}).get("name"),
                                                               "arguments": _args_dict((c.get("function") or {}).get("arguments"))}} for c in m["tool_calls"]]
        if m.get("content") is None:
            m["content"] = ""
        msgs.append(m)
    target_msg = {"role": "assistant", "content": row["content"], "reasoning_content": row["reasoning"],
                  "tool_calls": [{"type": "function", "function": {"name": c["name"], "arguments": _args_dict(c["arguments"])}} for c in row["tool_calls"]]}
    ctx_text = tok.apply_chat_template(msgs, tools=row.get("tools"), tokenize=False, add_generation_prompt=True)
    full_text = tok.apply_chat_template(msgs + [target_msg], tools=row.get("tools"), tokenize=False, add_generation_prompt=False)
    assert full_text.startswith(ctx_text), "template prefix mismatch"
    tgt_text = full_text[len(ctx_text):]
    ctx = tok(ctx_text, add_special_tokens=False)["input_ids"]
    tgt = tok(tgt_text, add_special_tokens=False)["input_ids"]
    if len(ctx) > max_ctx:
        ctx = ctx[-max_ctx:]
    split = tgt_text.find("</think>")
    call_start = len(tok(tgt_text[:split + len("</think>")], add_special_tokens=False)["input_ids"]) if split >= 0 else len(tgt)
    return ctx, tgt, call_start


def measure(base_path: str, ckpt_path: str | None, probe: Path, max_ctx: int, max_samples: int | None) -> dict:
    """Mean nll / kl / entropy per target token (overall, _think, _call) of ``ckpt_path`` (None = base) on CUDA."""
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer
    tok = AutoTokenizer.from_pretrained(base_path)
    dev = "cuda"
    base = AutoModelForCausalLM.from_pretrained(base_path, dtype=torch.bfloat16, attn_implementation="sdpa").to(dev).eval()
    ckpt = base if ckpt_path is None else AutoModelForCausalLM.from_pretrained(ckpt_path, dtype=torch.bfloat16, attn_implementation="sdpa").to(dev).eval()
    rows = [json.loads(l) for l in open(probe)]
    if max_samples:
        rows = rows[:max_samples]
    agg = {k: [0.0, 0] for k in ("nll", "kl", "entropy", "nll_think", "kl_think", "entropy_think", "nll_call", "kl_call", "entropy_call")}
    per_sample = []
    with torch.no_grad():
        for i, row in enumerate(rows):
            ctx, tgt, call_start = render(tok, row, max_ctx)
            ids = torch.tensor([ctx + tgt], device=dev)
            keep = len(tgt) + 1
            lb = base(ids, logits_to_keep=keep).logits[0, :-1].float()          # predicts tgt[0..T-1]
            lc = lb if ckpt is base else ckpt(ids, logits_to_keep=keep).logits[0, :-1].float()
            logp_b = torch.log_softmax(lb, -1); logp_c = torch.log_softmax(lc, -1)
            t = torch.tensor(tgt, device=dev)
            nll = -logp_c.gather(1, t[:, None])[:, 0]
            kl = (logp_b.exp() * (logp_b - logp_c)).sum(-1)
            ent = -(logp_c.exp() * logp_c).sum(-1)
            spans = {"": slice(0, len(tgt)), "_think": slice(0, call_start), "_call": slice(call_start, len(tgt))}
            s = {"task": row["task"], "turn": row["turn"], "ctx_tokens": len(ctx), "tgt_tokens": len(tgt), "think_tokens": call_start}
            for suf, sl in spans.items():
                n = sl.stop - sl.start
                if n <= 0:
                    continue
                for name, v in (("nll", nll), ("kl", kl), ("entropy", ent)):
                    val = float(v[sl].sum()); agg[name + suf][0] += val; agg[name + suf][1] += n; s[name + suf] = val / n
            per_sample.append(s)
            del lb, lc, logp_b, logp_c
            if (i + 1) % 10 == 0:
                print(f"  {i + 1}/{len(rows)} nll {agg['nll'][0] / agg['nll'][1]:.4f} kl {agg['kl'][0] / agg['kl'][1]:.4f} ent {agg['entropy'][0] / agg['entropy'][1]:.4f}", flush=True)
    out = {k: (v[0] / v[1] if v[1] else None) for k, v in agg.items()}
    out["tokens"] = agg["nll"][1]; out["samples"] = len(per_sample); out["per_sample"] = per_sample
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--build", action="store_true", help="build the probe set into --out and exit")
    ap.add_argument("--probe", type=Path, default=None, help="probe jsonl (required unless --build)")
    ap.add_argument("--source-job", type=Path, default=None, help="--build: a recorded run of the base model (with passback-proxy logs)")
    ap.add_argument("--exclude-tasks", type=Path, default=None, help="--build: eval split JSON whose task ids the probe skips")
    ap.add_argument("--per-trajectory", type=int, default=6, help="probe turns sampled per trial (--build)")
    ap.add_argument("--train-tasks", type=Path, default=None, help="--build: training task ids the probe skips (one per line)")
    ap.add_argument("--base", default=BASE, help="base model path (default $SFT_BASE_MODEL)")
    ap.add_argument("--ckpt", default=None, help="checkpoint dir or glob, last match used (omit = measure the base against itself)")
    ap.add_argument("--label", default=None, help="result label (default: checkpoint path)")
    ap.add_argument("--max-ctx", type=int, default=24000, help="context tokens kept per probe turn (left-truncated)")
    ap.add_argument("--max-samples", type=int, default=None, help="use only the first N probe turns")
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    if a.build:
        if not (a.source_job and a.train_tasks and a.exclude_tasks):
            raise SystemExit("--build needs --source-job, --train-tasks and --exclude-tasks")
        build_probe(a.out, a.source_job, a.train_tasks, a.exclude_tasks, a.per_trajectory)
        return
    if not a.probe:
        raise SystemExit("--probe is required (build one with --build)")
    ckpt = sorted(glob.glob(a.ckpt))[-1] if a.ckpt and any(ch in a.ckpt for ch in "*?") else a.ckpt
    if not a.base:
        raise SystemExit("set SFT_BASE_MODEL in opera/.env (the base model path) or pass --base")
    res = measure(a.base, ckpt, a.probe, a.max_ctx, a.max_samples)
    res.update({"label": a.label or (ckpt or "base"), "ckpt": ckpt, "probe": str(a.probe), "max_ctx": a.max_ctx})
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(res, indent=1) + "\n")
    print(f"{res['label']}: tokens {res['tokens']} nll {res['nll']:.4f} kl {res['kl']:.4f} entropy {res['entropy']:.4f} | think nll {res['nll_think']:.4f} kl {res['kl_think']:.4f} | call nll {res['nll_call']:.4f} kl {res['kl_call']:.4f}")


if __name__ == "__main__":
    main()
