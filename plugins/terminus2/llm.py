"""LLM clients for Terminus 2 that carry the model's reasoning between turns.

:class:`ReasoningResponsesLLM` (Responses API) re-sends each turn's reasoning items statelessly;
:class:`ThinkingChatLLM` (chat completions) re-sends signed thinking blocks.
"""
from __future__ import annotations

from typing import Any

import litellm
from harbor.llms.base import LLMResponse
from harbor.llms.lite_llm import LiteLLM

ITEMS_KEY = "_responses_items"                      # private key on harbor's assistant message dicts
INCLUDE_ENCRYPTED = "reasoning.encrypted_content"


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """``obj[key]`` for a dict, ``obj.key`` otherwise."""
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


def output_items_as_input(output: list[Any]) -> list[dict[str, Any]]:
    """The response's output items re-encoded as input items: reasoning (id, summary, encrypted_content) and
    assistant messages (output_text parts) — the shape the Responses API accepts back."""
    items: list[dict[str, Any]] = []
    for it in output or []:
        t = _get(it, "type")
        if t == "reasoning":
            item: dict[str, Any] = {"type": "reasoning", "id": _get(it, "id"),
                                    "summary": [{"type": "summary_text", "text": _get(s, "text", "")} for s in (_get(it, "summary") or [])]}
            enc = _get(it, "encrypted_content")
            if enc:
                item["encrypted_content"] = enc
            if item["id"]:
                items.append(item)
        elif t == "message":
            text = "".join(_get(p, "text", "") for p in (_get(it, "content") or []) if _get(p, "type") == "output_text")
            items.append({"type": "message", "role": "assistant", "id": _get(it, "id"),
                          "content": [{"type": "output_text", "text": text}]})
    return [i for i in items if i.get("id")] or items


class ReasoningResponsesLLM(LiteLLM):
    """harbor's LiteLLM client on the Responses API, stateless, with reasoning carry-over.

    Each reply's output items (``reasoning`` with ``encrypted_content``, the ``message``) are attached to the
    assistant message harbor's ``Chat`` appends and sent back in the next call's ``input``. A summarized history
    carries no items and falls back to plain role/content."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._pending: tuple[str, list[dict[str, Any]]] | None = None      # (assistant text, its output items)

    async def _call_responses(self, prompt: str, message_history: list[Any] = [], response_format: Any = None,
                              logging_path: Any = None, **kwargs: Any) -> LLMResponse:
        """One Responses call over the full item history; raises harbor's ``OutputLengthExceededError`` on truncation."""
        kwargs.pop("previous_response_id", None)                                   # always stateless
        # attach the previous turn's items to the assistant message harbor appended for that reply (matched by text)
        if self._pending and message_history and isinstance(message_history[-1], dict) \
                and message_history[-1].get("role") == "assistant" and ITEMS_KEY not in message_history[-1] \
                and message_history[-1].get("content") == self._pending[0]:
            message_history[-1][ITEMS_KEY] = self._pending[1]
        self._pending = None
        try:
            rk: dict[str, Any] = self._build_base_kwargs(logging_path)
            if self._reasoning_effort is not None:
                rk["reasoning"] = {"effort": self._reasoning_effort}
            max_out = self.get_model_output_limit()
            if max_out is not None:
                rk["max_output_tokens"] = max_out
            if response_format is not None:
                rk["response_format"] = response_format
            if self._temperature is not None:
                rk["temperature"] = self._temperature
            rk.update(kwargs)
            include = list(rk.get("include") or [])
            if INCLUDE_ENCRYPTED not in include:
                include.append(INCLUDE_ENCRYPTED)
            rk["include"] = include
            rk["input"] = self.build_input(message_history, prompt)
            response = await litellm.aresponses(**rk)
        except Exception as e:                                                       # noqa: BLE001 — harbor's classifier re-raises
            self._handle_litellm_error(e)
        content = ""
        for it in response.output:
            if _get(it, "type") == "message":
                for part in _get(it, "content") or []:
                    if _get(part, "type") == "output_text":
                        content += _get(part, "text", "")
        items = output_items_as_input(response.output)
        self._pending = (content, items) if items else None
        usage = self._extract_responses_usage_info(response)
        if _get(response, "status") == "incomplete":
            details = _get(response, "incomplete_details")
            if _get(details, "reason", "unknown") == "max_output_tokens":
                from harbor.llms.base import OutputLengthExceededError
                raise OutputLengthExceededError(f"Model {self._model_name} hit max_tokens limit. Response was truncated.",
                                                truncated_response=content)
        summary = [_get(s, "text", "") for it in response.output if _get(it, "type") == "reasoning" for s in (_get(it, "summary") or [])]
        return LLMResponse(content=content, reasoning_content="\n".join(t for t in summary if t) or None,
                           model_name=_get(response, "model"), usage=usage, response_id=_get(response, "id"))

    @staticmethod
    def build_input(message_history: list[Any], prompt: str) -> list[dict[str, Any]]:
        """The Responses ``input``: stored output items where a message has them, role/content otherwise, then the prompt."""
        items: list[dict[str, Any]] = []
        for msg in message_history:
            if isinstance(msg, dict) and msg.get(ITEMS_KEY):
                items.extend(msg[ITEMS_KEY])
            else:
                items.append({"role": _get(msg, "role", "user"), "content": _get(msg, "content", "")})
        items.append({"role": "user", "content": prompt})
        return items


BLOCKS_KEY = "thinking_blocks"


class ThinkingChatLLM(LiteLLM):
    """harbor's chat-completions client with carry-over of a claude model's signed thinking blocks.

    The blocks of the reply just returned are stashed and attached (``thinking_blocks``) to that assistant message
    on the next call, matched by text; a summarized history gets none and falls back to plain role/content."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._pending: tuple[str, list[dict[str, Any]]] | None = None      # (assistant text, its thinking blocks)

    def _extract_usage_info(self, response: Any) -> Any:
        # harbor's chat path hands the raw completion here right after the call: the one hook that sees the message
        try:
            msg = response["choices"][0]["message"]
            blocks = _get(msg, BLOCKS_KEY)
            if blocks:
                content = _get(msg, "content") or ""
                self._pending = (content, [dict(b) for b in blocks])
        except Exception:                                                   # noqa: BLE001 — carry-over is best effort
            pass
        return super()._extract_usage_info(response)

    async def call(self, prompt: str, message_history: list[Any] = [], response_format: Any = None,
                   logging_path: Any = None, **kwargs: Any) -> LLMResponse:
        if self._pending and message_history and isinstance(message_history[-1], dict) \
                and message_history[-1].get("role") == "assistant" and BLOCKS_KEY not in message_history[-1] \
                and message_history[-1].get("content") == self._pending[0]:
            message_history[-1][BLOCKS_KEY] = self._pending[1]
        self._pending = None
        return await super().call(prompt, message_history, response_format, logging_path, **kwargs)

