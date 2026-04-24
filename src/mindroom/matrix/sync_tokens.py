"""Persist Matrix sync tokens across bot restarts."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

_CERTIFICATION_MARKER_VERSION = "mindroom-sync-token-cache-certified-v1"


@dataclass(frozen=True)
class SyncTokenRecord:
    """Loaded sync token plus whether it was saved after cache certification."""

    token: str
    certified: bool


def _sync_token_path(storage_path: Path, agent_name: str) -> Path:
    """Return the on-disk path for one agent's sync token."""
    return storage_path / "sync_tokens" / f"{agent_name}.token"


def _sync_token_certification_path(storage_path: Path, agent_name: str) -> Path:
    """Return the marker path proving one token came from certified persistence."""
    return storage_path / "sync_tokens" / f"{agent_name}.token.certified"


def _sync_token_certification_digest(token: str) -> str:
    """Return the stable certification digest for a token value."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _sync_token_certification_contents(token: str) -> str:
    """Return the marker contents for one certified token."""
    return f"{_CERTIFICATION_MARKER_VERSION}\n{_sync_token_certification_digest(token)}\n"


def _sync_token_certification_matches(marker_text: str, token: str) -> bool:
    """Return whether a marker certifies the loaded token value."""
    return marker_text == _sync_token_certification_contents(token)


def save_sync_token(storage_path: Path, agent_name: str, token: str) -> None:
    """Persist one sync token."""
    token_path = _sync_token_path(storage_path, agent_name)
    certification_path = _sync_token_certification_path(storage_path, agent_name)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(token, encoding="utf-8")
    certification_path.write_text(_sync_token_certification_contents(token), encoding="utf-8")


def clear_sync_token(storage_path: Path, agent_name: str) -> None:
    """Remove one persisted sync token when present."""
    token_path = _sync_token_path(storage_path, agent_name)
    certification_path = _sync_token_certification_path(storage_path, agent_name)
    token_path.unlink(missing_ok=True)
    certification_path.unlink(missing_ok=True)


def load_sync_token(storage_path: Path, agent_name: str) -> str | None:
    """Load one persisted sync token, or ``None`` on first run."""
    record = load_sync_token_record(storage_path, agent_name)
    if record is None:
        return None
    return record.token


def load_sync_token_record(storage_path: Path, agent_name: str) -> SyncTokenRecord | None:
    """Load one persisted sync token with its certification provenance."""
    token_path = _sync_token_path(storage_path, agent_name)
    if not token_path.is_file():
        return None
    token = token_path.read_text(encoding="utf-8").strip()
    if not token:
        return None

    certification_path = _sync_token_certification_path(storage_path, agent_name)
    certified = False
    if certification_path.is_file():
        try:
            marker_text = certification_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            marker_text = ""
        certified = _sync_token_certification_matches(marker_text, token)
    return SyncTokenRecord(token=token, certified=certified)
