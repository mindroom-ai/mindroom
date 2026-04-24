"""Persist Matrix sync tokens across bot restarts."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

_CERTIFICATION_MARKER_VERSION = "mindroom-sync-token-cache-certified-v2"


@dataclass(frozen=True)
class SyncTokenRecord:
    """Loaded sync token plus whether it was saved after cache certification."""

    token: str
    certified: bool
    thread_cache_valid_after: float | None = None


def _sync_token_path(storage_path: Path, agent_name: str) -> Path:
    """Return the on-disk path for one agent's sync token."""
    return storage_path / "sync_tokens" / f"{agent_name}.token"


def _sync_token_certification_path(storage_path: Path, agent_name: str) -> Path:
    """Return the marker path proving one token came from certified persistence."""
    return storage_path / "sync_tokens" / f"{agent_name}.token.certified"


def _sync_token_certification_digest(token: str) -> str:
    """Return the stable certification digest for a token value."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _sync_token_certification_contents(token: str, *, thread_cache_valid_after: float) -> str:
    """Return the marker contents for one certified token."""
    payload = {
        "thread_cache_valid_after": thread_cache_valid_after,
        "token_sha256": _sync_token_certification_digest(token),
        "version": _CERTIFICATION_MARKER_VERSION,
    }
    return json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n"


def _normalized_thread_cache_valid_after(value: object) -> float | None:
    """Return a safe cache-validity boundary parsed from certification metadata."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    boundary = float(value)
    if not math.isfinite(boundary):
        return None
    return boundary


def _sync_token_certification_valid_after(marker_text: str, token: str) -> float | None:
    """Return the cache boundary if a marker certifies the loaded token value."""
    try:
        payload = json.loads(marker_text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("version") != _CERTIFICATION_MARKER_VERSION:
        return None
    if payload.get("token_sha256") != _sync_token_certification_digest(token):
        return None
    return _normalized_thread_cache_valid_after(payload.get("thread_cache_valid_after"))


def save_sync_token(
    storage_path: Path,
    agent_name: str,
    token: str,
    *,
    thread_cache_valid_after: float,
) -> None:
    """Persist one sync token."""
    token_path = _sync_token_path(storage_path, agent_name)
    certification_path = _sync_token_certification_path(storage_path, agent_name)
    valid_after = _normalized_thread_cache_valid_after(thread_cache_valid_after)
    if valid_after is None:
        msg = "Certified sync tokens require a finite thread-cache valid-after boundary"
        raise ValueError(msg)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(token, encoding="utf-8")
    certification_path.write_text(
        _sync_token_certification_contents(token, thread_cache_valid_after=valid_after),
        encoding="utf-8",
    )


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
    thread_cache_valid_after: float | None = None
    if certification_path.is_file():
        try:
            marker_text = certification_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            marker_text = ""
        thread_cache_valid_after = _sync_token_certification_valid_after(marker_text, token)
    return SyncTokenRecord(
        token=token,
        certified=thread_cache_valid_after is not None,
        thread_cache_valid_after=thread_cache_valid_after,
    )
