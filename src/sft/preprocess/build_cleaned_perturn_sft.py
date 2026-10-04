#!/usr/bin/env python3
"""Per-turn SFT samples from cleaned passing trajectories (by default the shortest trajectory per task).

Input: a per-turn export from ``export_policy_sft.py``. Bad tool-call turns (unknown tool, validation error, refused)
are removed with their observations; each remaining call becomes one sample: history as the server renders it back
(loss off) + target ``<think>\\n{thought}`` + tool call (loss on). Multi-call turns are split into one sample per call.

    python src/sft/preprocess/build_cleaned_perturn_sft.py src/sft/data/<per-turn dataset> --out src/sft/data/<dataset>
"""
from __future__ import annotations

import argparse
import collections
import json
import re
from pathlib import Path

THINK = re.compile(r"<think>\n?(.*?)\n?</think>", re.S)
MARKUP = ("<tool_call>", "</tool_call>", "<|im_end|>", "<|im_start|>", "</think>", "<think>")
BAD_OBS = ("Error validating tool", "Cannot execute multiple commands", "action was not executed", "Cannot infer 'command'")  # refusal markers


def groups_of(msgs: list[dict]) -> list[dict]:
    """Turns as {assistant: idx | None, calls: [idx], obs: [idx]}, in trajectory order (preamble rows skipped)."""
    out = []; i = 0
    while i < len(msgs):
        m = msgs[i]
        if m["role"] == "assistant" or (m["role"] == "tool_call" and (i == 0 or msgs[i - 1]["role"] not in ("assistant", "tool_call"))):
            g = {"assistant": i if m["role"] == "assistant" else None, "calls": [], "obs": []}
            if m["role"] == "assistant":
                i += 1
            while i < len(msgs) and msgs[i]["role"] == "tool_call":
                g["calls"].append(i); i += 1
            while i < len(msgs) and msgs[i]["role"] == "tool_response":
                g["obs"].append(i); i += 1
            out.append(g)
        else:
            i += 1
    return out


def is_bad(msgs: list[dict], g: dict, tools: set[str]) -> str | None:
    """Why a turn is bad (no call, unparseable, unknown tool, refused / validation error), or None if it is fine."""
    if not g["calls"]:
        return "no call"
    try:
        names = [json.loads(msgs[c]["content"]).get("name") for c in g["calls"]]
    except ValueError:
        return "unparseable call"
    if any(n not in tools for n in names):
        return "invalid tool name"
    o = " ".join(msgs[j]["content"][:300] for j in g["obs"])
    if any(k in o for k in BAD_OBS):
        return "refused / validation error"
    return None


def call_key(content: str) -> str:
    """Serialisation-independent identity of a tool call: canonical JSON of (name, parsed arguments)."""
    try:
        c = json.loads(content)
        a = c.get("arguments")
        if isinstance(a, str):
            try:
                a = json.loads(a)
            except ValueError:
                pass
        return json.dumps([c.get("name"), a], sort_keys=True, ensure_ascii=False)
    except (ValueError, AttributeError):
        return content


def open_thought(content: str) -> str | None:
    """Thought + visible text of an assistant row as one open-form thought; None if empty or containing markup."""
    m = THINK.search(content)
    think = (m.group(1) if m else "").strip()
    visible = THINK.sub("", content, count=1).strip()
    t = f"{think}\n\n{visible}" if think and visible else (think or visible)
    if not t or any(k in t for k in MARKUP):
        return None
    return t


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dataset", type=Path, help="a per-turn dataset directory (export_policy_sft.py output)")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--max-think-chars", type=int, default=None, help="drop samples whose thought is longer than this")
    ap.add_argument("--multi-call-thought", choices=("repeat", "empty", "drop"), default="repeat",
                    help="thought given to the 2nd..kth single-call splits of a multi-call turn: the turn's thought again (default), an empty thought, or no sample")
    ap.add_argument("--all-trajectories", action="store_true", help="keep every passing trajectory of a task instead of the shortest one")
    ap.add_argument("--max-tokens", type=int, default=None, help="drop samples whose cleaned context exceeds this (chars/3.3, counted after the bad turns are removed)")
    a = ap.parse_args()

    per: dict[tuple, dict[int, dict]] = collections.defaultdict(dict)
    for line in a.dataset.joinpath("train.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line); per[(r["meta"].get("source_job"), r["meta"]["trial"])][r["meta"]["turn"]] = r
    # one trajectory per task: the one with the fewest turns
    by_task: dict[str, list[tuple[int, tuple]]] = collections.defaultdict(list)
    for key, turns in per.items():
        last = turns[max(turns)]
        by_task[last["meta"]["task"]].append((len(groups_of(last["messages"])), key))
    chosen = {task: min(v)[1] for task, v in by_task.items()}
    if a.all_trajectories:
        chosen = {(task, key): key for task, v in by_task.items() for _, key in v}

    stats = collections.Counter(); samples = []
    for task, key in sorted(chosen.items()):
        turns = per[key]; last = turns[max(turns)]; msgs = [dict(m) for m in last["messages"]]
        tools = {t["function"]["name"] for t in json.loads(last.get("tools") or "[]")}
        gs = groups_of(msgs)
        # match turns to samples by call content; use the position only when turn numbering is contiguous
        contiguous = sorted(turns) == list(range(1, len(gs) + 1))
        by_call: dict[str, list[dict]] = collections.defaultdict(list)          # call identity -> samples, in turn order
        for t_ in sorted(turns):
            s_ = turns[t_]
            calls_ = [m for m in s_["messages"] if m.get("loss") is True and m["role"] == "tool_call"]
            if len(calls_) == 1:
                by_call[call_key(calls_[0]["content"])].append(s_)
        by_multi: dict[tuple, list[dict]] = collections.defaultdict(list)     # multi-call samples, keyed by their call set
        for t_ in sorted(turns):
            s_ = turns[t_]
            calls_ = [m for m in s_["messages"] if m.get("loss") is True and m["role"] == "tool_call"]
            if len(calls_) > 1:
                by_multi[tuple(sorted(call_key(m["content"]) for m in calls_))].append(s_)
        used: set[int] = set()
        def match_multi(keys: list[str]):
            for s_ in by_multi.get(tuple(sorted(keys)), []):
                if id(s_) not in used:
                    used.add(id(s_)); return s_
            return None
        def match(content: str):
            for s_ in by_call.get(call_key(content), []):
                if id(s_) not in used:
                    used.add(id(s_)); return s_
            return None
        if not contiguous:
            stats["trajectories with non-contiguous turn numbering (matched by call content)"] += 1
        bad = [is_bad(msgs, g, tools) for g in gs]
        keep = []
        for gi, g in enumerate(gs):
            if bad[gi]:
                stats["bad:" + bad[gi]] += 1
                continue
            keep.append(gi)
        stats["turns"] += len(gs); stats["turns kept"] += len(keep)
        preamble = msgs[: (gs[0]["assistant"] if gs[0]["assistant"] is not None else gs[0]["calls"][0])] if gs else msgs
        history: list[dict] = list(preamble)
        for gi in keep:
            g = gs[gi]; turn_no = gi + 1
            keys = [call_key(msgs[c]["content"]) for c in g["calls"]]
            src = None
            if len(keys) == 1:
                pos = turns.get(turn_no) if contiguous else None
                if pos is not None and call_key(next(m["content"] for m in pos["messages"] if m.get("loss") is True and m["role"] == "tool_call")) == keys[0]:
                    src = pos
                else:
                    src = match(msgs[g["calls"][0]]["content"])
                    if src is not None and pos is not None:
                        stats["positional lookup would have attached the wrong thought"] += 1
            else:
                src = match_multi(keys)
            tgt = next((m for m in src["messages"] if m.get("loss") is True and m["role"] == "assistant"), None) if src else None
            thought = open_thought(tgt["content"]) if tgt else None
            if a.max_think_chars and thought and len(thought) > a.max_think_chars:
                thought = None; stats["thought too long"] += 1
            if not thought:
                stats["turn without sample (no thought / no matching export sample)"] += 1
            # a multi-call turn becomes one sample per call; split j sees the turn's earlier calls in its history
            n_calls = len(g["calls"]); obs_by_call = [[] for _ in range(n_calls)]
            if len(g["obs"]) == n_calls:
                for j_, o in enumerate(g["obs"]):
                    obs_by_call[j_].append(o)
            else:
                obs_by_call[-1] = list(g["obs"])
            if n_calls > 1:
                stats["multi-call turns split"] += 1; stats["calls from split turns"] += n_calls
            split_hist: list[dict] = []
            for j_, c in enumerate(g["calls"]):
                th = thought if (j_ == 0 or a.multi_call_thought == "repeat") else ("" if a.multi_call_thought == "empty" else None)
                if thought and th is not None:
                    ctx = history + split_hist
                    over = a.max_tokens is not None and (sum(len(m["content"]) for m in ctx) + len(th) + len(msgs[c]["content"]) + len(last.get("tools") or "")) / 3.3 > a.max_tokens
                    if over:
                        stats["over max tokens"] += 1
                    else:
                        sample = [dict(m) for m in ctx]
                        for m in sample:
                            m["loss"] = False
                        sample.append({"role": "assistant", "content": f"<think>\n{th}", "loss": True})
                        sample.append({"role": "tool_call", "content": msgs[c]["content"], "loss": True})
                        samples.append({"messages": sample, "tools": last.get("tools"),
                                        "meta": {**last.get("meta", {}), "turn": turn_no, "split": j_, "n_calls": n_calls, "kind": "cleaned_perturn",
                                                 "note_in_think": bool(src and src["meta"].get("note_in_think"))}})
                        stats["samples"] += 1; stats["note samples"] += bool(src and src["meta"].get("note_in_think"))
                        if j_ > 0:
                            stats["samples from split continuations"] += 1
                # this split as history: assistant row, call, observation(s)
                if j_ == 0 and g["assistant"] is not None:
                    split_hist.append({**msgs[g["assistant"]], "loss": False})
                elif j_ > 0:
                    split_hist.append({"role": "assistant", "content": "", "loss": False})
                split_hist.append({**msgs[c], "loss": False})
                for o in obs_by_call[j_]:
                    split_hist.append({**msgs[o], "loss": False})
            history.extend(split_hist)
    a.out.mkdir(parents=True, exist_ok=True)
    with a.out.joinpath("train.jsonl").open("w", encoding="utf-8") as fh:
        for s in samples:
            fh.write(json.dumps(s, ensure_ascii=False) + "\n")
    manifest = {"source": str(a.dataset), "trajectories": len(chosen), "tasks": len({(k[0] if isinstance(k, tuple) else k) for k in chosen}),
                "all_trajectories": a.all_trajectories, "max_tokens": a.max_tokens, "multi_call_thought": a.multi_call_thought, **dict(stats),
                "supervised_tokens_est": round(sum(len(m["content"]) for s in samples for m in s["messages"] if m.get("loss")) / 3.3),
                "context_tokens_est": round(sum(len(m["content"]) for s in samples for m in s["messages"]) / 3.3)}
    a.out.joinpath("manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
