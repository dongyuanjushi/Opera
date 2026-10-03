"""``operator`` — the default strategy: a prompted critic returning one taxonomy-typed suggestion per review.

The suggestion is gated by the applicability chain and tracked as one open finding. The system prompt is the protocol
(``harnesses/swe/operator.md``) with the benchmark's task contract; the rollout is quoted as untrusted data together
with the trusted runtime blocks (:func:`critics.prompts.build_review_messages`).
"""
from __future__ import annotations

from typing import Any, ClassVar

from ..models import decision_json_schema
from ..prompts import build_review_messages
from ..transcript import TurnState
from .base import ReviewContext, Strategy


class OperatorStrategy(Strategy):
    """The operator critic: strict JSON decision over the configured operator set, criterion required."""
    name: ClassVar[str] = "operator"
    description: ClassVar[str] = "this repository's operator critic: one taxonomy-typed, gated, ledger-tracked suggestion per review"
    reference: ClassVar[str] = "swe-rebench operator-v19"      # provenance label recorded on events; keep stable
    requires_criterion: ClassVar[bool] = True

    def build_messages(self, state: TurnState, review: ReviewContext) -> list[dict[str, str]] | None:
        return build_review_messages(
            self.prompts, state.messages, open_finding=review.prior_finding, finding_history=review.finding_ledger,
            progress=review.progress, evidence=review.evidence,
            event=review.trigger, event_reason=review.event_reason, operators=self.operators,
            turn_index=state.turn_index, max_turns=state.max_turns if review.budget_context else None, bounds=review.bounds)

    def schema(self) -> dict[str, Any] | None:
        return decision_json_schema(self.operators, finding_status=True)

    def prompt_hashes(self) -> dict[str, str]:
        return self.prompts.hashes()
