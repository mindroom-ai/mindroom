"""Line-preserving helpers for CLI-managed `.env` files."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping


def env_path_for_config(config_path: str | Path) -> Path:
    """Return the `.env` path next to the active config file."""
    resolved_config_path = Path(config_path).expanduser().resolve()
    return resolved_config_path.parent / ".env"


def write_private_env_text(env_path: Path, content: str) -> None:
    """Write an env file that only the owning OS user can read or modify."""
    env_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(env_path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "r+", encoding="utf-8") as f:
        os.fchmod(f.fileno(), 0o600)
        f.seek(0)
        f.truncate()
        f.write(content)


def upsert_env_values(env_path: Path, values: Mapping[str, str]) -> Path:
    """Upsert KEY=value entries while preserving unrelated lines."""
    env_path.parent.mkdir(parents=True, exist_ok=True)
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []

    for key, value in values.items():
        _upsert_env_value(lines, key, value)

    write_private_env_text(env_path, f"{'\n'.join(lines)}\n")
    return env_path


def _upsert_env_value(lines: list[str], key: str, value: str) -> None:
    pattern = re.compile(rf"^\s*(?:export\s+)?{re.escape(key)}\s*=")
    for idx, line in enumerate(lines):
        if pattern.match(line):
            lines[idx] = f"{key}={value}"
            return
    lines.append(f"{key}={value}")
