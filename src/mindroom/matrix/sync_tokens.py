"""Persist Matrix sync-token checkpoints across bot restarts."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from mindroom.matrix.sync_certification import SyncCheckpoint
from mindroom.matrix.sync_token_values import normalize_sync_token

if TYPE_CHECKING:
    from pathlib import Path

_SYNC_TOKEN_RECORD_VERSION = "mindroom-sync-token-v2"  # noqa: S105


def _sync_token_path(storage_path: Path, agent_name: str) -> Path:
    """Return the on-disk path for one agent's sync token."""
    return storage_path / "sync_tokens" / f"{agent_name}.token"


def _checkpoint_from_json(text: str) -> SyncCheckpoint | None:
    """Return a checkpoint from the durable JSON format."""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or payload.get("version") != _SYNC_TOKEN_RECORD_VERSION:
        return None
    token = normalize_sync_token(payload.get("token"))
    if token is None:
        return None
    cache_generation_value = payload.get("cache_generation")
    cache_generation = normalize_sync_token(cache_generation_value)
    if cache_generation is None:
        return None
    return SyncCheckpoint(token=token, cache_generation=cache_generation)


def _record_json(checkpoint: SyncCheckpoint) -> str:
    """Return the durable JSON token record for one certified checkpoint."""
    payload = {
        "cache_generation": checkpoint.cache_generation,
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
    generation_value = normalize_sync_token(cache_generation)
    if generation_value is None:
        msg = "Certified sync tokens require a non-empty cache generation"
        raise ValueError(msg)
    checkpoint = SyncCheckpoint(token=token_value, cache_generation=generation_value)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(_record_json(checkpoint), encoding="utf-8")


def clear_sync_token(storage_path: Path, agent_name: str) -> None:
    """Remove one persisted sync token when present."""
    token_path = _sync_token_path(storage_path, agent_name)
    token_path.unlink(missing_ok=True)


def load_sync_checkpoint(storage_path: Path, agent_name: str) -> SyncCheckpoint | None:
    """Load one persisted cache-certified sync checkpoint."""
    token_path = _sync_token_path(storage_path, agent_name)
    if not token_path.is_file():
        return None
    try:
        token_text = token_path.read_text(encoding="utf-8").strip()
    except UnicodeDecodeError:
        return None
    if not token_text:
        return None

    return _checkpoint_from_json(token_text)
