"""Line-preserving helpers for CLI-managed `.env` files."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping


def env_path_for_config(config_path: str | Path) -> Path:
    """Return the `.env` path next to the active config file."""
    resolved_config_path = Path(config_path).expanduser().resolve()
    return resolved_config_path.parent / ".env"


def upsert_env_values(env_path: Path, values: Mapping[str, str]) -> Path:
    """Upsert KEY=value entries while preserving unrelated lines."""
    env_path.parent.mkdir(parents=True, exist_ok=True)
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []

    for key, value in values.items():
        _upsert_env_value(lines, key, value)

    env_path.write_text(f"{'\n'.join(lines)}\n", encoding="utf-8")
    return env_path


def _upsert_env_value(lines: list[str], key: str, value: str) -> None:
    pattern = re.compile(rf"^\s*(?:export\s+)?{re.escape(key)}\s*=")
    for idx, line in enumerate(lines):
        if pattern.match(line):
            lines[idx] = f"{key}={value}"
            return
    lines.append(f"{key}={value}")
