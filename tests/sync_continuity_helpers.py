"""Test helpers for unified Matrix sync continuity."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.matrix.sync_certification import SyncCheckpoint
from mindroom.matrix.sync_continuity import SyncContinuityStore

if TYPE_CHECKING:
    from pathlib import Path


def save_sync_token(
    storage_path: Path,
    agent_name: str,
    token: str,
    *,
    cache_generation: str,
) -> None:
    """Persist one checkpoint through the production continuity owner."""
    SyncContinuityStore(storage_path, agent_name).replace_checkpoint(
        SyncCheckpoint(token=token, cache_generation=cache_generation),
    )


def clear_sync_token(storage_path: Path, agent_name: str) -> None:
    """Clear one checkpoint through the production continuity owner."""
    SyncContinuityStore(storage_path, agent_name).clear_checkpoint()


def load_sync_checkpoint(storage_path: Path, agent_name: str) -> SyncCheckpoint | None:
    """Load one checkpoint through the production continuity owner."""
    return SyncContinuityStore(storage_path, agent_name).load().checkpoint
