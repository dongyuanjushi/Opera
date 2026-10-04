"""harbor's OpenHands (``openhands-ai``) agent with its LLM endpoint routed through the critic proxy's URL channel."""
from __future__ import annotations

from dataclasses import replace
from typing import Any

from harbor.agents.installed.openhands import OpenHands
from harbor.agents.model_connection import ResolvedModelConnection

try:
    from plugins.harness_common import DUMMY_API_KEY, channel_base_url, resolve_proxy_url
except ImportError:
    from ..harness_common import DUMMY_API_KEY, channel_base_url, resolve_proxy_url  # type: ignore[no-redef]


class CriticOpenHands(OpenHands):
    """harbor's ``OpenHands`` with ``LLM_BASE_URL = <proxy>/c/<session>/<max_iterations>/v1``; ``name()`` is
    ``critic-openhands``.

    ``critic_proxy_url``: the proxy root (default ``$OPERA_CRITIC_PROXY_URL``; without one this is harbor's agent).
    ``max_iterations``: OpenHands' iteration cap, also the budget in the URL; <= 0 or None = no cap."""

    def __init__(self, *args: Any, critic_proxy_url: str | None = None, max_iterations: int | None = None, **kwargs: Any) -> None:
        self._proxy = resolve_proxy_url(critic_proxy_url)
        self._max_iterations = int(max_iterations) if max_iterations and int(max_iterations) > 0 else None
        if self._max_iterations:
            kwargs["max_iterations"] = self._max_iterations      # harbor turns it into MAX_ITERATIONS
        super().__init__(*args, **kwargs)

    @staticmethod
    def name() -> str:
        return "critic-openhands"

    @property
    def model_connection(self) -> ResolvedModelConnection:
        """harbor's model connection, re-pointed at the proxy's URL channel with a placeholder key when a proxy is set."""
        access = super().model_connection
        if not self._proxy:
            return access
        session = self.session_id or "openhands"
        url = channel_base_url(self._proxy, session, self._max_iterations)
        env = {**access.env, "LLM_BASE_URL": url}
        env.setdefault("LLM_API_KEY", DUMMY_API_KEY)
        return replace(access, base_url=url, configured_base_url=url, api_key=access.api_key or DUMMY_API_KEY, env=env)
