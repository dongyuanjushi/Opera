"""harbor's OpenHands SDK agent with opera's runner and its LLM endpoint routed through the critic proxy's URL channel."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from harbor.agents.installed.openhands_sdk import OpenHandsSDK

try:
    from plugins.harness_common import DUMMY_API_KEY, channel_base_url, resolve_proxy_url, trial_session_id
except ImportError:  # imported as a bare package (tests)
    from ..harness_common import DUMMY_API_KEY, channel_base_url, resolve_proxy_url, trial_session_id  # type: ignore[no-redef]


class CriticOpenHandsSDK(OpenHandsSDK):
    """harbor's ``OpenHandsSDK`` running opera's runner; ``name()`` is ``critic-openhands-sdk``.

    ``critic_proxy_url``: the proxy root (default ``$OPERA_CRITIC_PROXY_URL``); when set,
    ``LLM_BASE_URL = <proxy>/c/<trial>/<max_iterations>/v1``. ``max_iterations``: the SDK's iteration cap;
    <= 0 or None = the SDK's own default."""

    def __init__(self, *args: Any, critic_proxy_url: str | None = None, max_iterations: int | None = None,
                 **kwargs: Any) -> None:
        self._proxy = resolve_proxy_url(critic_proxy_url)
        if max_iterations is not None and int(max_iterations) <= 0:
            max_iterations = None
        super().__init__(*args, max_iterations=max_iterations, **kwargs)

    async def install(self, environment: Any) -> None:
        """harbor's install, then opera's runner (``openhands_sdk_runner.py``) over ``/installed-agent/run_agent.py``.

        harbor's stock runner always sends the SDK's default top-level ``reasoning_effort``; opera's sends it only
        when ``LLM_REASONING_EFFORT`` asks for it and adds the other ``LLM_*`` knobs."""
        await super().install(environment)
        runner = Path(__file__).resolve().parent / "openhands_sdk_runner.py"
        local = Path(self.logs_dir) / "run_agent.py"
        local.write_text(runner.read_text(encoding="utf-8"), encoding="utf-8")
        await environment.upload_file(source_path=local, target_path="/installed-agent/run_agent.py")
        await environment.exec(command="chmod +x /installed-agent/run_agent.py", user="root")

    @staticmethod
    def name() -> str:
        return "critic-openhands-sdk"

    def critic_base_url(self) -> str | None:
        """``<proxy>/c/<trial>/<max_iterations>/v1`` for this trial (resolved at run time), or None without a proxy."""
        if not self._proxy:
            return None
        return channel_base_url(self._proxy, trial_session_id(self.logs_dir), self._max_iterations)

    def _get_env(self, key: str, *alternatives: str) -> str | None:
        """harbor's env lookup, with ``LLM_BASE_URL`` / ``LLM_API_KEY`` answered for the proxy when one is set."""
        if self._proxy and key == "LLM_BASE_URL":
            return self.critic_base_url()
        if self._proxy and key == "LLM_API_KEY":
            # the proxy holds the real upstream key; the container only needs a placeholder
            return super()._get_env(key, *alternatives) or DUMMY_API_KEY
        return super()._get_env(key, *alternatives)
