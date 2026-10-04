"""The container backend of a run: ``xrlenv`` (the cluster plugin) or ``docker`` (Harbor / Pier's native Docker).

Builds the framework's EnvironmentConfig and runs the read-only preflight; nothing here imports an xrlenv client
when Docker is selected.
"""
from __future__ import annotations

import importlib
import os
import shutil
import subprocess
from typing import Any

ENVIRONMENTS = ("xrlenv", "docker")
XRLENV_IMPORT_PATHS = {
    "harbor": "xrlenv_plugins.harbor:XrlenvHarborEnvironmentCluster",
    "pier": "xrlenv_plugins.pier:XrlenvPierEnvironmentCluster",
}


def validate_environment(environment: str) -> str:
    """Return ``environment`` if it is one of ``ENVIRONMENTS``, else exit (argparse does not check YAML defaults)."""
    if environment not in ENVIRONMENTS:
        raise SystemExit(f"unknown environment {environment!r}; choose xrlenv or docker")
    return environment


def cpu_pinning_for(environment: str, requested: bool | None, *, default: bool = True) -> bool:
    """Whether to pin CPUs: ``requested`` when given, else ``default`` on xrlenv and never on docker.
    Explicit pinning on docker is an error."""
    validate_environment(environment)
    pinning = (default and environment == "xrlenv") if requested is None else requested
    if pinning and environment == "docker":
        raise SystemExit("--cpu-pinning requires --environment xrlenv; native Docker uses task CPU quotas. "
                         "Omit --cpu-pinning or use --no-cpu-pinning.")
    return bool(pinning)


def environment_record(runtime: str, environment: str, *, cpu_pinning: bool | None = None) -> dict[str, Any]:
    """The resolved backend as recorded in protocol.json. ``runtime`` is ``harbor`` or ``pier``; ``cpu_pinning``
    ``None`` means automatic (on for harbor on xrlenv)."""
    validate_environment(environment)
    if runtime not in XRLENV_IMPORT_PATHS:
        raise ValueError(f"unknown benchmark runtime {runtime!r}")
    return {"backend": environment, "runtime": runtime,
            "type": "docker" if environment == "docker" else None,
            "import_path": XRLENV_IMPORT_PATHS[runtime] if environment == "xrlenv" else None,
            "cpu_pinning": cpu_pinning_for(environment, cpu_pinning, default=runtime == "harbor")}


def build_environment_config(runtime: str, environment: str = "xrlenv", *, cpu_pinning: bool | None = None,
                             cpus_multiplier: float = 1.0, memory_multiplier: float = 1.0,
                             **overrides: Any) -> Any:
    """The ``runtime``'s (``harbor`` | ``pier``) EnvironmentConfig for ``environment``. The resource multipliers
    (1.0 = the task's own resources) are xrlenv-only; ``overrides`` are passed to EnvironmentConfig as-is."""
    record = environment_record(runtime, environment, cpu_pinning=cpu_pinning)
    kwargs: dict[str, Any] = {}
    if environment == "xrlenv":
        if record["cpu_pinning"]:
            kwargs["xrlenv_cpu_pinning"] = True
        if cpus_multiplier != 1.0:
            kwargs["xrlenv_cpu_multiplier"] = cpus_multiplier
        if memory_multiplier != 1.0:
            kwargs["xrlenv_mem_multiplier"] = memory_multiplier
    elif cpus_multiplier != 1.0 or memory_multiplier != 1.0:
        raise SystemExit("resource multipliers require --environment xrlenv; for native Docker use "
                         "--override-cpus / --override-memory-mb or the task's resource settings")
    config_class = importlib.import_module(f"{runtime}.models.trial.config").EnvironmentConfig
    return config_class(type=record["type"], import_path=record["import_path"], kwargs=kwargs, **overrides)


def require_environment(environment: str, *, require_token: bool = False, compose: bool = True) -> None:
    """Read-only checks before a run starts; dry runs must not call this.

    xrlenv: ``XRLENV_GRPC_HOST`` must be set (and ``XRLENV_CONSUMER_TOKEN`` when ``require_token``).
    docker: the CLI must reach a running daemon (and the Compose plugin when ``compose``)."""
    validate_environment(environment)
    if environment == "xrlenv":
        required = ["XRLENV_GRPC_HOST"] + (["XRLENV_CONSUMER_TOKEN"] if require_token else [])
        missing = [name for name in required if not os.environ.get(name)]
        if missing:
            raise SystemExit(f"{', '.join(missing)} not set — source opera/.env for --environment xrlenv")
        return
    if not shutil.which("docker"):
        raise SystemExit("--environment docker needs the Docker CLI on PATH and access to a running Docker daemon")
    commands = [(["docker", "info", "--format", "{{.ServerVersion}}"], "Docker daemon")]
    if compose:
        commands.append((["docker", "compose", "version", "--short"], "Docker Compose plugin"))
    for command, label in commands:
        try:
            subprocess.run(command, check=True, capture_output=True, text=True, timeout=10)
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or "").strip()[:500]
            raise SystemExit(f"--environment docker cannot access the {label}: {detail}") from None
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SystemExit(f"--environment docker cannot access the {label}: {exc}") from None


def environment_label(environment: str) -> str:
    """A short human-readable name of the backend for log lines."""
    validate_environment(environment)
    if environment == "docker":
        return "native Docker"
    return f"{os.environ.get('XRLENV_GRPC_HOST', '<unset>')}:{os.environ.get('XRLENV_GRPC_PORT', '50051')} (xrlenv)"
