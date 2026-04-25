"""Persist Matrix sync-token checkpoints across bot restarts."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.matrix.sync_certification import SyncCheckpoint

if TYPE_CHECKING:
    from pathlib import Path

_SYNC_TOKEN_RECORD_VERSION = "mindroom-sync-token-v1"  # noqa: S105


@dataclass(frozen=True)
class SyncTokenRecord:
    """Loaded sync token plus optional durable cache checkpoint."""

    token: str
    checkpoint: SyncCheckpoint | None = None

    @property
    def certified(self) -> bool:
        """Return whether this token carries cache-trust certification."""
        return self.checkpoint is not None

    @property
    def thread_cache_valid_after(self) -> float | None:
        """Return the certified thread-cache boundary when present."""
        if self.checkpoint is None:
            return None
        return self.checkpoint.thread_cache_valid_after


def _sync_token_path(storage_path: Path, agent_name: str) -> Path:
    """Return the on-disk path for one agent's sync token."""
    return storage_path / "sync_tokens" / f"{agent_name}.token"


def _sync_token_certification_path(storage_path: Path, agent_name: str) -> Path:
    """Return the legacy marker path removed during cleanup."""
    return storage_path / "sync_tokens" / f"{agent_name}.token.certified"


def _normalized_thread_cache_valid_after(value: object) -> float | None:
    """Return a safe cache-validity boundary parsed from checkpoint metadata."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    boundary = float(value)
    if not math.isfinite(boundary):
        return None
    return boundary


def _normalized_token(value: object) -> str | None:
    """Return a safe sync token string parsed from disk."""
    if not isinstance(value, str):
        return None
    token = value.strip()
    return token or None


def _record_from_json(text: str) -> SyncTokenRecord | None:
    """Return a token record from the JSON checkpoint format."""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or payload.get("version") != _SYNC_TOKEN_RECORD_VERSION:
        return None
    token = _normalized_token(payload.get("token"))
    valid_after = _normalized_thread_cache_valid_after(payload.get("thread_cache_valid_after"))
    if token is None or valid_after is None:
        return None
    checkpoint = SyncCheckpoint(token=token, thread_cache_valid_after=valid_after)
    return SyncTokenRecord(token=token, checkpoint=checkpoint)


def _record_json(checkpoint: SyncCheckpoint) -> str:
    """Return the durable JSON token record for one certified checkpoint."""
    payload = {
        "thread_cache_valid_after": checkpoint.thread_cache_valid_after,
        "token": checkpoint.token,
        "version": _SYNC_TOKEN_RECORD_VERSION,
    }
    return json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n"


def save_sync_token(
    storage_path: Path,
    agent_name: str,
    token: str,
    *,
    thread_cache_valid_after: float,
) -> None:
    """Persist one cache-certified sync token checkpoint."""
    token_path = _sync_token_path(storage_path, agent_name)
    token_value = _normalized_token(token)
    if token_value is None:
        msg = "Certified sync tokens require a non-empty token"
        raise ValueError(msg)
    valid_after = _normalized_thread_cache_valid_after(thread_cache_valid_after)
    if valid_after is None:
        msg = "Certified sync tokens require a finite thread-cache valid-after boundary"
        raise ValueError(msg)
    checkpoint = SyncCheckpoint(token=token_value, thread_cache_valid_after=valid_after)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(_record_json(checkpoint), encoding="utf-8")
    _sync_token_certification_path(storage_path, agent_name).unlink(missing_ok=True)


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
    try:
        token_text = token_path.read_text(encoding="utf-8").strip()
    except UnicodeDecodeError:
        return None
    if not token_text:
        return None

    if token_text.lstrip().startswith("{"):
        return _record_from_json(token_text)

    token = _normalized_token(token_text)
    if token is None:
        return None
    return SyncTokenRecord(token=token)
