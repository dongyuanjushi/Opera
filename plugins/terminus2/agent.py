"""Terminus 2: harbor's official agent plus session identity and the critic proxy seam.

:class:`Terminus2Xrlenv` adds the trial session id (trajectory id, ``X-Session-ID`` header) and reasoning
carry-over; :class:`CriticTerminus2` also routes the LLM client through the critic proxy. The agent loop, prompt,
parser and tmux driver are harbor's; :func:`official_drift` checks them against the validated version.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import time
from pathlib import Path
from typing import Any

from harbor.agents.terminus_2 import Terminus2

try:
    from plugins.harness_common import plain_base_url, resolve_proxy_url, trial_agent_budget_s
except ImportError:  # imported as a bare package (tests)
    from ..harness_common import plain_base_url, resolve_proxy_url, trial_agent_budget_s  # type: ignore[no-redef]

HARBOR_VERSION_VALIDATED = "0.22.0"
TERMINUS2_AGENT_NAME = "terminus-2"
TERMINUS2_AGENT_VERSION = "2.0.0"
SESSION_HEADER = "X-Session-ID"          # harbor's own session header (session-affine routers key on it)
MAX_TURNS_HEADER = "X-Critic-Max-Turns"  # the critic proxy's per-conversation turn budget (re-read every request)
TURN_HEADER = "X-Critic-Turn"            # the agent's true episode index (survives context summarization)
REVIEW_HEADER = "X-Critic-Review"        # "off" = forward without a review (context-summarization sub-calls)
MIN_TIMED_TURNS = 3                      # turns to time before the wall-clock budget is turned into a turn budget
UNBOUNDED_TURNS = 1_000_000              # Terminus2's max_episodes when max_turns is None (no cap)
# sha256 of the validated harbor version's Terminus 2 assets: the prompt templates, the parsers, the terminal driver.
OFFICIAL_ASSETS: dict[str, str] = {
    "templates/terminus-json-plain.txt": "89a3dc3a15752b748a99fb907c9251ab31a464e13386583fb48476b078b54a34",
    "templates/timeout.txt": "32bf9aa7b157a6a0e0ba0d33b81e59dd67204276f158afc1fe6cb684ced68872",
    "terminus_json_plain_parser.py": "ba7eeaeb28e2f72ecdc5850617c77cc571692c68698306637cc2cfed41130688",
    "terminus_xml_plain_parser.py": "b36a7349ebb5db3fcac94cc61df36d8473869672f9d62fbdf5c63548861760c2",
    "tmux_session.py": "38642b794c3358809dabddfb36ea53d57fadf3e1e6fd736d82b39da8f3ce7976",
}


def terminus2_dir() -> Path:
    """The installed harbor package's Terminus 2 directory."""
    import harbor.agents.terminus_2.terminus_2 as mod
    return Path(mod.__file__).resolve().parent


def installed_harbor_version() -> str | None:
    """The installed harbor version, or None when harbor is not installed as a distribution."""
    try:
        return importlib.metadata.version("harbor")
    except importlib.metadata.PackageNotFoundError:
        return None


def asset_fingerprints(root: Path | None = None) -> dict[str, str | None]:
    """sha256 of every :data:`OFFICIAL_ASSETS` file under ``root`` (default: the installed Terminus 2); None = missing."""
    root = root or terminus2_dir()
    out: dict[str, str | None] = {}
    for rel in OFFICIAL_ASSETS:
        p = root / rel
        out[rel] = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None
    return out


def official_drift(root: Path | None = None) -> list[str]:
    """Why the installed Terminus 2 is NOT the version this harness was validated against (empty = official)."""
    drift: list[str] = []
    v = installed_harbor_version()
    if v != HARBOR_VERSION_VALIDATED:
        drift.append(f"harbor {v} installed; the harness was validated against {HARBOR_VERSION_VALIDATED}")
    if Terminus2.name() != TERMINUS2_AGENT_NAME:
        drift.append(f"harbor's agent name is {Terminus2.name()!r}, expected {TERMINUS2_AGENT_NAME!r}")
    for rel, sha in asset_fingerprints(root).items():
        if sha != OFFICIAL_ASSETS[rel]:
            drift.append(f"{rel}: sha256 {sha} != validated {OFFICIAL_ASSETS[rel]}")
    return drift


def protocol_record() -> dict[str, Any]:
    """What ran, for a job's protocol.json: harbor + agent versions, asset fingerprints, drift (empty = official)."""
    return {"harbor_version": installed_harbor_version(), "harbor_version_validated": HARBOR_VERSION_VALIDATED,
            "agent_name": TERMINUS2_AGENT_NAME, "agent_version": TERMINUS2_AGENT_VERSION,
            "assets_sha256": asset_fingerprints(), "drift": official_drift()}


class Terminus2Xrlenv(Terminus2):
    """harbor's Terminus 2 with the trial's session id as the trajectory id; ``name()`` stays ``terminus-2``.

    ``session_affinity``: send the session id as the ``X-Session-ID`` header of every LLM request.
    ``stateless_responses``: Responses API — send the full history on every call instead of chaining on
    ``previous_response_id``. ``reasoning_carryover``: Responses API — keep the model's reasoning items between
    turns (:class:`~plugins.terminus2.llm.ReasoningResponsesLLM`). ``thinking_carryover``: chat
    completions — keep signed thinking blocks between turns (:class:`~plugins.terminus2.llm.ThinkingChatLLM`).
    ``max_turns`` < 1 means no turn cap."""

    def __init__(self, *args: Any, session_affinity: bool = True, stateless_responses: bool = True,
                 reasoning_carryover: bool = False, thinking_carryover: bool = False, **kwargs: Any) -> None:
        if kwargs.get("max_turns") is not None and int(kwargs["max_turns"]) < 1:
            kwargs["max_turns"] = None
        self._session_affinity = bool(session_affinity)
        self._reasoning_carryover = bool(reasoning_carryover)
        self._thinking_carryover = bool(thinking_carryover)
        self._stateless_responses = bool(stateless_responses)
        super().__init__(*args, **kwargs)

    @staticmethod
    def name() -> str:
        return Terminus2.name()

    def _trial_session_id(self) -> str | None:
        return self._user_provided_session_id or self.session_id

    def _set_request_header(self, name: str, value: str | None) -> None:
        """Set (or, with ``value`` None, remove) a header harbor's LLM client sends on every call."""
        headers = self._llm_call_kwargs.setdefault("extra_headers", {})
        if not isinstance(headers, dict):
            raise ValueError("llm_call_kwargs.extra_headers must be a mapping")
        if value is None:
            headers.pop(name, None)
        else:
            headers[name] = value

    def request_headers(self) -> dict[str, str]:
        """The extra headers currently sent with every LLM request."""
        return dict(self._llm_call_kwargs.get("extra_headers") or {})

    def _reset_per_run_state(self) -> None:
        super()._reset_per_run_state()
        sid = self._trial_session_id()
        if sid:
            self._session_id = sid                     # trajectory / summarization-subagent ids
            if self._session_affinity:
                self._set_request_header(SESSION_HEADER, sid)

    def _init_llm(self, *args: Any, **kwargs: Any):
        """harbor's LLM client, replaced by the carry-over client when one is enabled for the LiteLLM backend."""
        if self._reasoning_carryover and kwargs.get("use_responses_api") and \
                str(getattr(kwargs.get("llm_backend"), "value", kwargs.get("llm_backend"))) == "litellm":
            from .llm import ReasoningResponsesLLM
            constructor = dict(kwargs.get("llm_kwargs") or {})
            constructor.pop("litellm_debug", None)
            if kwargs.get("temperature") is not None:
                constructor["temperature"] = kwargs["temperature"]
            return ReasoningResponsesLLM(model_name=kwargs["model_name"], api_base=kwargs.get("api_base"),
                                         collect_rollout_details=kwargs.get("collect_rollout_details", False),
                                         session_id=kwargs.get("session_id"), max_thinking_tokens=kwargs.get("max_thinking_tokens"),
                                         reasoning_effort=kwargs.get("reasoning_effort"), model_info=kwargs.get("model_info"),
                                         use_responses_api=True, **constructor)
        if self._thinking_carryover and not kwargs.get("use_responses_api") and \
                str(getattr(kwargs.get("llm_backend"), "value", kwargs.get("llm_backend"))) == "litellm":
            from .llm import ThinkingChatLLM
            constructor = dict(kwargs.get("llm_kwargs") or {})
            constructor.pop("litellm_debug", None)
            if kwargs.get("temperature") is not None:
                constructor["temperature"] = kwargs["temperature"]
            return ThinkingChatLLM(model_name=kwargs["model_name"], api_base=kwargs.get("api_base"),
                                   collect_rollout_details=kwargs.get("collect_rollout_details", False),
                                   session_id=kwargs.get("session_id"), max_thinking_tokens=kwargs.get("max_thinking_tokens"),
                                   reasoning_effort=kwargs.get("reasoning_effort"), model_info=kwargs.get("model_info"),
                                   use_responses_api=False, **constructor)
        return super()._init_llm(*args, **kwargs)

    async def _query_llm(self, chat: Any, prompt: str, original_instruction: str = "", session: Any = None):
        if self._stateless_responses and hasattr(chat, "reset_response_chain"):
            chat.reset_response_chain()               # never chain on previous_response_id
        return await super()._query_llm(chat, prompt, original_instruction, session)


class CriticTerminus2(Terminus2Xrlenv):
    """Terminus 2 with its LLM client routed through the critic proxy; ``name()`` is ``critic-terminus-2``.

    ``critic_proxy_url``: the proxy root (default ``$OPERA_CRITIC_PROXY_URL``; without one this is
    :class:`Terminus2Xrlenv`). ``critic_max_turns``: the turn budget reported to the critic, <= 0 or None = none.
    Each request carries ``X-Critic-Turn`` (the episode index) and ``X-Critic-Max-Turns`` (the turn cap or the
    wall-clock estimate, whichever is lower); context-summarization sub-calls carry ``X-Critic-Review: off``."""

    def __init__(self, *args: Any, critic_proxy_url: str | None = None, critic_max_turns: int | None = None,
                 **kwargs: Any) -> None:
        self._proxy = resolve_proxy_url(critic_proxy_url)
        self._critic_max_turns = int(critic_max_turns) if critic_max_turns and int(critic_max_turns) > 0 else None
        self._run_started: float | None = None
        self._wall_budget_s: float | None = None
        self._clock = time.monotonic
        super().__init__(*args, **kwargs)
        if self._proxy:
            # harbor's LiteLLM client reads api_base on every call
            self._llm._api_base = self.critic_base_url()
            budget = self.turn_budget()
            if budget:
                self._set_request_header(MAX_TURNS_HEADER, str(budget))

    @staticmethod
    def name() -> str:
        return "critic-terminus-2"

    def critic_base_url(self) -> str | None:
        """``<proxy>/v1``, or None without a proxy."""
        return plain_base_url(self._proxy) if self._proxy else None

    def turn_budget(self) -> int | None:
        """The turn budget reported to the critic: ``critic_max_turns``, else Terminus 2's own ``max_turns`` cap,
        else None."""
        if self._critic_max_turns:
            return self._critic_max_turns
        return self._max_episodes if self._max_episodes < UNBOUNDED_TURNS else None

    def _reset_per_run_state(self) -> None:
        super()._reset_per_run_state()
        self._run_started = self._clock()
        self._wall_budget_s = trial_agent_budget_s(self.logs_dir)

    def estimated_turn_budget(self, *, now: float | None = None) -> int | None:
        """The turn index at which the trial's wall clock runs out, from the mean turn latency so far; None until
        :data:`MIN_TIMED_TURNS` turns have been timed or when the budget is unknown. ``now``: a clock reading
        (default: the current time)."""
        if not self._wall_budget_s or self._run_started is None:
            return None
        done = self._n_episodes - 1                                    # inferences completed before this one
        if done < MIN_TIMED_TURNS:
            return None
        elapsed = (self._clock() if now is None else now) - self._run_started
        if elapsed <= 0:
            return None
        remaining = max(0.0, self._wall_budget_s - elapsed)
        return self._n_episodes + int(remaining / (elapsed / done))

    async def _query_llm(self, chat: Any, prompt: str, original_instruction: str = "", session: Any = None):
        """Per-request critic headers, then harbor's own (retrying) query."""
        if self._proxy:
            self._set_request_header(TURN_HEADER, str(self._n_episodes))
            # the binding budget: the turn cap or the wall clock, whichever ends first
            candidates = [b for b in (self.turn_budget(), self.estimated_turn_budget()) if b]
            budget = min(candidates) if candidates else None
            self._set_request_header(MAX_TURNS_HEADER, str(budget) if budget else None)
        return await super()._query_llm(chat, prompt, original_instruction, session)

    async def _run_subagent(self, *args: Any, **kwargs: Any):
        """Context-summarization sub-calls are not agent turns: ask the proxy to forward them unreviewed."""
        if not self._proxy:
            return await super()._run_subagent(*args, **kwargs)
        self._set_request_header(REVIEW_HEADER, "off")
        try:
            return await super()._run_subagent(*args, **kwargs)
        finally:
            self._set_request_header(REVIEW_HEADER, None)
