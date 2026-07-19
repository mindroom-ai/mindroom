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


@dataclass(frozen=True)
class _SyncTokenRecord:
    """One sync checkpoint bound to the cache generation that certified it."""

    checkpoint: SyncCheckpoint
    cache_generation: str

    @property
    def token(self) -> str:
        """Return the checkpoint token."""
        return self.checkpoint.token

    def is_bound_to(self, cache_generation: str | None) -> bool:
        """Return whether this record was certified against the active cache."""
        return cache_generation is not None and self.cache_generation == cache_generation


def _sync_token_path(storage_path: Path, agent_name: str) -> Path:
    """Return the on-disk path for one agent's sync token."""
    return storage_path / "sync_tokens" / f"{agent_name}.token"


def _record_from_json(text: str) -> _SyncTokenRecord | None:
    """Return a token record from the JSON checkpoint format."""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or payload.get("version") != _SYNC_TOKEN_RECORD_VERSION:
        return None
    token = normalize_sync_token(payload.get("token"))
    if token is None:
        return None
    cache_generation = payload.get("cache_generation")
    if not isinstance(cache_generation, str) or not cache_generation:
        return None
    return _SyncTokenRecord(
        checkpoint=SyncCheckpoint(token=token),
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


def clear_sync_token(storage_path: Path, agent_name: str) -> None:
    """Remove one persisted sync token when present."""
    token_path = _sync_token_path(storage_path, agent_name)
    token_path.unlink(missing_ok=True)


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

    return _record_from_json(token_text)
