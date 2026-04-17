"""Persist Matrix sync tokens across bot restarts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    import structlog


def _sync_token_path(storage_path: Path, agent_name: str) -> Path:
    """Return the on-disk path for one agent's sync token."""
    return storage_path / "sync_tokens" / f"{agent_name}.token"


@dataclass(frozen=True)
class SyncTokenStore:
    """Persist one agent's Matrix sync token across restarts."""

    storage_path: Path
    agent_name: str
    logger: structlog.stdlib.BoundLogger | Any | None = None

    def save(self, token: str) -> None:
        """Persist one sync token."""
        try:
            save_sync_token(self.storage_path, self.agent_name, token)
        except OSError as exc:
            if self.logger is not None:
                self.logger.warning("matrix_sync_token_save_failed", error=str(exc))
            return

    def load(self) -> str | None:
        """Load the persisted sync token, or ``None`` on first run."""
        return load_sync_token(self.storage_path, self.agent_name)


def save_sync_token(storage_path: Path, agent_name: str, token: str) -> None:
    """Persist one sync token."""
    token_path = _sync_token_path(storage_path, agent_name)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(token, encoding="utf-8")


def load_sync_token(storage_path: Path, agent_name: str) -> str | None:
    """Load one persisted sync token, or ``None`` on first run."""
    token_path = _sync_token_path(storage_path, agent_name)
    if not token_path.is_file():
        return None
    token = token_path.read_text(encoding="utf-8").strip()
    return token or None
