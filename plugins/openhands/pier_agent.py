"""The OpenHands SDK as a pier installed agent (DeepSWE): installs the SDK in the task container and runs
``openhands_sdk_runner.py``, optionally through the critic proxy. Versions come from ``plugins/harnesses.lock.yaml``.
"""
from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any

from pier.agents.installed.base import BaseInstalledAgent
from pier.agents.network import allowlist_from_urls
from pier.environments.base import BaseEnvironment
from pier.models.agent.context import AgentContext
from pier.models.agent.install import AgentInstallSpec, InstallStep
from pier.models.agent.network import NetworkAllowlist

try:
    from plugins.harness_common import DUMMY_API_KEY, channel_base_url, load_lock, pier_exec_timeout_s, resolve_proxy_url, trial_session_id
except ImportError:
    from ..harness_common import DUMMY_API_KEY, channel_base_url, load_lock, pier_exec_timeout_s, resolve_proxy_url, trial_session_id  # type: ignore[no-redef]

VENV = "/opt/openhands-sdk-venv"
RUNNER_SRC = Path(__file__).resolve().parent / "openhands_sdk_runner.py"
RUNNER_DST = "/installed-agent/run_agent.py"
TRAJECTORY = "/logs/agent/trajectory.json"
OUTPUT_LOG = "/logs/agent/openhands_sdk.txt"
_LOCK = load_lock()


class OpenHandsPier(BaseInstalledAgent):
    """OpenHands SDK agent for pier; the endpoint is ``LLM_BASE_URL`` / ``LLM_API_KEY`` in the agent env and
    ``model_name`` the LiteLLM name (``provider/model``).

    ``max_iterations``: the SDK's iteration cap, <= 0 or None = no cap. ``python_version`` / ``uv_version`` /
    ``version``: container-side pins (default: the lock; ``version=""`` floats). ``reasoning_effort``: top-level
    effort, None = not sent. ``litellm_extra_body``: dict or JSON string merged into every request body.
    ``native_tool_calling=False``: describe tools in the prompt. ``temperature``: sampling temperature.
    ``load_skills``: load skills from ``SKILL_PATHS``. ``critic_proxy_url``: the proxy root (default
    ``$OPERA_CRITIC_PROXY_URL``); when set, the LLM base URL is ``<proxy>/c/<trial>/<max_iterations>/v1``."""

    def __init__(self, max_iterations: int | None = None, python_version: str | None = None,
                 reasoning_effort: str | None = None, native_tool_calling: bool | None = None,
                 temperature: float | None = None, load_skills: bool = False, uv_version: str | None = None,
                 critic_proxy_url: str | None = None, litellm_extra_body: dict[str, Any] | str | None = None,
                 *args: Any, **kwargs: Any) -> None:
        if "version" not in kwargs:
            kwargs["version"] = _LOCK.get("openhands_sdk")
        super().__init__(*args, **kwargs)
        self._max_iterations = int(max_iterations) if max_iterations and int(max_iterations) > 0 else None
        self._python_version = str(python_version or _LOCK.get("python_sdk") or "3.12")
        self._reasoning_effort = reasoning_effort
        self._litellm_extra_body = litellm_extra_body if isinstance(litellm_extra_body, str) or not litellm_extra_body else json.dumps(litellm_extra_body)
        self._native_tool_calling = native_tool_calling
        self._temperature = temperature
        self._load_skills = load_skills
        self._uv_version = uv_version or _LOCK.get("uv", "")
        self._proxy = resolve_proxy_url(critic_proxy_url)

    def exec_timeout(self) -> int | None:
        """The exec deadline (seconds) for the agent command: the task's budget plus a margin, resolved at run
        time; None = the environment's default."""
        return pier_exec_timeout_s(getattr(self, "logs_dir", None))

    @staticmethod
    def name() -> str:
        return "openhands-sdk"

    def get_version_command(self) -> str | None:
        return f"{VENV}/bin/python -c 'import openhands.sdk; print(openhands.sdk.__version__)' 2>/dev/null"

    def parse_version(self, stdout: str) -> str:
        return stdout.strip()

    def install_spec(self) -> AgentInstallSpec:
        """The container-side install: system packages, uv, a Python venv with ``openhands-sdk`` + ``openhands-tools``."""
        spec = f"=={self._version}" if self._version else ""
        root_run = (
            "if command -v apt-get &>/dev/null; then apt-get update && apt-get install -y curl git coreutils build-essential;"
            " elif command -v apk &>/dev/null; then apk add --no-cache curl bash git coreutils build-base;"
            " elif command -v yum &>/dev/null; then yum install -y curl git coreutils gcc make;"
            " elif command -v dnf &>/dev/null; then dnf install -y curl git coreutils gcc make;"
            ' else echo "Warning: no known package manager; assuming curl/git are present" >&2; fi; '
            f"mkdir -p {VENV} /installed-agent && chmod 777 {VENV} /installed-agent"
        )
        uv_url = f"https://astral.sh/uv/{self._uv_version + '/' if self._uv_version else ''}install.sh"
        agent_run = f"""
set -euo pipefail
curl -LsSf --retry 5 --retry-delay 2 --retry-connrefused --max-time 300 {uv_url} | sh
if [ -f "$HOME/.local/bin/env" ]; then . "$HOME/.local/bin/env"; else export PATH="$HOME/.local/bin:$PATH"; fi
uv python install {self._python_version}
uv venv {VENV} --python {self._python_version} --clear
. {VENV}/bin/activate
uv pip install {shlex.quote("openhands-sdk" + spec)} {shlex.quote("openhands-tools" + spec)} fastapi
{VENV}/bin/python -c 'import openhands.sdk, openhands.tools; print("openhands-sdk", openhands.sdk.__version__)'
"""
        return AgentInstallSpec(
            agent_name=self.name(), version=self._version,
            steps=[InstallStep(user="root", env={"DEBIAN_FRONTEND": "noninteractive"}, run=root_run),
                   InstallStep(user="agent", run=agent_run)],
            verification_command=self.get_version_command(),
        )

    async def install(self, environment: BaseEnvironment) -> None:
        """Run the install steps, then upload the runner (a copy stays with the trial artifacts)."""
        await super().install(environment)
        local_copy = Path(self.logs_dir) / "run_agent.py"
        local_copy.write_text(RUNNER_SRC.read_text(encoding="utf-8"), encoding="utf-8")
        await environment.upload_file(source_path=local_copy, target_path=RUNNER_DST)
        await environment.exec(command=f"chmod 755 {RUNNER_DST}", user="root")

    def network_allowlist(self) -> NetworkAllowlist:
        urls = [v for k in ("LLM_BASE_URL", "OPENAI_BASE_URL", "OPENAI_API_BASE", "HOSTED_VLLM_API_BASE") if (v := self._get_env(k))]
        if self._proxy:   # the egress allowlist is fixed at trial start — the critic proxy host must be on it
            urls.append(self._proxy)
        return allowlist_from_urls(urls)

    def build_env(self) -> dict[str, str]:
        """The runner's process env: model, endpoint (the proxy's URL channel when set), key and the ``LLM_*`` knobs."""
        if not self.model_name:
            raise ValueError("OpenHandsPier needs model_name (LiteLLM form, e.g. hosted_vllm/Qwen3.6-27B)")
        env = self.build_process_env({"LLM_MODEL": self.model_name, "AGENT_LOGS_DIR": "/logs/agent",
                                      "TRAJECTORY_PATH": TRAJECTORY, "LOAD_SKILLS": "1" if self._load_skills else "0"})
        base_url = self._get_env("LLM_BASE_URL") or self._get_env("OPENAI_BASE_URL") or self._get_env("HOSTED_VLLM_API_BASE")
        if self._proxy:   # critic proxy: per-trial conversation id + step budget ride in the URL path
            base_url = channel_base_url(self._proxy, trial_session_id(self.logs_dir), self._max_iterations)
            # pier merges ``_extra_env`` (the agent env, which carries the real upstream URL) over the env returned
            # here, so the channel URL must live in ``_extra_env`` too
            self._extra_env["LLM_BASE_URL"] = base_url
            self._extra_env.setdefault("LLM_API_KEY", DUMMY_API_KEY)
        if base_url:
            env["LLM_BASE_URL"] = base_url
        api_key = self._get_env("LLM_API_KEY") or self._get_env("OPENAI_API_KEY") or (DUMMY_API_KEY if self._proxy else None)
        if api_key:
            env["LLM_API_KEY"] = api_key
        elif self.model_name.lower().startswith("hosted_vllm/"):
            env["LLM_API_KEY"] = "dummy-key-for-local-vllm"
        else:
            raise ValueError(f"No API key for {self.model_name!r}: set LLM_API_KEY (agent env)")
        if self._max_iterations:
            env["MAX_ITERATIONS"] = str(self._max_iterations)
        if self._reasoning_effort:
            env["LLM_REASONING_EFFORT"] = str(self._reasoning_effort)
        if self._litellm_extra_body:
            env["LITELLM_EXTRA_BODY"] = self._litellm_extra_body
        if self._native_tool_calling is False:
            env["LLM_NATIVE_TOOL_CALLING"] = "false"
        if self._temperature is not None:
            env["LLM_TEMPERATURE"] = str(self._temperature)
        if sid := getattr(self, "session_id", None):     # harbor sets it; pier's BaseAgent does not
            env["SESSION_ID"] = str(sid)
        for name, value in self._get_env_prefixed("OPENHANDS_").items():
            env[name] = value
        return env

    async def run(self, instruction: str, environment: BaseEnvironment, context: AgentContext) -> None:
        env = self.build_env()
        command = (f"{VENV}/bin/python {RUNNER_DST} --instruction={shlex.quote(instruction)} "
                   f'--logs-dir="$AGENT_LOGS_DIR" --trajectory-path="$TRAJECTORY_PATH" 2>&1 | stdbuf -oL tee {OUTPUT_LOG}')
        await self.exec_as_agent(environment, command=command, env=env, timeout_sec=self.exec_timeout())

    def populate_context_post_run(self, context: AgentContext) -> None:
        """Copy cost and token totals from the runner's ATIF ``trajectory.json`` into ``context``."""
        path = Path(self.logs_dir) / "trajectory.json"
        if not path.exists():
            self.logger.debug("no trajectory at %s", path)
            return
        try:
            fm = json.loads(path.read_text(encoding="utf-8")).get("final_metrics", {}) or {}
        except (json.JSONDecodeError, OSError) as exc:
            self.logger.warning("cannot parse %s: %s", path, exc)
            return
        context.cost_usd = fm.get("total_cost_usd")
        context.n_input_tokens = fm.get("total_prompt_tokens", 0)
        context.n_output_tokens = fm.get("total_completion_tokens", 0)
        context.n_cache_tokens = fm.get("total_cached_tokens", 0)
