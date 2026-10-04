"""Model presets and the agent-harness seam shared by every benchmark.

Turns a model preset (``configs/policy-models.yaml``) plus a harness (``mini-swe-agent`` | ``openhands`` |
``terminus-2``) into the ``(agent, model_name, agent_kwargs, agent_env)`` tuple a sweep hands to harbor / pier.
"""
from __future__ import annotations

import json
import os
import signal
from pathlib import Path
from typing import Any

import yaml

OPERA_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODELS_FILE = OPERA_ROOT / "configs" / "policy-models.yaml"

HARNESSES = ("mini-swe-agent", "openhands", "terminus-2")
RUNTIMES = ("pier", "harbor")
# harbor selects the agent by import path, pier by the sweep's own names; the two ship parallel agent base
# classes, so each runtime gets the harness built against its own package.
HARBOR_IMPORT_PATHS = {"mini-swe-agent": "plugins.mini_sweagent:CriticMiniSweAgent",
                       "openhands": "plugins.openhands:CriticOpenHandsSDK",
                       "terminus-2": "plugins.terminus2:CriticTerminus2"}
TERMINUS2_STOCK = "terminus-2"                   # harbor's own agent, selected by name for the baseline arm
TERMINUS2_AFFINITY_IMPORT_PATH = "plugins.terminus2:Terminus2Xrlenv"
TERMINUS2_DEFAULT_MAX_OUTPUT_TOKENS = 16384      # the output reserve: model_info.max_input_tokens = served window - reserve
INCLUDE_ENCRYPTED_REASONING = "reasoning.encrypted_content"   # Responses API `include` for ZDR-safe reasoning carry-over
TERMINUS2_KEY_ENV = {"hosted_vllm": "HOSTED_VLLM_API_KEY", "openai": "OPENAI_API_KEY"}   # what LiteLLM reads per provider


def load_presets(path: Path = DEFAULT_MODELS_FILE) -> dict[str, dict[str, Any]]:
    """The agent-model presets of a presets YAML as ``{name: preset}``."""
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))["presets"]


def _first_env(names: list[str]) -> tuple[str | None, str | None]:
    """``(name, value)`` of the first exported, non-empty env var in ``names``; ``(None, None)`` when none is."""
    for n in names:
        v = os.environ.get(n)
        if v:
            return n, v.strip()
    return None, None


def resolve_endpoint(preset: dict[str, Any]) -> dict[str, str]:
    """The preset's endpoint from the environment: ``base_url`` (ending in ``/v1``), ``api_key`` (the preset's
    ``api_key_default`` when no key var is exported) and the names of the env vars they came from."""
    var, base = _first_env(list(preset["base_url_env"]))
    if not base:
        raise SystemExit(f"preset needs one of {preset['base_url_env']} exported (e.g. in ~/.bashrc)")
    base = base.rstrip("/")
    if not base.endswith("/v1"):
        base += "/v1"
    kvar, key = _first_env(list(preset.get("api_key_env", [])))
    if not key:
        key = preset.get("api_key_default")
    if not key:
        raise SystemExit(f"preset needs one of {preset['api_key_env']} exported (e.g. in ~/.bashrc)")
    return {"base_url": base, "api_key": key, "base_url_var": var or "", "api_key_var": kvar or "<default>"}


MINI_OBSERVATION_TEMPLATE = """{%- if output.output | length < LIMIT -%}
{
  "returncode": {{ output.returncode }},
  "output": {{ output.output | tojson }}
  {%- if output.exception_info %}, "exception_info": {{ output.exception_info | tojson }}{% endif %}
}
{%- else -%}
{
  "returncode": {{ output.returncode }},
  "output_head": {{ output.output[:HALF] | tojson }},
  "output_tail": {{ output.output[-HALF:] | tojson }},
  "elided_chars": {{ output.output | length - LIMIT }},
  "warning": "Output too long."
  {%- if output.exception_info %}, "exception_info": {{ output.exception_info | tojson }}{% endif %}
}
{%- endif -%}"""


def mini_observation_template(limit: int) -> str:
    """mini-swe-agent's observation template with the output cap set to ``limit`` characters (>= 200; the stock
    cap is 10 000). Longer outputs are shown as head + tail of ``limit // 2`` characters each."""
    limit = int(limit)
    if limit < 200:
        raise ValueError("observation cap must be at least 200 characters")
    return MINI_OBSERVATION_TEMPLATE.replace("LIMIT", str(limit)).replace("HALF", str(limit // 2))


# mini-swe-agent's stock instance template, verbatim; reproduced so a run can pass a modified prompt through
# ``agent.instance_template``.
MINI_INSTANCE_TEMPLATE_STOCK = """Please solve this issue: {{task}}

You can execute bash commands and edit files to implement the necessary changes.

## Recommended Workflow

This workflow should be done step-by-step so that you can iterate on your changes and any possible problems.

1. Analyze the codebase by finding and reading relevant files
2. Create a script to reproduce the issue
3. Edit the source code to resolve the issue
4. Verify your fix works by running your script again
5. Test edge cases to ensure your fix is robust
6. Submit your changes and finish your work by issuing the following command: `echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`.
   Do not combine it with any other command. <important>After this command, you cannot continue working on this task.</important>

## Command Execution Rules

You are operating in an environment where

1. You issue at least one command
2. The system executes the command(s) in a subshell
3. You see the result(s)
4. You write your next command(s)

Each response should include:

1. **Reasoning text** where you explain your analysis and plan
2. At least one tool call with your command

**CRITICAL REQUIREMENTS:**

- Your response SHOULD include reasoning text explaining what you're doing
- Your response MUST include AT LEAST ONE bash tool call
- Directory or environment variable changes are not persistent. Every action is executed in a new subshell.
- However, you can prefix any action with `MY_ENV_VAR=MY_VALUE cd /path/to/working/dir && ...` or write/load environment variables from files
- Submit your changes and finish your work by issuing the following command: `echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`.
  Do not combine it with any other command. <important>After this command, you cannot continue working on this task.</important>

Example of a CORRECT response:
<example_response>
I need to understand the structure of the repository first. Let me check what files are in the current directory to get a better understanding of the codebase.

[Makes bash tool call with {"command": "ls -la"} as arguments]
</example_response>

<system_information>
{{system}} {{release}} {{version}} {{machine}}
</system_information>

## Useful command examples

### Create a new file:

```bash
cat <<'EOF' > newfile.py
import numpy as np
hello = "world"
print(hello)
EOF
```

### Edit files with sed:

{%- if system == "Darwin" -%}
<important>
You are on MacOS. For all the below examples, you need to use `sed -i ''` instead of `sed -i`.
</important>
{%- endif -%}

```bash
# Replace all occurrences
sed -i 's/old_string/new_string/g' filename.py

# Replace only first occurrence
sed -i 's/old_string/new_string/' filename.py

# Replace first occurrence on line 1
sed -i '1s/old_string/new_string/' filename.py

# Replace all occurrences in lines 1-10
sed -i '1,10s/old_string/new_string/g' filename.py
```

### View file content:

```bash
# View specific lines with numbers
nl -ba filename.py | sed -n '10,20p'
```

### Any other command you want to run

```bash
anything
```
"""

# The ``state-notes`` variant: the plan must appear in the visible reply (which survives into the next turn), and
# the viewing example reads whole files instead of 10-line slices.
MINI_STATE_NOTES_BLOCK = """
**RESPONSE HEADER (required):** start every response with exactly these two visible lines, before anything else:

STATE: <one line — what you now know and have already decided, carried forward and updated from your previous STATE>
NEXT: <one line — the single next action and what you expect it to tell you>

Only your visible text is kept in the conversation; your private thinking is not. These two lines are the memory you
carry between steps, so keep them factual and current instead of re-deriving what you already established.
"""

MINI_VIEW_STOCK = """### View file content:

```bash
# View specific lines with numbers
nl -ba filename.py | sed -n '10,20p'
```"""

MINI_VIEW_WHOLE = """### View file content:

```bash
# Read the whole file in one go — prefer this over slicing the same file repeatedly
cat filename.py
```

```bash
# Very long file: read it in large sections with line numbers
nl -ba filename.py | sed -n '1,400p'
```"""

# The same rule as a per-turn reminder the proxy appends to every observation (mini renders its task prompt once).
MINI_STATE_NOTES_REMINDER = ("[reminder] Start your reply with the two required lines — STATE: <what you know and have decided> "
                             "and NEXT: <the single next action> — then make exactly one bash tool call.")

MINI_INSTANCE_PROMPTS = ("stock", "state-notes")
TURN_REMINDERS = {"state-notes": MINI_STATE_NOTES_REMINDER}


def mini_instance_template(variant: str) -> str:
    """mini-swe-agent's task prompt for ``variant``: ``stock``, or ``state-notes`` (adds the STATE/NEXT header
    requirement and replaces the 10-line viewing example with whole-file reads)."""
    if variant == "stock":
        return MINI_INSTANCE_TEMPLATE_STOCK
    if variant != "state-notes":
        raise ValueError(f"unknown mini instance prompt {variant!r} (choose from {MINI_INSTANCE_PROMPTS})")
    t = MINI_INSTANCE_TEMPLATE_STOCK
    anchor = "Example of a CORRECT response:"
    if anchor not in t or MINI_VIEW_STOCK not in t:
        raise ValueError("mini's instance template changed — update MINI_INSTANCE_TEMPLATE_STOCK")
    t = t.replace(anchor, MINI_STATE_NOTES_BLOCK.strip() + "\n\n" + anchor, 1)
    t = t.replace("<example_response>\nI need to understand",
                  "<example_response>\nSTATE: nothing inspected yet; the repo layout is unknown.\nNEXT: list the working directory to find the "
                  "project layout.\n\nI need to understand", 1)
    return t.replace(MINI_VIEW_STOCK, MINI_VIEW_WHOLE, 1)


def harness_config(harness: str, preset: dict[str, Any], endpoint: dict[str, str], *, step_limit: int,
                    extra_kwargs: dict[str, Any], extra_env: dict[str, str], pin: bool = True,
                    reasoning_effort: str | None = None, preset_name: str = "preset",
                    runtime: str = "pier", terminus: dict[str, Any] | None = None,
                    observation_chars: int | None = None, instance_prompt: str | None = None) -> tuple[str, str, dict[str, Any], dict[str, str]]:
    """``(agent, model_name, agent_kwargs, agent_env)`` for the sweep's agent seam.

    ``harness``: one of :data:`HARNESSES`. ``preset`` / ``preset_name``: the model preset and its name;
    ``endpoint``: :func:`resolve_endpoint`'s result. ``step_limit``: the agent's turn cap, <= 0 = no cap (the
    wall clock is the only budget). ``extra_kwargs`` / ``extra_env`` are merged last. ``pin=False`` floats the
    container-side packages instead of using ``plugins/harnesses.lock.yaml``. ``reasoning_effort`` is resolved
    against the preset (``none`` = the field is not sent). ``runtime``: ``pier`` returns the sweep's harness name
    and pier-shaped kwargs (``config_yaml``); ``harbor`` returns the harness's ``module:Class`` import path and
    harbor-shaped kwargs (``config`` dict). ``observation_chars`` / ``instance_prompt`` (mini-swe-agent only):
    the observation cap and the task-prompt variant (:data:`MINI_INSTANCE_PROMPTS`).

    ``terminus-2`` (harbor only) returns harbor's agent name with ``api_base``, ``model_info`` (required for
    vLLM presets; ``terminus["model_info"]``, see :func:`terminus2_model_info`), the effort transport and every
    non-None knob in ``terminus`` (``temperature``, ``parser_name``, ``use_responses_api``,
    ``record_terminal_session``, ``enable_summarize``, ``proactive_summarization_threshold``). Its agent env is
    empty: the endpoint and key live in the driver process (:func:`terminus2_host_env`).
    """
    from critics.config import resolve_reasoning_effort
    from plugins.harness_common import install_env, load_lock

    if runtime not in RUNTIMES:
        raise SystemExit(f"unknown runtime {runtime!r} (choose from {RUNTIMES})")
    model, kind = preset["model"], preset.get("kind", "vllm")
    responses = preset.get("api", "chat") == "responses"     # the preset runs on the Responses API on every harness
    effort = resolve_reasoning_effort(preset_name, preset, reasoning_effort)
    # where the effort travels: top-level (relay), or in the request body per the preset's reasoning_effort_field
    effort_field = preset.get("reasoning_effort_field")
    if effort_field == "output_config":
        # claude over chat completions: thinking + Anthropic's output_config.effort in the request body
        effort_body = {"thinking": {"type": preset.get("thinking") or "adaptive"}}
        if effort:
            effort_body["output_config"] = {"effort": effort}
    else:
        # the chat-template kwarg that carries the effort; the preset may name another (reasoning_effort_key)
        effort_key = preset.get("reasoning_effort_key") or "reasoning_effort"
        effort_body = ({"chat_template_kwargs": {effort_key: effort}} if effort_field == "chat_template_kwargs"
                       else {"reasoning_effort": effort}) if effort else None
    top_level_effort = bool(effort and kind == "relay" and effort_field != "output_config")
    lock = load_lock()
    if harness == "mini-swe-agent":
        # model_class=litellm keeps mini on /chat/completions (its default for openai/* ids is the Responses API)
        kwargs: dict[str, Any] = {"cost_limit": 0, "model_class": "litellm"}
        # mini reads step_limit 0 as "unlimited"
        cfg: dict[str, Any] = {"agent": {"step_limit": max(0, int(step_limit or 0))}}
        if observation_chars:
            cfg["model"] = {"observation_template": mini_observation_template(observation_chars)}
        if instance_prompt and instance_prompt != "stock":
            cfg["agent"]["instance_template"] = mini_instance_template(instance_prompt)
        env = {"OPENAI_BASE_URL": endpoint["base_url"], "OPENAI_API_BASE": endpoint["base_url"],
               "OPENAI_API_KEY": endpoint["api_key"], "MSWEA_API_KEY": endpoint["api_key"]}
        if pin:
            kwargs["version"] = lock["mini_swe_agent"]
        if top_level_effort:
            kwargs["model_kwargs"] = {"reasoning_effort": effort}
        elif effort_body:
            # LiteLLM's openai/ provider rejects reasoning_effort for non-OpenAI model ids, so it rides in extra_body
            cfg.setdefault("model", {})["model_kwargs"] = {"extra_body": effort_body}
        if responses:
            # mini's Responses model carries reasoning items between turns itself; encrypted reasoning must be asked for
            mk = cfg.setdefault("model", {}).setdefault("model_kwargs", {})
            mk["include"] = [INCLUDE_ENCRYPTED_REASONING]
            if effort:
                mk["reasoning"] = {"effort": effort}
            kwargs.pop("model_kwargs", None)
            cfg["model"]["model_class"] = "litellm_response"
            kwargs["model_class"] = "litellm_response"                # pier passes it on mini's CLI, which beats the YAML
        if preset.get("sampling"):
            top, body = split_sampling(preset["sampling"])
            mk = cfg.setdefault("model", {}).setdefault("model_kwargs", {})
            mk.update(top)
            if body:
                mk["extra_body"] = {**mk.get("extra_body", {}), **body}
        if runtime == "harbor":
            # harbor's MiniSweAgent takes the config as a mapping; its critic subclass needs the step budget
            kwargs.pop("model_class", None); kwargs.pop("cost_limit", None); kwargs.pop("model_kwargs", None)
            model_cfg = cfg.setdefault("model", {})
            # pin the model class and carry the effort inside the config: harbor's agent forces the Responses
            # API whenever its own reasoning_effort ctor arg is set for an openai/* model
            model_cfg.setdefault("model_class", "litellm")
            if top_level_effort and model_cfg["model_class"] == "litellm":
                model_cfg.setdefault("model_kwargs", {})["reasoning_effort"] = effort
            kwargs["config"], kwargs["step_limit"] = cfg, (int(step_limit) if step_limit and int(step_limit) > 0 else None)
            agent = HARBOR_IMPORT_PATHS["mini-swe-agent"]
        else:
            kwargs["config_yaml"] = yaml.safe_dump(cfg)
            agent = "mini-swe-agent"
        model_name = f"openai/{model}"
    elif harness == "openhands":
        # step_limit <= 0: no MAX_ITERATIONS — the SDK's own default applies
        kwargs = {"max_iterations": int(step_limit)} if step_limit and int(step_limit) > 0 else {}
        if not pin:
            kwargs["version"] = ""                            # OpenHandsPier pins from the lock unless told to float
        if top_level_effort:
            kwargs["reasoning_effort"] = effort                # -> LLM_REASONING_EFFORT
        elif effort_body:
            kwargs["litellm_extra_body"] = effort_body         # merged into the request body verbatim by the SDK
        env = {"LLM_BASE_URL": endpoint["base_url"], "LLM_API_KEY": endpoint["api_key"]}
        sampling = preset.get("sampling") or {}
        if sampling.get("temperature") is not None:
            env["LLM_TEMPERATURE"] = str(sampling["temperature"])
        if sampling.get("top_p") is not None:
            env["LLM_TOP_P"] = str(sampling["top_p"])
        if sampling.get("max_tokens") is not None:                     # per-call output cap (reasoning + answer)
            env["LLM_MAX_OUTPUT_TOKENS"] = str(sampling["max_tokens"])
        if preset.get("openhands_retry_malformed_calls"):
            # the runner re-prompts when a run ends on tool-call markup written as text
            env["LLM_RETRY_MALFORMED_CALLS"] = str(int(preset["openhands_retry_malformed_calls"]))
        prompt_tools = preset.get("openhands_native_tool_calling") is False
        if prompt_tools:
            # opt-in per preset: the SDK describes the tools in the prompt and parses the calls from the reply text
            env["LLM_NATIVE_TOOL_CALLING"] = "false"
            if runtime != "harbor":
                kwargs["native_tool_calling"] = False
            if preset.get("openhands_tool_text_repair"):
                # the runner repairs corrupted prompt-mode calls before the SDK parses them
                env["LLM_TOOL_TEXT_REPAIR"] = str(preset["openhands_tool_text_repair"])
        elif kind == "vllm":
            # vLLM constrains tool-call decoding for tool_choice="auto" only when a tool is marked strict
            env["LLM_STRICT_TOOLS"] = "1"
        _, body_extra = split_sampling(sampling)
        if body_extra:
            kwargs["litellm_extra_body"] = {**(kwargs.get("litellm_extra_body") or {}), **body_extra}
        # relay models take the SDK's own Responses path under openai/; ``openhands_provider: hosted_vllm`` on a
        # relay preset pins the SDK to /chat/completions
        provider = preset.get("openhands_provider") or ("openai" if (responses or kind == "relay") else "hosted_vllm")
        if runtime == "harbor":
            # harbor's OpenHandsSDK defaults reasoning_effort to "high": always pass ours, None included;
            # the request-body extras travel as LITELLM_EXTRA_BODY in the env
            body = kwargs.pop("litellm_extra_body", None)
            kwargs["reasoning_effort"] = effort if top_level_effort else None
            if body:
                env["LITELLM_EXTRA_BODY"] = json.dumps(body) if not isinstance(body, str) else body
            agent = HARBOR_IMPORT_PATHS["openhands"]
        else:
            agent = "openhands"
        model_name = f"{provider}/{model}"
    elif harness == "terminus-2":
        if runtime != "harbor":
            raise SystemExit("terminus-2 is a harbor agent (it runs host-side in the harbor driver); pier has no equivalent")
        t = dict(terminus or {})
        provider = "openai" if kind == "relay" else "hosted_vllm"
        kwargs = {"api_base": endpoint["base_url"]}
        info = t.pop("model_info", None)
        if provider == "hosted_vllm" and not info:
            raise SystemExit("terminus-2 on a vLLM preset needs model_info — harbor's hosted_vllm contract (max_input_tokens, "
                             "max_output_tokens, costs); the launcher probes the served window (/v1/models max_model_len) or takes --context-window")
        if info:
            kwargs["model_info"] = dict(info)
        if top_level_effort:
            kwargs["reasoning_effort"] = effort
        elif effort_body:
            kwargs["llm_call_kwargs"] = {"extra_body": effort_body}
        # preset knobs: ``interleaved_thinking`` keeps the model's reasoning_content in the chat history (harbor
        # drops it by default); ``sampling`` rides in llm_call_kwargs, and an explicit temperature wins over it
        if preset.get("interleaved_thinking"):
            kwargs["interleaved_thinking"] = True
        if preset.get("thinking_carryover") and not responses:
            kwargs["thinking_carryover"] = True                # chat completions: signed thinking blocks travel between turns
        sampling = dict(preset.get("sampling") or {})
        if t.get("temperature") is not None:
            sampling.pop("temperature", None)
        top, body = split_sampling(sampling)
        if top or body:
            call = dict(kwargs.get("llm_call_kwargs", {}))
            call.update(top)
            if body:
                call["extra_body"] = {**call.get("extra_body", {}), **body}
            kwargs["llm_call_kwargs"] = call
        if step_limit and int(step_limit) > 0:
            kwargs["max_turns"] = int(step_limit)
        if responses and t.get("use_responses_api") is None:
            t["use_responses_api"] = True                      # the preset's transport
        if t.get("use_responses_api"):
            kwargs["stateless_responses"] = True               # full history per call, no previous_response_id
            if preset.get("reasoning_carryover", True):
                kwargs["reasoning_carryover"] = True           # reasoning items (encrypted_content) travel between turns
        for key in ("temperature", "parser_name", "use_responses_api", "record_terminal_session", "enable_summarize",
                    "proactive_summarization_threshold"):
            if t.get(key) is not None:
                kwargs[key] = t[key]
        env = {}       # host-side agent: the agent env only reaches the container's tmux session
        agent = TERMINUS2_STOCK
        model_name = f"{provider}/{model}"
    else:
        raise SystemExit(f"unknown harness {harness!r} (choose from {HARNESSES})")
    if harness != "terminus-2":
        env.update(install_env())                              # container-side installs only
    kwargs.update(extra_kwargs); env.update(extra_env)
    return agent, model_name, kwargs, env


def terminus2_model_info(preset: dict[str, Any], *, context_window: int | None,
                         max_output_tokens: int = TERMINUS2_DEFAULT_MAX_OUTPUT_TOKENS) -> dict[str, Any] | None:
    """harbor's ``model_info`` for Terminus 2 (required for ``hosted_vllm/`` models), or None when no window is known.

    ``context_window``: the served window in tokens (None = the preset's ``context_window``);
    ``max_output_tokens``: the output reserve, so ``max_input_tokens`` = window - reserve. Costs are zero."""
    cw = context_window or preset.get("context_window")
    if not cw:
        return None
    return {"max_input_tokens": int(cw) - int(max_output_tokens), "max_output_tokens": int(max_output_tokens),
            "input_cost_per_token": 0.0, "output_cost_per_token": 0.0}


def terminus2_host_env(model_name: str, endpoint: dict[str, str], *, critic: bool = False) -> dict[str, str]:
    """The driver-process env a Terminus 2 run needs: the provider's API-key variable LiteLLM reads for
    ``model_name`` (``provider/model``). ``critic=True`` sets the placeholder key — the proxy owns the real one."""
    from plugins.harness_common import DUMMY_API_KEY
    provider = model_name.split("/", 1)[0] if "/" in model_name else "openai"
    var = TERMINUS2_KEY_ENV.get(provider, f"{provider.upper()}_API_KEY")
    return {var: DUMMY_API_KEY if critic else endpoint["api_key"]}


# Which agent-env vars carry the model endpoint, per harness — the ones a critic-routed run must NOT set.
CRITIC_BASE_URL_ENV = {"mini-swe-agent": ("OPENAI_BASE_URL", "OPENAI_API_BASE"), "openhands": ("LLM_BASE_URL",)}
CRITIC_KEY_ENV = {"mini-swe-agent": ("OPENAI_API_KEY", "MSWEA_API_KEY"), "openhands": ("LLM_API_KEY",)}


def context_control_env(*, condense: bool = False, condenser_max_size: int | None = None,
                        condenser_keep_first: int | None = None, max_input_tokens: int | None = None,
                        llm_timeout: int | None = None, llm_num_retries: int | None = None) -> dict[str, str]:
    """Env for the OpenHands SDK runner's context / transport ceilings; a key is set only when asked for.

    ``condense`` turns on the LLM-summarizing condenser (``condenser_max_size``: history events that trigger it,
    ``condenser_keep_first``: leading events kept verbatim); ``max_input_tokens``: the LLM's input cap;
    ``llm_timeout``: per-request timeout in seconds; ``llm_num_retries``: transport retries.
    """
    env: dict[str, str] = {}
    if condense:
        env["CONDENSER"] = "llm-summarizing"
        if condenser_max_size:
            env["CONDENSER_MAX_SIZE"] = str(condenser_max_size)
        if condenser_keep_first:
            env["CONDENSER_KEEP_FIRST"] = str(condenser_keep_first)
    if max_input_tokens:
        env["LLM_MAX_INPUT_TOKENS"] = str(max_input_tokens)
    if llm_timeout:
        env["LLM_TIMEOUT"] = str(llm_timeout)
    if llm_num_retries:
        env["LLM_NUM_RETRIES"] = str(llm_num_retries)
    return env


def critic_routed_env(harness: str, agent_env: dict[str, str]) -> dict[str, str]:
    """The agent env for a run routed through the critic proxy: the endpoint vars removed, the key replaced by a
    placeholder. Both runtimes merge the agent env over what the agent resolves itself, so a real endpoint left
    in it would bypass the proxy; the harness's critic subclass supplies the proxy URL instead."""
    from plugins.harness_common import DUMMY_API_KEY

    env = {k: v for k, v in agent_env.items() if k not in CRITIC_BASE_URL_ENV.get(harness, ())}
    for key in CRITIC_KEY_ENV.get(harness, ()):
        if key in env:
            env[key] = DUMMY_API_KEY
    return env


def install_signal_handlers() -> None:
    """Make SIGTERM behave like Ctrl-C (KeyboardInterrupt), so the runner's cleanup stops the in-flight containers
    and the critic proxy. SIGHUP is left alone (nohup ignores it)."""

    def _terminate(signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt(f"signal {signal.Signals(signum).name}")

    signal.signal(signal.SIGTERM, _terminate)


def kv_pairs(pairs: list[str]) -> dict[str, str]:
    """``["K=V", …]`` → ``{K: V}`` (split on the first ``=``)."""
    out: dict[str, str] = {}
    for kv in pairs:
        k, _, v = kv.partition("=")
        out[k] = v
    return out


# Sampling params LiteLLM forwards top-level; any other (top_k, min_p, …) rides in extra_body.
LITELLM_SAMPLING_PARAMS = ("temperature", "top_p", "presence_penalty", "frequency_penalty", "max_tokens", "seed", "stop")


def split_sampling(sampling: dict[str, Any] | None) -> tuple[dict[str, Any], dict[str, Any]]:
    """A preset's ``sampling`` → (top-level LiteLLM kwargs, extra_body entries)."""
    top = {k: v for k, v in (sampling or {}).items() if k in LITELLM_SAMPLING_PARAMS}
    body = {k: v for k, v in (sampling or {}).items() if k not in LITELLM_SAMPLING_PARAMS}
    return top, body


def coerce_scalar(value: str) -> Any:
    """``K=V`` values for agent ctor kwargs: ``true``/``false`` → bool, an int or float literal → number, else the string."""
    low = value.strip().lower()
    if low in ("true", "false"):
        return low == "true"
    for cast in (int, float):
        try:
            return cast(value)
        except ValueError:
            pass
    return value
