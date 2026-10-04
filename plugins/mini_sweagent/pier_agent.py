"""pier's mini-swe-agent (DeepSWE) with the task-budget exec deadline and optional critic-proxy routing."""
from __future__ import annotations

from typing import Any

from pier.agents.installed.mini_swe_agent import MiniSweAgent
from pier.agents.network import allowlist_from_urls
from pier.environments.base import BaseEnvironment
from pier.models.agent.network import NetworkAllowlist

try:
    from plugins.harness_common import DUMMY_API_KEY, channel_base_url, pier_exec_timeout_s, resolve_proxy_url, trial_session_id
except ImportError:
    from ..harness_common import DUMMY_API_KEY, channel_base_url, pier_exec_timeout_s, resolve_proxy_url, trial_session_id  # type: ignore[no-redef]

__all__ = ["MiniSweAgentPier"]


class MiniSweAgentPier(MiniSweAgent):
    """pier's ``MiniSweAgent`` whose agent command carries the task's own time budget as its exec deadline.

    ``critic_proxy_url`` (default ``$OPERA_CRITIC_PROXY_URL``): when set, the LLM base URL becomes
    ``<proxy>/c/<trial>/<step_limit>/v1``. ``step_limit``: the budget reported to the critic; <= 0 or None = no cap."""

    def __init__(self, *args: Any, critic_proxy_url: str | None = None, step_limit: int | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._proxy = resolve_proxy_url(critic_proxy_url)
        self._step_limit = int(step_limit) if step_limit and int(step_limit) > 0 else None

    def exec_timeout(self) -> int | None:
        """The exec deadline (seconds) for the agent command: the task's budget plus a margin, resolved at run
        time; None = the environment's default."""
        return pier_exec_timeout_s(getattr(self, "logs_dir", None))

    def critic_base_url(self) -> str | None:
        """The proxy's URL channel for this trial, or None without a proxy."""
        if not self._proxy:
            return None
        return channel_base_url(self._proxy, trial_session_id(self.logs_dir), self._step_limit)

    def network_allowlist(self) -> NetworkAllowlist:
        # pier fixes the egress allowlist at trial start, so the proxy host must be on it
        base = super().network_allowlist()
        if not self._proxy:
            return base
        return allowlist_from_urls([self._proxy], default_domains=base.domains)

    async def run(self, instruction: str, environment: BaseEnvironment, context: Any) -> None:
        url = self.critic_base_url()
        if url:
            # pier's run() reads the endpoint from extra_env (highest precedence): repoint it at the proxy
            self._extra_env.update({"OPENAI_BASE_URL": url, "OPENAI_API_BASE": url})
            self._extra_env.setdefault("OPENAI_API_KEY", DUMMY_API_KEY)
            self._extra_env.setdefault("MSWEA_API_KEY", DUMMY_API_KEY)
        await super().run(instruction, environment, context)

    async def exec_as_agent(self, environment: BaseEnvironment, command: str, env: dict[str, str] | None = None,
                            timeout_sec: int | None = None, **kwargs: Any) -> Any:
        """pier's exec, with the task-budget deadline when the caller passes no ``timeout_sec``."""
        return await super().exec_as_agent(environment, command, env=env, timeout_sec=timeout_sec or self.exec_timeout(), **kwargs)
