"""Persist Matrix sync tokens across bot restarts."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def _sync_token_path(storage_path: Path, agent_name: str) -> Path:
    """Return the on-disk path for one agent's sync token."""
    return storage_path / "sync_tokens" / f"{agent_name}.token"


def save_sync_token(storage_path: Path, agent_name: str, token: str) -> None:
    """Persist one sync token."""
    token_path = _sync_token_path(storage_path, agent_name)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(token, encoding="utf-8")


def clear_sync_token(storage_path: Path, agent_name: str) -> None:
    """Remove one persisted sync token when present."""
    token_path = _sync_token_path(storage_path, agent_name)
    token_path.unlink(missing_ok=True)


def load_sync_token(storage_path: Path, agent_name: str) -> str | None:
    """Load one persisted sync token, or ``None`` on first run."""
    token_path = _sync_token_path(storage_path, agent_name)
    if not token_path.is_file():
        return None
    token = token_path.read_text(encoding="utf-8").strip()
    return token or None
