"""Harness packs — one directory per agent scaffold.

A pack supplies the operator set (``operators.py``), the policy defaults (``policy.yaml``) and the prompt files
(``operator.md``, ``intervention_audit.md``, ``guidance.md``).
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ..models import OperatorSet

_HERE = Path(__file__).resolve().parent
DEFAULT_HARNESS = "swe"                                     # the pack other packs fall back to
PROMPT_FILES = ("operator.md", "intervention_audit.md")     # the files a complete pack provides


@dataclass(frozen=True)
class HarnessPack:
    """A loaded pack: its directory, operator set and ``policy.yaml`` data."""
    name: str
    dir: Path
    operators: OperatorSet
    policy: dict[str, Any]

    def prompt_path(self, filename: str) -> Path | None:
        """The pack's file for ``filename``, else None."""
        p = self.dir / filename
        return p if p.is_file() else None


def known_harnesses() -> list[str]:
    """Names of the pack directories (those containing ``operators.py``)."""
    return sorted(p.name for p in _HERE.iterdir() if p.is_dir() and (p / "operators.py").is_file())


def load_pack(name: str) -> HarnessPack:
    """Load the pack ``name``; raises FileNotFoundError for an unknown one."""
    d = _HERE / name
    if not (d / "operators.py").is_file():
        raise FileNotFoundError(f"unknown harness pack {name!r} (known: {known_harnesses()})")
    mod = importlib.import_module(f"{__name__}.{name}.operators")
    ops: OperatorSet = mod.OPERATORS
    policy = yaml.safe_load((d / "policy.yaml").read_text(encoding="utf-8")) if (d / "policy.yaml").is_file() else {}
    return HarnessPack(name, d, ops, dict(policy or {}))


# register every pack's operator set on import
from ..models import OPERATOR_SETS as _SETS  # noqa: E402
for _name in known_harnesses():
    _ops = importlib.import_module(f"{__name__}.{_name}.operators").OPERATORS
    _SETS.setdefault(_ops.name, _ops)
