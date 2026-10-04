"""harbor's mini-swe-agent with its LLM endpoint routed through the critic proxy.

Only the endpoint seam changes; the agent, its prompts and its trajectory handling are harbor's.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any

from harbor.agents.installed.mini_swe_agent import MiniSweAgent
from harbor.agents.model_connection import ResolvedModelConnection

try:
    from plugins.harness_common import DUMMY_API_KEY, plain_base_url, resolve_proxy_url
except ImportError:  # imported as a bare package (tests)
    from ..harness_common import DUMMY_API_KEY, plain_base_url, resolve_proxy_url  # type: ignore[no-redef]


def _deep_merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    """``base`` with ``extra`` merged in recursively (``extra`` wins); neither input is modified."""
    out = dict(base)
    for k, v in extra.items():
        out[k] = _deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


class CriticMiniSweAgent(MiniSweAgent):
    """harbor's ``MiniSweAgent`` talking to the critic proxy; ``name()`` is ``critic-mini-swe-agent``.

    ``critic_proxy_url``: the proxy root (default ``$OPERA_CRITIC_PROXY_URL``; without one this is harbor's agent).
    ``step_limit``: mini's ``agent.step_limit``, also sent as ``X-Critic-Max-Turns``; <= 0 or None = no turn cap.
    ``config``: mini's config mapping."""

    def __init__(self, *args: Any, critic_proxy_url: str | None = None, step_limit: int | None = None,
                 config: dict[str, Any] | None = None, **kwargs: Any) -> None:
        self._proxy = resolve_proxy_url(critic_proxy_url)
        self._step_limit = int(step_limit) if step_limit and int(step_limit) > 0 else None
        cfg: dict[str, Any] = dict(config or {})
        if self._step_limit:
            cfg = _deep_merge(cfg, {"agent": {"step_limit": self._step_limit}})
        if self._proxy and self._step_limit:
            # mini-swe-agent forwards model_kwargs.extra_headers to litellm on every request
            cfg = _deep_merge(cfg, {"model": {"model_kwargs": {"extra_headers": {"X-Critic-Max-Turns": str(self._step_limit)}}}})
        super().__init__(*args, config=cfg or None, **kwargs)

    @staticmethod
    def name() -> str:
        return "critic-mini-swe-agent"

    @property
    def model_connection(self) -> ResolvedModelConnection:
        """harbor's model connection, re-pointed at ``<proxy>/v1`` with a placeholder key when a proxy is set."""
        access = super().model_connection
        if not self._proxy:
            return access
        url = plain_base_url(self._proxy)
        env = {**access.env, "OPENAI_BASE_URL": url, "OPENAI_API_BASE": url}
        env.setdefault("MSWEA_API_KEY", DUMMY_API_KEY)
        return replace(access, base_url=url, configured_base_url=url, api_key=access.api_key or DUMMY_API_KEY, env=env)
