"""``passthrough`` — the no-critic control: every request is forwarded unmodified; nothing is reviewed or logged.

It runs through the same launcher, proxy and job naming as every other strategy.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

from ..transcript import TurnState
from .base import ReviewContext, Strategy


@dataclass
class PassthroughOutcome:
    """The engine outcome for an unreviewed turn: the messages unchanged, nothing injected."""
    messages: list[dict[str, Any]]
    injected: str | None = None
    event: Any = field(default_factory=lambda: SimpleNamespace(delivery={}, applied=False))


class PassthroughEngine:
    """The proxy's engine protocol (``review`` / ``prompt_hashes`` / ``_conversations``) without a critic."""

    def __init__(self, cfg: Any, *, log_dir: Path | None = None) -> None:
        self.cfg = cfg
        self.strategy = PassthroughStrategy
        self.prompt_hashes: dict[str, str] = {}
        self._conversations: dict[str, int] = {}
        self.log_dir = log_dir

    def review(self, state: TurnState) -> PassthroughOutcome:
        self._conversations[state.conversation] = state.turn_index
        return PassthroughOutcome(messages=[dict(m) for m in state.messages])


class PassthroughStrategy(Strategy):
    """Declines every review point and builds a :class:`PassthroughEngine`."""
    name: ClassVar[str] = "passthrough"
    description: ClassVar[str] = "no review; the A/B control (the proxy forwards every request unmodified)"

    def build_messages(self, state: TurnState, review: ReviewContext) -> list[dict[str, str]] | None:
        return None

    @classmethod
    def build_engine(cls, cfg: Any, **kw: Any) -> PassthroughEngine:
        return PassthroughEngine(cfg, log_dir=kw.get("log_dir"))
