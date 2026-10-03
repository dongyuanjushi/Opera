"""Critic strategy registry: ``strategy: <name>`` in a config or ``--critic-strategy`` selects a class from ``STRATEGIES``.

A new strategy is one module deriving :class:`critics.strategies.base.Strategy` (never importing another strategy),
one entry in ``STRATEGIES`` below, and its knobs in ``configs/strategies/<name>.yaml``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .agentic_rubrics import AgenticRubricsStrategy
from .base import ReviewContext, Strategy, free_text_decision
from .llm_verifier import LlmVerifierStrategy
from .operator import OperatorStrategy
from .passthrough import PassthroughEngine, PassthroughStrategy
from .swe_prm import SwePrmStrategy
from .swe_search import SweSearchStrategy

STRATEGIES: dict[str, type[Strategy]] = {cls.name: cls for cls in (
    OperatorStrategy, PassthroughStrategy,
    SwePrmStrategy, SweSearchStrategy, LlmVerifierStrategy, AgenticRubricsStrategy,
)}
KNOWN_STRATEGIES: tuple[str, ...] = tuple(STRATEGIES)


def get_strategy(name: str) -> type[Strategy]:
    """The strategy class registered under ``name``; ``ValueError`` listing the known names otherwise."""
    try:
        return STRATEGIES[name]
    except KeyError:
        raise ValueError(f"unknown critic strategy {name!r}; known: {', '.join(KNOWN_STRATEGIES)} "
                         "(a new strategy registers its class in critics.strategies.STRATEGIES)") from None


def describe_strategies() -> str:
    """One ``- name: description [reference]`` line per registered strategy (for ``--help``)."""
    return "\n".join(f"- {name}: {cls.description}" + (f" [{cls.reference}]" if cls.reference else "") for name, cls in STRATEGIES.items())


def build_engine(cfg: Any, *, complete: Any | None = None, log_dir: Path | None = None,
                 evidence_provider: Any | None = None) -> Any:
    """The engine for ``cfg.strategy`` (the proxy's entry point)."""
    return get_strategy(cfg.strategy).build_engine(cfg, complete=complete, log_dir=log_dir, evidence_provider=evidence_provider)


__all__ = ["STRATEGIES", "KNOWN_STRATEGIES", "Strategy", "ReviewContext", "PassthroughEngine",
           "build_engine", "describe_strategies", "free_text_decision", "get_strategy"]
