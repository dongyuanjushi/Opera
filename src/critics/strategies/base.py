"""The base class every critic strategy derives from, plus the helpers the baselines share.

A strategy decides when to look (``scheduler``), what to ask (``build_messages``) and how to read the answer
(``parse`` / ``parse_review``); transport, applicability gates, the finding ledger, delivery and the event log stay
in :class:`critics.engine.CriticEngine`. A baseline that answers in prose routes it through one carrier operator
(:func:`free_text_decision`) so it uses the same typed injection path.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, ClassVar, Mapping, Sequence

from ..models import CONTINUE, CriticDecision, CriticResponseError, OperatorSet, parse_critic_decision
from ..prompts import PromptSet
from ..schedule import Scheduler
from ..transcript import Bounds, TurnState, bound_messages, flatten_rollout, latest_tracked_diff, problem_statement  # noqa: F401 (re-exported)


@dataclass
class ReviewContext:
    """Everything the engine knows at a review point, handed to :meth:`Strategy.build_messages`."""
    trigger: str                                      # the schedule's event kind (completion | stuck | idle | interval | trigger:<custom>)
    prior_finding: Mapping[str, Any] | None           # the OPEN finding, compacted (findings.Finding.prompt_view)
    finding_ledger: Sequence[Mapping[str, Any]]       # earlier findings and how they ended
    progress: Mapping[str, Any]                       # runtime facts (action counts, streaks, diff paths, …)
    evidence: Mapping[str, Any] | None                # an EvidenceProvider's repository evidence
    bounds: Bounds                                    # the transcript bound that fits the critic model's window
    memory: dict[str, Any] = field(default_factory=dict)   # per-conversation, strategy-owned, persisted with the conversation
    readiness: str | None = None                      # the readiness event (completion | idle) when this is a readiness check
    budget_context: bool = False                      # whether the review may show the agent's turn budget
    event_reason: str = ""                            # the schedule's one-line reason for this review


class Strategy:
    """One critic method. Subclasses set the class attributes and override the hooks they need."""

    #: the value of ``strategy:`` / ``--critic-strategy`` that selects it (also the job-id token)
    name: ClassVar[str]
    #: one line for ``--help`` and the manifest
    description: ClassVar[str] = ""
    #: the paper it reproduces (recorded on every event for provenance)
    reference: ClassVar[str] = ""
    #: option name -> default; an unknown ``strategy_options`` key fails at config time
    options: ClassVar[dict[str, Any]] = {}
    #: a new finding must carry a resolution criterion (the operator protocol); prose baselines carry none
    requires_criterion: ClassVar[bool] = False

    def __init__(self, operators: OperatorSet, prompts: PromptSet, *, options: Mapping[str, Any] | None = None,
                 contract: str = "") -> None:
        self.operators = operators
        self.prompts = prompts                  # the operator critic's prompts
        self.opts = self.resolve_options(options)
        self.contract = contract                # the benchmark's trusted task contract (may be empty)

    @classmethod
    def resolve_options(cls, given: Mapping[str, Any] | None) -> dict[str, Any]:
        """``cls.options`` overridden by ``given``; an unknown key raises ``ValueError``."""
        given = dict(given or {})
        unknown = sorted(set(given) - set(cls.options))
        if unknown:
            raise ValueError(f"strategy {cls.name!r}: unknown strategy_options {unknown} (known: {sorted(cls.options)})")
        return {**cls.options, **given}

    # ── the hooks ──────────────────────────────────────────────────────────────
    def scheduler(self, default: Scheduler) -> Scheduler:
        """When to look. ``default`` is the configured schedule; a strategy may wrap or replace it."""
        return default

    def build_messages(self, state: TurnState, review: ReviewContext) -> list[dict[str, str]] | None:
        """The chat messages sent to the critic model; ``None`` declines this review point (no call is made)."""
        raise NotImplementedError

    def parse(self, text: str) -> CriticDecision:
        """The critic's reply as a :class:`CriticDecision` (default: the strict operator decision parser)."""
        return parse_critic_decision(text, operators=self.operators)

    def parse_review(self, text: str, review: ReviewContext, *, logprobs: Any | None = None) -> CriticDecision:
        """:meth:`parse` with the review point in hand; override to use ``review.memory`` or the reply's token
        ``logprobs`` (see :meth:`request_logprobs`). The default ignores both."""
        return self.parse(text)

    def request_logprobs(self) -> int | None:
        """Top-k token log-probabilities to request with the review call (``None`` = none). Chat-completions endpoints
        only; on the Responses API ``parse_review`` sees ``logprobs=None``."""
        return None

    def schema(self) -> dict[str, Any] | None:
        """A strict JSON schema for ``response_format`` endpoints, or ``None``."""
        return None

    def prompt_hashes(self) -> dict[str, str]:
        """Prompt name -> sha256, recorded in the manifest and on events."""
        return {}

    def event_fields(self) -> dict[str, Any]:
        """The provenance fields stamped on every event."""
        return {"strategy": self.name, "reference": self.reference}

    @classmethod
    def build_engine(cls, cfg: Any, **kw: Any) -> Any:
        """The engine that runs this strategy (the proxy's entry point); the passthrough control overrides it."""
        from ..engine import CriticEngine        # lazy: the engine imports this package
        return CriticEngine.from_config(cfg, strategy_cls=cls, **kw)

    # ── helpers shared by the baselines ────────────────────────────────────────
    def carrier(self, name: str) -> str:
        """``name`` if it is in the operator set, else the set's default carrier; ``ValueError`` when neither exists."""
        if name not in self.operators:
            fallback = self.operators.default_carrier
            if fallback is None:
                raise ValueError(f"strategy {self.name!r}: carrier operator {name!r} is not in operator set "
                                 f"{self.operators.name!r} (choose one of {self.operators.names})")
            name = fallback
        return name


def sha256_text(text: str) -> str:
    """Hex sha256 of ``text`` (UTF-8)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def free_text_decision(text: str, *, confidence: float, operator: str) -> CriticDecision:
    """Wrap untyped guidance in the typed decision carrier (``operator`` must be one of the configured set)."""
    body = (text or "").strip()
    if not body:
        raise CriticResponseError("baseline critic returned empty guidance")
    return CriticDecision(operator, body, float(confidence), "none")


def continue_decision(text: str, confidence: float) -> CriticDecision:
    """A ``continue`` decision (nothing is delivered) carrying ``text`` as its rationale."""
    return CriticDecision(CONTINUE, (text or "").strip() or "no intervention is needed", float(confidence), "none")


def first_json_object(text: str, *, required_key: str) -> dict[str, Any] | None:
    """The first JSON object in ``text`` carrying ``required_key`` (fences / prose tolerated)."""
    decoder = json.JSONDecoder()
    for index, ch in enumerate(text or ""):
        if ch != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and required_key in value:
            return value
    return None


# ── rollout views for the baselines' prompts ──────────────────────────────────

def bounded_transcript(state: TurnState, bounds: Bounds) -> list[dict[str, str]]:
    """The audited transcript, bounded to the critic model's window (critic notes stripped)."""
    return flatten_rollout(bound_messages(state.messages, bounds))[1]


def render_rollout(transcript: Sequence[Mapping[str, str]]) -> str:
    """The transcript as ``[role] content`` blocks separated by blank lines."""
    return "\n\n".join(f"[{item.get('role', '?')}] {item.get('content', '')}" for item in transcript)


def recent_steps(transcript: Sequence[Mapping[str, str]], k: int) -> list[dict[str, str]]:
    """The last ``k`` agent steps (an assistant message plus the observations after it)."""
    cutoff, seen = 0, 0
    for pos in range(len(transcript) - 1, -1, -1):
        if transcript[pos].get("role") == "assistant":
            seen += 1
            if seen >= k:
                cutoff = pos
                break
    return [dict(item) for item in transcript[cutoff:]]


def last_action(transcript: Sequence[Mapping[str, str]]) -> tuple[str, str]:
    """The latest assistant action and the observations that followed it."""
    pos = next((i for i in range(len(transcript) - 1, -1, -1) if transcript[i].get("role") == "assistant"), None)
    if pos is None:
        return "", ""
    observation = "\n".join(item.get("content", "") for item in transcript[pos + 1:] if item.get("role") in {"tool", "user"})
    return transcript[pos].get("content", ""), observation


def tracked_diff(state: TurnState) -> str:
    """The latest maintained-source diff visible in the rollout (from the transcript layer)."""
    return latest_tracked_diff(state.transcript) or ""
