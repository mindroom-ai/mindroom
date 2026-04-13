"""Persist Matrix sync tokens across bot restarts."""

from __future__ import annotations

import os
import tempfile
from contextlib import suppress
from pathlib import Path

from mindroom import constants


def _sync_token_path(storage_path: Path, agent_name: str) -> Path:
    """Return the on-disk path for one agent's sync token."""
    return storage_path / "sync_tokens" / f"{agent_name}.token"


def _fsync_directory(directory_path: Path) -> None:
    """Flush one directory entry update to disk."""
    flags = os.O_RDONLY
    with suppress(AttributeError):
        flags |= os.O_DIRECTORY
    directory_fd = os.open(directory_path, flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def save_sync_token(storage_path: Path, agent_name: str, token: str) -> None:
    """Persist one sync token with an atomic temp-file replace."""
    token_path = _sync_token_path(storage_path, agent_name)
    token_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=token_path.parent,
        prefix=f"{token_path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        tmp_path = Path(handle.name)
        handle.write(token)
        handle.flush()
        os.fsync(handle.fileno())

    constants.safe_replace(tmp_path, token_path)
    _fsync_directory(token_path.parent)


def load_sync_token(storage_path: Path, agent_name: str) -> str | None:
    """Load one persisted sync token, or ``None`` on first run."""
    token_path = _sync_token_path(storage_path, agent_name)
    if not token_path.is_file():
        return None
    token = token_path.read_text(encoding="utf-8").strip()
    return token or None
