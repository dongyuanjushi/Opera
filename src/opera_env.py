"""Repo root and the ``.env`` loader shared by the launchers.

``.env`` (see .env.example) holds hosts, keys and paths only; behaviour is set by CLI flags.

    from opera_env import load_dotenv, require_env
    load_dotenv(); require_env("XRLENV_BENCHMARK_CACHE")
"""
from __future__ import annotations

import os
from pathlib import Path

OPERA_ROOT = Path(__file__).resolve().parents[1]
DOTENV = OPERA_ROOT / ".env"


def load_dotenv(path: Path = DOTENV, *, override: bool = False) -> dict[str, str]:
    """Load ``KEY=VALUE`` lines from *path* into ``os.environ`` and return what was set.

    Existing shell variables win unless *override*; ``export`` prefixes and matching quotes are stripped.
    """
    loaded: dict[str, str] = {}
    if not path.is_file():
        return loaded
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if override or key not in os.environ:
            os.environ[key] = value
            loaded[key] = value
    return loaded


def require_env(*keys: str) -> None:
    """Exit with a message naming any of *keys* that is unset or empty."""
    missing = [k for k in keys if not os.environ.get(k)]
    if missing:
        raise SystemExit(
            f"missing required env {missing} — set them in {DOTENV} or the shell."
        )
