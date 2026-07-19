"""Persist Matrix sync-token checkpoints across bot restarts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.matrix.sync_certification import SyncCheckpoint
from mindroom.matrix.sync_token_values import normalize_sync_token

if TYPE_CHECKING:
    from pathlib import Path

_SYNC_TOKEN_RECORD_VERSION = "mindroom-sync-token-v2"  # noqa: S105
_LEGACY_SYNC_TOKEN_RECORD_VERSION = "mindroom-sync-token-v1"  # noqa: S105


@dataclass(frozen=True)
class _SyncTokenRecord:
    """Loaded sync token plus optional durable cache checkpoint."""

    token: str
    checkpoint: SyncCheckpoint | None = None
    cache_generation: str | None = None

    @property
    def certified(self) -> bool:
        """Return whether this token carries cache-trust certification."""
        return self.checkpoint is not None

    def is_bound_to(self, cache_generation: str | None) -> bool:
        """Return whether this record was certified against the active cache."""
        return cache_generation is not None and self.cache_generation == cache_generation


def _sync_token_path(storage_path: Path, agent_name: str) -> Path:
    """Return the on-disk path for one agent's sync token."""
    return storage_path / "sync_tokens" / f"{agent_name}.token"


def _sync_token_certification_path(storage_path: Path, agent_name: str) -> Path:
    """Return the legacy marker path removed during cleanup."""
    return storage_path / "sync_tokens" / f"{agent_name}.token.certified"


def _json_object(text: str) -> dict[str, object] | None:
    """Parse one JSON object without accepting other JSON value shapes."""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _record_from_json(text: str) -> _SyncTokenRecord | None:
    """Return a token record from the JSON checkpoint format."""
    payload = _json_object(text)
    if payload is None:
        return None
    token = normalize_sync_token(payload.get("token"))
    if token is None:
        return None
    version = payload.get("version")
    if version == _LEGACY_SYNC_TOKEN_RECORD_VERSION:
        return _SyncTokenRecord(token=token)
    if version != _SYNC_TOKEN_RECORD_VERSION:
        return None
    cache_generation = payload.get("cache_generation")
    if not isinstance(cache_generation, str) or not cache_generation:
        return None
    checkpoint = SyncCheckpoint(token=token)
    return _SyncTokenRecord(
        token=token,
        checkpoint=checkpoint,
        cache_generation=cache_generation,
    )


def _record_json(checkpoint: SyncCheckpoint, *, cache_generation: str) -> str:
    """Return the durable JSON token record for one certified checkpoint."""
    payload = {
        "cache_generation": cache_generation,
        "token": checkpoint.token,
        "version": _SYNC_TOKEN_RECORD_VERSION,
    }
    return json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n"


def save_sync_token(
    storage_path: Path,
    agent_name: str,
    token: str,
    *,
    cache_generation: str,
) -> None:
    """Persist one cache-certified sync token checkpoint."""
    token_path = _sync_token_path(storage_path, agent_name)
    token_value = normalize_sync_token(token)
    if token_value is None:
        msg = "Certified sync tokens require a non-empty token"
        raise ValueError(msg)
    if not cache_generation:
        msg = "Certified sync tokens require a cache generation"
        raise ValueError(msg)
    checkpoint = SyncCheckpoint(token=token_value)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(
        _record_json(checkpoint, cache_generation=cache_generation),
        encoding="utf-8",
    )
    _sync_token_certification_path(storage_path, agent_name).unlink(missing_ok=True)


def clear_sync_token(storage_path: Path, agent_name: str) -> None:
    """Remove one persisted sync token when present."""
    token_path = _sync_token_path(storage_path, agent_name)
    certification_path = _sync_token_certification_path(storage_path, agent_name)
    token_path.unlink(missing_ok=True)
    certification_path.unlink(missing_ok=True)


def load_sync_token_record(storage_path: Path, agent_name: str) -> _SyncTokenRecord | None:
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

    token = normalize_sync_token(token_text)
    if token is None:
        return None
    return _SyncTokenRecord(token=token)
