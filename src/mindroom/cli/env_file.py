"""Line-preserving helpers for CLI-managed `.env` files."""

from __future__ import annotations

import io
import os
import re
import secrets
from pathlib import Path
from typing import TYPE_CHECKING

from dotenv.parser import parse_stream

if TYPE_CHECKING:
    from collections.abc import Mapping

# str.splitlines, which reads the file back for the next upsert, breaks lines at each of these characters.
_LINE_BREAK_CHARACTERS = frozenset("\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029")


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
    assignments = {key: _env_assignment(key, value) for key, value in values.items()}
    env_path.parent.mkdir(parents=True, exist_ok=True)
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []

    for key, assignment in assignments.items():
        _upsert_env_value(lines, key, assignment)

    write_private_env_text(env_path, f"{'\n'.join(lines)}\n")
    return env_path


def _env_assignment(key: str, value: str) -> str:
    """Return a `KEY=value` line that python-dotenv, which loads `.env`, reads back as exactly `value`."""
    if "\x00" in value or not _LINE_BREAK_CHARACTERS.isdisjoint(value):
        msg = f"Refusing to write {key} to the env file: its value contains a line break or NUL character"
        raise ValueError(msg)
    # python-dotenv expands `${NAME}` in every value, quoted or not, and has no escape for it.
    if "${" not in value:
        quoted = value.replace("\\", "\\\\").replace("'", "\\'")
        for assignment in (f"{key}={value}", f"{key}='{quoted}'"):
            # Parse it before a line holding a quote, as later lines may: a quoted value ending in `\` then runs past its line.
            binding = next(parse_stream(io.StringIO(f"{assignment}\n_='")))
            if (binding.key, binding.value) == (key, value):
                return assignment
    msg = f"Refusing to write {key} to the env file: python-dotenv would not read its value back unchanged"
    raise ValueError(msg)


def _upsert_env_value(lines: list[str], key: str, assignment: str) -> None:
    """Set the first assignment of `key` and drop later ones, which dotenv would otherwise let win."""
    pattern = re.compile(rf"^\s*(?:export\s+)?{re.escape(key)}\s*=")
    matches = [idx for idx, line in enumerate(lines) if pattern.match(line)]
    if not matches:
        lines.append(assignment)
        return
    lines[matches[0]] = assignment
    for idx in reversed(matches[1:]):
        del lines[idx]
