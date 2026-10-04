#!/usr/bin/env python3
"""OpenHands SDK runner, executed inside the task container: drives one ``Conversation`` on the instruction
and writes an ATIF ``trajectory.json``.

Configuration is read from the environment:
  LLM_MODEL / LLM_API_KEY / LLM_BASE_URL    the LiteLLM model name, key and endpoint
  LLM_REASONING_EFFORT=<effort>             top-level reasoning effort; unset = the field is not sent
  LITELLM_EXTRA_BODY=<json>                 merged into every request body
  LLM_TEMPERATURE / LLM_TOP_P               sampling
  LLM_MAX_INPUT_TOKENS / LLM_MAX_OUTPUT_TOKENS / LLM_TIMEOUT / LLM_NUM_RETRIES   integer ceilings (unset = SDK defaults)
  LLM_NATIVE_TOOL_CALLING=false             describe tools in the prompt instead of native calls
  LLM_TOOL_TEXT_REPAIR=deepseek             prompt-mode tools only: repair corrupted tool calls before the SDK parses them
  LLM_RETRY_MALFORMED_CALLS=<n>             re-prompt, at most n times in a row, when a run ends on tool-call markup
                                            written as text or on a degenerate reply
  LLM_STRICT_TOOLS=1                        mark every function tool ``strict`` (vLLM then constrains tool-call decoding)
  CONDENSER=llm-summarizing                 summarize old history; CONDENSER_MAX_SIZE / _KEEP_FIRST / _MAX_TOKENS tune it
  MAX_ITERATIONS=<n>                        iteration cap per run
  LOAD_SKILLS / SKILL_PATHS, MCP_SERVERS_JSON, SESSION_ID   skills, MCP servers and the trajectory's session id
"""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from openhands.sdk import (
    LLM,
    Agent,
    AgentContext,
    Conversation,
    Tool,
    get_logger,
)
from openhands.sdk.context import Skill
from openhands.sdk.event import (
    ActionEvent,
    MessageEvent,
    ObservationEvent,
    TokenEvent,
)
from openhands.tools.file_editor import FileEditorTool
from openhands.tools.task_tracker import TaskTrackerTool
from openhands.tools.terminal import TerminalTool

logger = get_logger(__name__)


def load_skill_from_file(skill_path: Path) -> Skill | None:
    """Load a skill from a SKILL.md file."""
    if not skill_path.exists():
        return None

    content = skill_path.read_text()
    name = skill_path.parent.name

    return Skill(
        name=name,
        content=content,
        source=str(skill_path),
        trigger=None,  # always active
    )


def discover_skills(skill_paths: list[str]) -> list[Skill]:
    """Load every ``<path>/<skill>/SKILL.md`` under ``skill_paths``, first occurrence of a name wins."""
    seen_names: set[str] = set()
    skills: list[Skill] = []

    for base_path_str in skill_paths:
        base_path = Path(base_path_str).expanduser()
        if not base_path.exists():
            continue

        for skill_dir in base_path.iterdir():
            if not skill_dir.is_dir():
                continue

            skill_file = skill_dir / "SKILL.md"
            if skill_file.exists():
                skill = load_skill_from_file(skill_file)
                if skill and skill.name not in seen_names:
                    seen_names.add(skill.name)
                    skills.append(skill)
                    logger.debug(f"Loaded skill: {skill.name} from {skill_file}")

    return skills


def build_trajectory(
    events: list[dict[str, Any]],
    llm_metrics: dict[str, Any],
    model_name: str,
    system_prompt: str | None = None,
    tool_definitions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build an ATIF trajectory from conversation events.

    ``events``: dicts of type ``user_message`` / ``assistant_message`` / ``tool_result``; ``llm_metrics``: token
    and cost totals; a ``system_prompt`` becomes the first step."""
    steps: list[dict[str, Any]] = []
    step_id = 1

    for event in events:
        event_type = event.get("type", "")

        if event_type == "user_message":
            steps.append(
                {
                    "step_id": step_id,
                    "timestamp": event.get("timestamp"),
                    "source": "user",
                    "message": event.get("content", ""),
                }
            )
            step_id += 1

        elif event_type == "assistant_message":
            step: dict[str, Any] = {
                "step_id": step_id,
                "timestamp": event.get("timestamp"),
                "source": "agent",
                "message": event.get("content", ""),
                "model_name": model_name,
            }

            tool_calls = event.get("tool_calls", [])
            if tool_calls:
                step["tool_calls"] = [
                    {
                        "tool_call_id": tc.get("id", ""),
                        "function_name": tc.get("name", ""),
                        "arguments": tc.get("arguments", {}),
                    }
                    for tc in tool_calls
                ]

            token_data = event.get("token_ids")
            if token_data:
                step["metrics"] = {
                    "prompt_token_ids": token_data.get("prompt_token_ids", []),
                    "completion_token_ids": token_data.get("response_token_ids", []),
                }

            steps.append(step)
            step_id += 1

        elif event_type == "tool_result":
            # the observation belongs to the preceding agent step
            if steps and steps[-1].get("source") == "agent":
                steps[-1]["observation"] = {
                    "results": [
                        {
                            "source_call_id": event.get("tool_call_id"),
                            "content": event.get("content", ""),
                        }
                    ]
                }

    if system_prompt:
        system_step: dict[str, Any] = {
            "step_id": 0,
            "timestamp": steps[0]["timestamp"] if steps else None,
            "source": "system",
            "message": system_prompt,
        }
        steps.insert(0, system_step)

    for i, step in enumerate(steps):
        step["step_id"] = i + 1

    trajectory = {
        "schema_version": "ATIF-v1.5",
        "session_id": os.environ.get("SESSION_ID", "opera-session"),
        "agent": {
            "name": "openhands-sdk",
            "tool_definitions": tool_definitions if tool_definitions else None,
            "version": "unknown",
        },
        "steps": steps,
        "final_metrics": {
            "total_prompt_tokens": llm_metrics.get("prompt_tokens", 0),
            "total_completion_tokens": llm_metrics.get("completion_tokens", 0),
            "total_cached_tokens": llm_metrics.get("cached_tokens", 0),
            "total_cost_usd": llm_metrics.get("cost_usd", 0.0),
        },
    }

    return trajectory


def _enable_strict_tools() -> None:
    """Send every function tool as ``strict`` so vLLM constrains tool-call decoding.

    The SDK calls LiteLLM through the module-level name ``litellm_completion`` imported in
    ``openhands.sdk.llm.llm``; wrapping that name covers every request the agent makes. Only
    ``function`` tools are touched, and a tool that already carries ``strict`` is left alone.
    """
    try:
        from openhands.sdk.llm import llm as _sdk_llm
    except Exception as exc:  # pragma: no cover - SDK layout changed
        print(f"Warning: LLM_STRICT_TOOLS set but the SDK module was not importable ({exc})", file=sys.stderr)
        return
    inner = getattr(_sdk_llm, "litellm_completion", None)
    if inner is None:
        print("Warning: LLM_STRICT_TOOLS set but litellm_completion was not found on the SDK module", file=sys.stderr)
        return

    def _strict(tools: Any) -> Any:
        if not isinstance(tools, list):
            return tools
        out = []
        for tool in tools:
            fn = tool.get("function") if isinstance(tool, dict) else None
            if isinstance(fn, dict) and tool.get("type", "function") == "function" and "strict" not in fn:
                tool = {**tool, "function": {**fn, "strict": True}}
            out.append(tool)
        return out

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if kwargs.get("tools"):
            kwargs["tools"] = _strict(kwargs["tools"])
        return inner(*args, **kwargs)

    _sdk_llm.litellm_completion = wrapper
    print("LLM_STRICT_TOOLS: function tools will be sent with strict=true", file=sys.stderr)


def repair_tool_text(text: str) -> str:
    """Rebuild a prompt-mode tool call that a model wrote in a corrupted form (LLM_TOOL_TEXT_REPAIR=deepseek).

    With native tool calling off the SDK parses ``<function=NAME><parameter=K>V</parameter></function>`` out of the
    reply text; a reply that mixes DSML tokens into that format (``<｜DSML｜=terminal>``, ``</｜DSML｜>``,
    ``<terminal>`` / ``<command>`` without ``=``, a missing ``<function=…>`` line) would be read as a final answer.

    A reply that already holds a well-formed ``<function=…>…</function>`` block and no DSML token is returned
    unchanged. Otherwise the tokens are normalized, every parameter is extracted (its value runs to the next
    parameter/function marker or a closing tag, so ``</div>`` inside a command survives), the tool is the one the
    reply names or — when it names none — the one its parameters identify (``path`` → file_editor, ``command`` →
    terminal, ``message`` → finish), and the call is re-emitted in the canonical form after any leading prose.
    A reply with no recoverable parameter is returned unchanged."""
    import re

    tool_names = {"terminal", "file_editor", "task_tracker", "finish", "think"}
    aliases = {"bash": "terminal", "execute_bash": "terminal", "str_replace_editor": "file_editor"}
    well_formed = re.search(r"<function=[^>\n]+>.*?</function>", text, re.S)
    if "｜" not in text and well_formed:
        return text
    if "｜" not in text and "<parameter" not in text and not re.search(r"<(%s)>" % "|".join(tool_names | set(aliases)), text):
        return text

    OPEN, CLOSE = "\x00OPEN=", "\x00CLOSE\x00"
    s = text
    # name-attribute DSML openers:  <｜DSML｜_function name="terminal">  /  <｜DSML｜_parameter name="summary">
    s = re.sub(r"<?｜DSML｜[_a-z]*\s+name=\"([^\"]+)\"[^>]*>", lambda m: f"{OPEN}{m.group(1)}\x00", s)
    # '=' DSML openers, with or without a kind:  <｜DSML｜=terminal>  <｜DSML｜function=x>  <｜DSML｜_parameter=x>  ｜on=terminal>
    s = re.sub(r"<?｜(?:DSML｜)?[_a-z]*=([^>\s]+)>", lambda m: f"{OPEN}{m.group(1)}\x00", s)
    # every DSML closer ends the current value
    s = re.sub(r"</｜DSML｜[^>]*>", CLOSE, s)
    # leftover DSML openers without a name (<｜DSML｜r>, <｜DSML｜>) are noise
    s = re.sub(r"<｜DSML｜[^>]*>", "", s)
    # canonical openers / closers
    s = re.sub(r"<(?:function|parameter)=([^>\s]+)>", lambda m: f"{OPEN}{m.group(1)}\x00", s)
    s = re.sub(r"<parameter>([a-z_]+)>", lambda m: f"{OPEN}{m.group(1)}\x00", s)          # <parameter>summary>
    s = re.sub(r"<(%s)>" % "|".join(sorted(tool_names | set(aliases) | {"command", "path", "summary", "security_risk", "message"})),
               lambda m: f"{OPEN}{m.group(1)}\x00", s)                                    # <terminal> / <command>
    s = re.sub(r"</(?:parameter|function|%s)>" % "|".join(sorted(tool_names | set(aliases))), CLOSE, s)

    first = s.find(OPEN)
    if first < 0:
        return text
    prose = s[:first].replace(CLOSE, "").rstrip()
    body = s[first:]
    tool, params = None, []
    for m in re.finditer(re.escape(OPEN) + r"([^\x00]+)\x00(.*?)(?=\x00|$)", body, re.S):
        name, value = m.group(1).strip(), m.group(2)
        name = aliases.get(name, name)
        if name in tool_names:
            tool = tool or name
            continue
        value = value.strip("\n")
        if name and not any(k == name for k, _ in params):
            params.append((name, value))
    if not params:
        return text
    keys = {k for k, _ in params}
    if tool is None:
        tool = "file_editor" if "path" in keys else "terminal" if "command" in keys else "finish" if "message" in keys else None
    if tool is None:
        return text
    call = "\n".join([f"<function={tool}>"] + [f"<parameter={k}>{v}</parameter>" for k, v in params] + ["</function>"])
    return (prose + "\n\n" + call) if prose else call


def _enable_tool_text_repair() -> None:
    """Run ``repair_tool_text`` on every reply the SDK parses in prompt-mode tool calling.

    Hook: ``fn_call_converter._preprocess_model_output`` — the SDK's own per-model cleanup, looked up as a module
    global by ``_fix_stopword`` right before the function-call regex runs. If a future SDK drops it, the repair
    wraps ``convert_non_fncall_messages_to_fncall_messages`` instead (in the converter and in the mixin that
    imported it by name), rewriting assistant contents before they are parsed."""
    try:
        from openhands.sdk.llm.mixins import fn_call_converter as _conv
    except Exception as exc:  # pragma: no cover - SDK layout changed
        print(f"Warning: LLM_TOOL_TEXT_REPAIR set but the SDK converter was not importable ({exc})", file=sys.stderr)
        return
    inner = getattr(_conv, "_preprocess_model_output", None)
    if inner is not None:
        _conv._preprocess_model_output = lambda content: inner(repair_tool_text(content))
        print("LLM_TOOL_TEXT_REPAIR: replies are repaired before the SDK parses tool calls (_preprocess_model_output)",
              file=sys.stderr)
        return
    convert = getattr(_conv, "convert_non_fncall_messages_to_fncall_messages", None)
    if convert is None:
        print("Warning: LLM_TOOL_TEXT_REPAIR set but no SDK parsing hook was found", file=sys.stderr)
        return

    def _fix(messages: Any, *args: Any, **kwargs: Any) -> Any:
        out = []
        for m in messages:
            if isinstance(m, dict) and m.get("role") == "assistant":
                c = m.get("content")
                if isinstance(c, str):
                    m = {**m, "content": repair_tool_text(c)}
                elif isinstance(c, list):
                    m = {**m, "content": [{**p, "text": repair_tool_text(p["text"])} if isinstance(p, dict) and isinstance(p.get("text"), str) else p for p in c]}
            out.append(m)
        return convert(out, *args, **kwargs)

    _conv.convert_non_fncall_messages_to_fncall_messages = _fix
    try:
        from openhands.sdk.llm.mixins import non_native_fc as _mixin
        _mixin.convert_non_fncall_messages_to_fncall_messages = _fix
    except Exception:
        pass
    print("LLM_TOOL_TEXT_REPAIR: replies are repaired before the SDK parses tool calls (converter wrap)", file=sys.stderr)


MALFORMED_CALL_MARKERS = ("｜DSML｜", "<｜", "<invoke", "</invoke", "<parameter", "</parameter", "<function", "</function",
                          "<tool_call", "</tool_call")
MALFORMED_CALL_NOTICE = (
    "Your previous reply contained tool-call markup as plain text, so no tool was executed and nothing changed. "
    "Do not describe the call in text: issue it again as a proper tool call, exactly once, and continue the task. "
    "If you had already finished the task, call the finish tool.")


DEGENERATE_NOTICE = (
    "Your previous reply degenerated into repeated text, so no tool was executed and nothing changed. "
    "Continue the task from where you were: issue your next tool call now, or call the finish tool if the task is done.")


def looks_degenerate(text: str) -> bool:
    """A reply that collapsed into one short token repeated dozens of times ('— — — …', ' to to to …'). Such a
    reply carries no tool call, so the SDK would end the run on it."""
    import re
    return bool(re.search(r"(\S{1,12})(?:\s+\1){25,}", text))


def looks_like_malformed_tool_call(text: str) -> bool:
    """A final agent message that is really a tool call the server/SDK failed to parse: any tool-call marker left
    in the message text means the call was not executed."""
    return any(marker in text for marker in MALFORMED_CALL_MARKERS)


def last_agent_text(conversation: Any) -> str:
    """Text of the most recent agent MessageEvent ('' when the run ended on an action instead)."""
    for event in reversed(list(conversation.state.events)):
        if isinstance(event, (ActionEvent, ObservationEvent)):
            return ""
        if isinstance(event, MessageEvent) and event.source == "agent":
            content = getattr(event.llm_message, "content", None) if event.llm_message else None
            if isinstance(content, list):
                return "\n".join(getattr(c, "text", "") or "" for c in content)
            return str(content or "")
    return ""


def run_with_malformed_call_retry(conversation: Any, max_retries: int) -> int:
    """``conversation.run()``; while the run ended on a malformed tool call written as text, tell the agent the call
    was not executed and run again — at most ``max_retries`` times in a row (a successful action resets the
    count, because the next malformed ending is a new incident). Returns the number of notices sent."""
    sent = streak = 0
    conversation.run()
    while streak < max_retries:
        text = last_agent_text(conversation)
        degenerate = looks_degenerate(text)
        if not degenerate and not looks_like_malformed_tool_call(text):
            break
        sent += 1
        streak += 1
        before = sum(1 for e in conversation.state.events if isinstance(e, ActionEvent))
        print(f"LLM_RETRY_MALFORMED_CALLS: run ended on a {'degenerate reply' if degenerate else 'malformed tool call'}; re-prompting (notice {sent}, streak {streak})",
              file=sys.stderr)
        conversation.send_message(DEGENERATE_NOTICE if degenerate else MALFORMED_CALL_NOTICE)
        conversation.run()
        if sum(1 for e in conversation.state.events if isinstance(e, ActionEvent)) > before:
            streak = 0                                    # the agent acted again: a later malformed ending starts over
    return sent


def main():
    """Run the agent on ``--instruction`` and write the trajectory to ``--trajectory-path``."""
    parser = argparse.ArgumentParser(description="Run OpenHands SDK agent")
    parser.add_argument("--instruction", required=True, help="Task instruction")
    parser.add_argument("--logs-dir", required=True, help="Directory for logs")
    parser.add_argument(
        "--trajectory-path", required=True, help="Path to save trajectory"
    )
    args = parser.parse_args()

    model = os.environ.get("LLM_MODEL", "anthropic/claude-sonnet-4-5-20250929")
    api_key = os.environ.get("LLM_API_KEY")
    base_url = os.environ.get("LLM_BASE_URL")

    if not api_key:
        print("Error: LLM_API_KEY environment variable not set", file=sys.stderr)
        sys.exit(1)

    logs_dir = Path(args.logs_dir)
    logs_dir.mkdir(parents=True, exist_ok=True)

    litellm_extra_body: dict[str, Any] = {}
    extra_body_raw = os.environ.get("LITELLM_EXTRA_BODY")
    if extra_body_raw:
        litellm_extra_body = json.loads(extra_body_raw)
        logger.debug(f"LiteLLM extra body: {litellm_extra_body}")

    llm_kwargs: dict[str, Any] = {
        "model": model,
        "api_key": api_key,
        "base_url": base_url,
    }
    if litellm_extra_body:
        llm_kwargs["litellm_extra_body"] = litellm_extra_body
    temperature_raw = os.environ.get("LLM_TEMPERATURE")
    if temperature_raw:
        llm_kwargs["temperature"] = float(temperature_raw)
    if os.environ.get("LLM_TOP_P"):
        llm_kwargs["top_p"] = float(os.environ["LLM_TOP_P"])
    # always explicit: the SDK would otherwise send its default "high" top-level, which some chat templates reject
    llm_kwargs["reasoning_effort"] = os.environ.get("LLM_REASONING_EFFORT") or None
    if os.environ.get("LLM_NATIVE_TOOL_CALLING", "").lower() in {"false", "0"}:
        llm_kwargs["native_tool_calling"] = False
    for env_name, kwarg in (("LLM_MAX_INPUT_TOKENS", "max_input_tokens"), ("LLM_MAX_OUTPUT_TOKENS", "max_output_tokens"),
                            ("LLM_TIMEOUT", "timeout"), ("LLM_NUM_RETRIES", "num_retries")):
        raw = os.environ.get(env_name)
        if raw:
            llm_kwargs[kwarg] = int(raw)
    if os.environ.get("LLM_STRICT_TOOLS", "").lower() in {"1", "true", "yes"}:
        _enable_strict_tools()
    if os.environ.get("LLM_TOOL_TEXT_REPAIR", "").lower() == "deepseek" and llm_kwargs.get("native_tool_calling") is False:
        _enable_tool_text_repair()
    try:
        llm = LLM(**llm_kwargs)
    except TypeError as exc:  # older/newer SDK without one of the optional knobs
        print(f"Warning: LLM(...) rejected an option ({exc}); retrying with the basics", file=sys.stderr)
        try:
            llm = LLM(model=model, api_key=api_key, base_url=base_url, reasoning_effort=llm_kwargs["reasoning_effort"])
        except TypeError:
            llm = LLM(model=model, api_key=api_key, base_url=base_url)

    tools = [
        Tool(name=TerminalTool.name),
        Tool(name=FileEditorTool.name),
        Tool(name=TaskTrackerTool.name),
    ]

    skills: list[Skill] = []
    if os.environ.get("LOAD_SKILLS", "1") == "1":
        skill_paths_str = os.environ.get("SKILL_PATHS", "")
        if skill_paths_str:
            skill_paths = skill_paths_str.split(":")
            skills = discover_skills(skill_paths)
            logger.debug(f"Loaded {len(skills)} skills")

    agent_context = AgentContext(skills=skills)

    # MCP servers: the SDK expects a flat {name: server} mapping
    mcp_config = None
    mcp_servers_raw = os.environ.get("MCP_SERVERS_JSON")
    if mcp_servers_raw:
        mcp_servers = json.loads(mcp_servers_raw)
        mcp_config = {}
        for mcp in mcp_servers:
            server_name = mcp.get("name", "mcp-server")
            transport = mcp.get("transport", "stdio")
            server_cfg: dict[str, Any] = {}
            if transport == "stdio":
                if mcp.get("command"):
                    server_cfg["command"] = mcp["command"]
                if mcp.get("args"):
                    server_cfg["args"] = mcp["args"]
            else:
                if mcp.get("url"):
                    server_cfg["url"] = mcp["url"]
                server_cfg["transport"] = transport
            mcp_config[server_name] = server_cfg
        logger.debug(f"MCP config: {json.dumps(mcp_config, indent=2)}")

    agent_kwargs: dict[str, Any] = {
        "llm": llm,
        "tools": tools,
        "agent_context": agent_context,
    }
    if mcp_config:
        agent_kwargs["mcp_config"] = mcp_config
    # condenser: folds the middle of the history into a summary once it grows past max_size events (or max_tokens
    # tokens), keeping the first keep_first events verbatim; without one the full history is sent every step
    condenser_kind = (os.environ.get("CONDENSER") or "none").strip().lower()
    if condenser_kind not in {"none", ""}:
        try:
            from openhands.sdk.context.condenser import LLMSummarizingCondenser

            cond_kwargs: dict[str, Any] = {
                "llm": llm,
                "max_size": int(os.environ.get("CONDENSER_MAX_SIZE") or 120),
                "keep_first": int(os.environ.get("CONDENSER_KEEP_FIRST") or 4),
            }
            cond_max_tokens = os.environ.get("CONDENSER_MAX_TOKENS") or os.environ.get("LLM_MAX_INPUT_TOKENS")
            if cond_max_tokens:
                cond_kwargs["max_tokens"] = int(cond_max_tokens)
            agent_kwargs["condenser"] = LLMSummarizingCondenser(**cond_kwargs)
            print(f"Condenser: llm-summarizing (max_size={cond_kwargs['max_size']}, "
                  f"keep_first={cond_kwargs['keep_first']}, max_tokens={cond_kwargs.get('max_tokens')})", file=sys.stderr)
        except (ImportError, TypeError, ValueError) as exc:      # SDK without condensers / rejected knob
            print(f"Warning: condenser {condenser_kind!r} unavailable ({exc}); running without one", file=sys.stderr)
    try:
        agent = Agent(**agent_kwargs)
    except (TypeError, ValueError) as exc:                        # SDK whose Agent has no condenser field
        if "condenser" not in agent_kwargs:
            raise
        print(f"Warning: Agent(...) rejected the condenser ({exc}); running without one", file=sys.stderr)
        agent_kwargs.pop("condenser")
        agent = Agent(**agent_kwargs)

    # the workspace is the container's working directory
    workspace = os.getcwd()
    conv_kwargs: dict[str, Any] = {"agent": agent, "workspace": workspace}
    max_iter_raw = os.environ.get("MAX_ITERATIONS")
    if max_iter_raw:
        conv_kwargs["max_iteration_per_run"] = int(max_iter_raw)
        logger.debug(f"Max iterations per run: {max_iter_raw}")
    conversation = Conversation(**conv_kwargs)

    print(f"Starting agent with instruction: {args.instruction[:200]}...")
    print(f"Using model: {model}")
    if temperature_raw:
        print(f"Temperature: {temperature_raw}")
    if max_iter_raw:
        print(f"Max iterations per run: {max_iter_raw}")
    print(f"Loaded {len(skills)} skills")
    if mcp_config:
        print(f"MCP servers: {list(mcp_config.keys())}")

    conversation.send_message(args.instruction)
    retry_raw = os.environ.get("LLM_RETRY_MALFORMED_CALLS", "")
    if retry_raw.isdigit() and int(retry_raw) > 0:
        notices = run_with_malformed_call_retry(conversation, int(retry_raw))
        print(f"LLM_RETRY_MALFORMED_CALLS: {notices} malformed-call notice(s) sent")
    else:
        conversation.run()

    token_usage = llm.metrics.accumulated_token_usage
    metrics = {
        "prompt_tokens": token_usage.prompt_tokens if token_usage else 0,
        "completion_tokens": token_usage.completion_tokens if token_usage else 0,
        "cached_tokens": token_usage.cache_read_tokens if token_usage else 0,
        "cost_usd": llm.metrics.accumulated_cost,
    }

    system_prompt = None
    tool_definitions: list[dict[str, Any]] = []
    try:
        system_prompt = agent.static_system_message
    except Exception as e:
        logger.debug(f"Could not extract system prompt: {e}")
    try:
        for tool_name, tool_obj in agent.tools_map.items():
            tool_definitions.append(tool_obj.to_openai_tool())
    except Exception as e:
        logger.debug(f"Could not extract tool definitions: {e}")

    if system_prompt:
        print(f"Captured system prompt ({len(system_prompt)} chars)")
    print(f"Captured {len(tool_definitions)} tool definitions")

    # SDK events -> the dicts build_trajectory() takes
    events_list: list[dict[str, Any]] = []
    last_agent_timestamp: str | None = None
    for event in conversation.state.events:
        if isinstance(event, MessageEvent):
            content = ""
            if event.llm_message:
                msg_content = getattr(event.llm_message, "content", None)
                if isinstance(msg_content, list):
                    content = "\n".join(
                        getattr(c, "text", str(c))
                        for c in msg_content
                        if getattr(c, "text", None)
                    )
                elif msg_content:
                    content = str(msg_content)
            if event.source == "user":
                events_list.append(
                    {
                        "type": "user_message",
                        "content": content,
                        "timestamp": event.timestamp,
                    }
                )
            elif event.source == "agent":
                entry: dict[str, Any] = {
                    "type": "assistant_message",
                    "content": content,
                    "timestamp": event.timestamp,
                }
                events_list.append(entry)
                last_agent_timestamp = event.timestamp
        elif isinstance(event, ActionEvent):
            tool_call_args: dict[str, Any] = {}
            # arguments: the raw tool call's (OpenAI format), else the parsed action's fields
            if event.tool_call and hasattr(event.tool_call, "function"):
                raw_args = getattr(event.tool_call.function, "arguments", None)
                if isinstance(raw_args, str):
                    try:
                        tool_call_args = json.loads(raw_args)
                    except json.JSONDecodeError:
                        tool_call_args = {"raw": raw_args}
                elif isinstance(raw_args, dict):
                    tool_call_args = raw_args
            if not tool_call_args and event.action:
                try:
                    action_dict = (
                        event.action.model_dump()
                        if hasattr(event.action, "model_dump")
                        else vars(event.action)
                    )
                    tool_call_args = {
                        k: v
                        for k, v in action_dict.items()
                        if k != "kind" and v is not None
                    }
                except Exception:
                    pass
            entry = {
                "type": "assistant_message",
                "content": "",
                "timestamp": event.timestamp,
                "tool_calls": [
                    {
                        "id": event.tool_call_id,
                        "name": event.tool_name,
                        "arguments": tool_call_args,
                    }
                ],
            }
            events_list.append(entry)
            last_agent_timestamp = event.timestamp
        elif isinstance(event, ObservationEvent):
            obs_content = ""
            if event.observation:
                obs_raw = getattr(event.observation, "content", None)
                if isinstance(obs_raw, list):
                    obs_content = "\n".join(
                        getattr(c, "text", str(c))
                        for c in obs_raw
                        if getattr(c, "text", None)
                    )
                elif obs_raw:
                    obs_content = str(obs_raw)
                else:
                    obs_content = str(event.observation)
            events_list.append(
                {
                    "type": "tool_result",
                    "tool_call_id": event.tool_call_id,
                    "content": obs_content,
                    "timestamp": event.timestamp,
                }
            )
        elif isinstance(event, TokenEvent):
            if last_agent_timestamp and events_list:
                for ev in reversed(events_list):
                    if ev.get("timestamp") == last_agent_timestamp:
                        ev["token_ids"] = {
                            "prompt_token_ids": getattr(event, "prompt_token_ids", []),
                            "response_token_ids": getattr(
                                event, "response_token_ids", []
                            ),
                        }
                        break

    trajectory = build_trajectory(
        events_list,
        metrics,
        model,
        system_prompt=system_prompt,
        tool_definitions=tool_definitions,
    )

    trajectory_path = Path(args.trajectory_path)
    trajectory_path.parent.mkdir(parents=True, exist_ok=True)
    with open(trajectory_path, "w") as f:
        json.dump(trajectory, f, indent=2)

    print(f"Agent completed. Trajectory saved to {trajectory_path}")
    print(f"Total cost: ${metrics['cost_usd']:.4f}")


if __name__ == "__main__":
    main()
