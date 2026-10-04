#!/usr/bin/env python3
"""Export policy SFT data from the proxy's recorded API calls (exact prompt, tool schemas and the model's own thinking).

Reads ``<job>/critic-logs/<trial>/requests.jsonl`` or ``<job>/passback-proxy/<trial>/requests.jsonl``. One sample = one
recorded call: request history (loss off) + target ``<think>…</think>`` + tool call(s) (loss on), ``tools`` from the
request. Critic notes and sentences naming the critic are removed from the context; ``--note-into-think`` splices an
audited note's diagnosis into the target's thinking instead.

    python src/sft/preprocess/export_policy_sft.py --source-job results/<benchmark>/<split>/<job> \\
        --select repair --think-from reasoning --tasks-file <tasks.json> --tasks-key <split> --out src/sft/data/<dataset>
"""
from __future__ import annotations

import argparse
import ast
import collections
import json
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_records import repair_windows, rows_of, task_results  # noqa: E402

NOTE_MARKER = "<!-- opera-critic -->"
CHARS_PER_TOKEN = 3.3                      # token estimate: tokens ≈ chars / 3.3


def records(job: Path, trial: str) -> list[dict]:
    """One trial's recorded calls, oldest first (from critic-logs/ or passback-proxy/)."""
    for sub in ("critic-logs", "passback-proxy"):
        f = job / sub / trial / "requests.jsonl"
        if f.exists():
            rows = []
            for line in f.read_text(encoding="utf-8").splitlines():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
            return sorted(rows, key=lambda r: (r.get("turn_index") or 0, r.get("recorded_at") or 0))
    return []


def _text(content) -> str:
    """Plain text of message content: a string, a list of parts, or a stored Python repr of such a list."""
    if isinstance(content, str):
        if content.startswith("[") and "'type'" in content:
            try:
                content = ast.literal_eval(content)
            except (ValueError, SyntaxError):
                return content
        else:
            return content
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return str(content or "")


def norm_tools(req: dict, api: str) -> list[dict]:
    """The request's tool schemas in chat format; ``api``: chat | responses (flat schemas)."""
    tools = req.get("tools") or []
    if api == "chat":
        return tools
    out = []
    for t in tools:
        if t.get("type") == "function" and "function" not in t:
            out.append({"type": "function", "function": {k: v for k, v in t.items() if k != "type"}})
        else:
            out.append(t)
    return out


def history(req: dict, api: str) -> list[dict]:
    """The request's prompt as ms-swift agent messages (loss off), with critic mentions scrubbed from the model's own text."""
    msgs: list[dict] = []

    def add(role: str, content: str, loss: bool = False) -> None:
        msgs.append({"role": role, "content": content, "loss": loss} if role not in ("system", "user")
                    else {"role": role, "content": content})

    dropped_calls: list = []                     # ids of dropped tool calls; their responses are dropped too
    if api == "chat":
        for m in req.get("messages") or []:
            role, content = m.get("role"), _text(m.get("content"))
            if role in ("system", "user"):
                add(role, content)                    # notes are removed later by strip_notes()
            elif role == "assistant":
                if scrub_critic(content).strip():
                    add("assistant", scrub_critic(content))
                for tc in m.get("tool_calls") or []:
                    fn = tc.get("function") or {}
                    call = scrub_tool_call({"name": fn.get("name"), "arguments": fn.get("arguments")})
                    if call is None:
                        dropped_calls.append(tc.get("id"))
                        continue
                    add("tool_call", json.dumps(call, ensure_ascii=False))
            elif role == "tool":
                if m.get("tool_call_id") in dropped_calls:
                    continue
                add("tool_response", content)
        return msgs

    if req.get("instructions"):
        add("system", req["instructions"])
    for item in req.get("input") or []:
        kind = item.get("type")
        if kind == "message":
            content = _text(item.get("content"))
            if item.get("role") != "user":
                content = scrub_critic(content)
                if not content.strip():
                    continue
            add("user" if item.get("role") == "user" else "assistant", content)
        elif kind == "function_call":
            call = scrub_tool_call({"name": item.get("name"), "arguments": item.get("arguments")})
            if call is None:
                dropped_calls.append(item.get("call_id"))
                continue
            add("tool_call", json.dumps(call, ensure_ascii=False))
        elif kind == "function_call_output":
            if item.get("call_id") in dropped_calls:
                continue
            add("tool_response", _text(item.get("output")))
        # `reasoning` items are encrypted and skipped
    return msgs


def strip_notes(msgs: list[dict]) -> list[dict]:
    """Drop messages carrying NOTE_MARKER; for a note delivered as a substitute tool call, drop its tool response too."""
    out: list[dict] = []
    drop_response = False
    for m in msgs:
        if NOTE_MARKER in m["content"]:
            drop_response = m["role"] == "tool_call"
            continue
        if drop_response and m["role"] == "tool_response":
            drop_response = False
            continue
        out.append(m)
    return out


CRITIC_MENTION = re.compile(r"[^.!?:;\n]*\b(?:critic|critics|critic's|guidance)\b[^.!?:;\n]*(?:[.!?:;]|\n|$)", re.I)  # a sentence naming the critic
CRITIC_WORD = re.compile(r"\bcritics?(?:'s)?\b", re.I)        # must not appear in a sample's unsupervised context
dropped_mentions = [0]                                       # count of samples dropped for a critic mention in context


def scrub_tool_call(call: dict) -> dict | None:
    """Scrub critic mentions from a tool call's ``thought`` / ``summary``; None if the whole thought was about the critic."""
    args = call.get("arguments")
    if isinstance(args, str):
        try:
            parsed = json.loads(args)
        except (ValueError, TypeError):
            return call
    elif isinstance(args, dict):
        parsed = dict(args)
    else:
        return call
    changed = False
    for field in ("thought", "summary"):
        v = parsed.get(field)
        if isinstance(v, str) and CRITIC_MENTION.search(v):
            cleaned = scrub_critic(v)
            if field == "thought" and not cleaned.strip():
                return None
            parsed[field] = cleaned
            changed = True
    if not changed:
        return call
    return {**call, "arguments": json.dumps(parsed, ensure_ascii=False) if isinstance(args, str) else parsed}


def scrub_critic(text: str) -> str:
    """Remove sentences that mention the critic (CRITIC_MENTION)."""
    return re.sub(CRITIC_MENTION, "", text).strip()


NOTE_LEAD_IN = "Let me step back and check the work so far against what the task actually requires."
NOTE_BOILERPLATE = re.compile(r"Do not reset,? clean,? or delete[^\n]*\n*")
NOTE_REWRITES = (("Evidence:", "Issue:"), ("Issue resolution criterion:", "This is resolved when:"))


def note_body(text: str) -> str:
    """A note's diagnosis as thought text: from ``Evidence:`` on (or after the operator line), boilerplate removed and
    labels rephrased (NOTE_REWRITES); "" if the note has neither."""
    i = text.find("Evidence:")
    if i < 0:
        i = text.find("Recommended repair operator:")
        if i < 0:
            return ""
        nl = text.find("\n", i)
        i = nl + 1 if nl >= 0 else i
    body = text[i:]
    body = NOTE_BOILERPLATE.sub("", body)
    for src, dst in NOTE_REWRITES:
        body = body.replace(src, dst)
    return body.strip()


def audited_findings(job: Path, trial: str) -> set[str]:
    """Finding ids whose raise passed the intervention audit (only their notes are spliced into the thinking)."""
    d = job / "critic-logs" / trial / "decisions.jsonl"
    if not d.exists():
        return set()
    ok: set[str] = set()
    for row in rows_of(d):
        fid = row.get("finding_id")
        if not fid:
            continue
        for g in row.get("gates") or []:
            if isinstance(g, dict) and g.get("gate") == "intervention_audit" and g.get("accepted"):
                ok.add(fid)
    return ok


QUERY_ROLES = {"user", "tool_response"}
RESPONSE_ROLES = {"assistant", "tool_call"}


def normalize(msgs: list[dict]) -> list[dict]:
    """Merge adjacent query-side (or adjacent assistant) messages so ms-swift sees strict query/response alternation."""
    out: list[dict] = []
    for m in msgs:
        if out and m["role"] in QUERY_ROLES and out[-1]["role"] in QUERY_ROLES:
            out[-1]["content"] = (out[-1]["content"] + "\n\n" + m["content"]).strip()
            continue
        if out and m["role"] == "assistant" and out[-1]["role"] == "assistant":
            out[-1]["content"] = (out[-1]["content"] + "\n\n" + m["content"]).strip()
            continue
        out.append(dict(m))
    return out


def target(resp: dict, api: str, think_from: str, note: str = "") -> list[dict] | None:
    """The target turn: ``<think>…</think>`` + visible text, then its tool call(s), all with loss on.

    ``think_from``: reasoning (the returned reasoning) | message (visible text stands in when no reasoning is returned).
    ``note``, if given, is prepended to the thinking after NOTE_LEAD_IN. None if there is no tool call or no thought."""
    think, visible, calls = "", "", []
    if api == "chat":
        msg = ((resp.get("choices") or [{}])[0]).get("message") or {}
        think = scrub_critic((msg.get("reasoning") or msg.get("reasoning_content") or "").strip())
        visible = scrub_critic(_text(msg.get("content")).strip())
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            calls.append({"name": fn.get("name"), "arguments": fn.get("arguments")})
    else:
        for o in resp.get("output") or []:
            if o.get("type") == "reasoning":
                parts = o.get("summary")
                if isinstance(parts, str):
                    try:
                        parts = ast.literal_eval(parts)
                    except (ValueError, SyntaxError):
                        parts = []
                think = think or "".join(p.get("text", "") for p in (parts or []) if isinstance(p, dict)).strip()
            elif o.get("type") == "message":
                visible = (visible + "\n" + _text(o.get("content"))).strip()
            elif o.get("type") == "function_call":
                calls.append({"name": o.get("name"), "arguments": o.get("arguments")})
    if not calls:
        return None
    if think_from == "message" and not think:
        think, visible = visible, ""
    think, visible = scrub_critic(think), scrub_critic(visible)
    if note:
        note = f"{NOTE_LEAD_IN}\n\n{note}"
        think = f"{note}\n\n{think}" if think else note
    if not think:
        return None                            # never train an empty thought
    content = f"<think>\n{think}\n</think>" + (f"\n\n{visible}" if visible else "")
    cleaned = [scrub_tool_call(c) for c in calls]
    if any(c is None for c in cleaned):
        return None
    return [{"role": "assistant", "content": content, "loss": True}] + \
           [{"role": "tool_call", "content": json.dumps(c, ensure_ascii=False), "loss": True} for c in cleaned]


def samples(job: Path, select: str, think_from: str, keep: set[str] | None, drop: set[str], max_chars: int,
            note_into_think: bool = False, note_turns: str = "delivered") -> list[dict]:
    """Samples from one job's passing trials; ``select``: repair | passing, ``note_turns``: delivered | all."""
    out = []
    for task, (passed, trial) in sorted(task_results(job).items()):
        if not passed or task in drop or (keep is not None and task not in keep):
            continue
        rows = records(job, trial)
        if not rows:
            continue
        audited = audited_findings(job, trial) if note_into_think else set()
        turns = {r.get("turn_index") for r in rows if r.get("turn_index")}
        wanted = repair_windows(job, trial, max(turns or {0})) if select == "repair" else None
        for r in rows:
            turn = r.get("turn_index")
            if wanted is not None and turn not in wanted:
                continue
            api = r.get("api") or "chat"
            req, resp = r.get("request") or {}, r.get("response") or {}
            if r.get("response_rewritten"):
                continue          # reply replaced by the proxy's note-printing substitute
            raw = history(req, api)
            fid, delivered = r.get("finding_id"), (r.get("note") or "")
            fresh = r.get("delivery_mode") in ("intervention", "update")   # not a re-projection of an earlier note
            spliced = note_body(delivered) if (note_into_think and delivered and fid in audited
                                              and (fresh or note_turns == "all")) else ""
            tgt = target(resp, api, think_from, note=spliced)
            if not tgt or any(NOTE_MARKER in m["content"] for m in tgt):
                continue
            msgs = normalize(strip_notes(raw)) + tgt
            if any(CRITIC_WORD.search(m["content"]) for m in msgs if not m.get("loss")):
                dropped_mentions[0] += 1
                continue
            chars = sum(len(m["content"]) for m in msgs)
            if chars > max_chars:
                continue
            out.append({"messages": msgs, "tools": json.dumps(norm_tools(req, api), ensure_ascii=False),
                        "meta": {"kind": f"policy_{select}", "task": task, "trial": trial, "turn": turn,
                                 "think_from": think_from, "note_in_think": bool(spliced), "chars": chars,
                                 "source_job": job.name}})
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source-job", action="append", required=True, type=Path)
    ap.add_argument("--select", choices=("repair", "passing"), required=True)
    ap.add_argument("--think-from", choices=("reasoning", "message"), default="reasoning",
                    help="thought source: the model's returned reasoning, or its visible text when no reasoning is returned")
    ap.add_argument("--note-turns", choices=("delivered", "all"), default="delivered",
                    help="turns that get the note spliced: only the turn it was raised/updated on (default), or every turn it was shown")
    ap.add_argument("--note-into-think", action="store_true",
                    help="prepend an audited note's diagnosis to the target turn's thinking (the note never appears in the context)")
    ap.add_argument("--tasks-file", type=Path, help="JSON holding the task ids to keep")
    ap.add_argument("--tasks-key", default=None, help="<benchmark> key in --tasks-file (its 'matched' list is used)")
    ap.add_argument("--exclude-tasks-file", type=Path, help="JSON (eval split) whose task ids must NOT appear in training")
    ap.add_argument("--max-tokens", type=int, default=128000, help="drop samples above this (chars/3.3), never truncate")
    ap.add_argument("--limit", type=int, default=0, help="keep at most N samples (after shuffling)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()

    keep = None
    if a.tasks_file:
        blob = json.loads(a.tasks_file.read_text())
        part = blob[a.tasks_key] if a.tasks_key else blob
        keep = set(part["matched"] if isinstance(part, dict) and "matched" in part else part)
    drop: set[str] = set()
    if a.exclude_tasks_file:
        def walk(o):
            if isinstance(o, dict):
                for v in o.values():
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)
            elif isinstance(o, str) and o.startswith("instance_"):
                drop.add(o)
        walk(json.loads(a.exclude_tasks_file.read_text()))

    rows: list[dict] = []
    for job in a.source_job:
        rows += samples(job, a.select, a.think_from, keep, drop, int(a.max_tokens * CHARS_PER_TOKEN), a.note_into_think, a.note_turns)
    random.Random(a.seed).shuffle(rows)
    if a.limit:
        rows = rows[:a.limit]

    a.out.mkdir(parents=True, exist_ok=True)
    with (a.out / "train.jsonl").open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    chars = sorted(r["meta"]["chars"] for r in rows)
    manifest = {
        "select": a.select, "think_from": a.think_from, "note_into_think": a.note_into_think, "note_turns": a.note_turns,
        "dropped_critic_mention_in_context": dropped_mentions[0],
        "samples_with_note_in_think": sum(1 for r in rows if r["meta"].get("note_in_think")),
        "source_jobs": [str(j) for j in a.source_job],
        "samples": len(rows), "tasks": len({r["meta"]["task"] for r in rows}),
        "excluded_eval_tasks": len(drop), "max_tokens": a.max_tokens,
        "chars": {"p10": chars[len(chars) // 10], "p50": chars[len(chars) // 2], "p90": chars[int(.9 * len(chars))],
                  "total": sum(chars)} if chars else {},
        "est_tokens_total": round(sum(chars) / CHARS_PER_TOKEN),
        "per_source": dict(collections.Counter(r["meta"]["source_job"] for r in rows)),
    }
    (a.out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
