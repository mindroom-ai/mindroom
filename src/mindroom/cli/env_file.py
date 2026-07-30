"""Line-preserving helpers for CLI-managed `.env` files."""

from __future__ import annotations

import os
import re
import secrets
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
    if env_path.is_symlink():
        msg = f"Refusing to write env file through a symlink: {env_path}"
        raise ValueError(msg)

    tmp_path = env_path.with_name(f".{env_path.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        tmp_path.replace(env_path)
    finally:
        tmp_path.unlink(missing_ok=True)


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
