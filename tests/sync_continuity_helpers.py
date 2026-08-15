"""Test helpers for unified Matrix sync continuity."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.matrix.sync_continuity import SyncContinuityStore
from mindroom.matrix.sync_token_values import SyncCheckpoint

if TYPE_CHECKING:
    from pathlib import Path


def save_sync_token(
    storage_path: Path,
    agent_name: str,
    token: str,
    *,
    store_generation: str,
) -> None:
    """Persist one checkpoint through the production continuity owner."""
    SyncContinuityStore(storage_path, agent_name)._replace_checkpoint(
        SyncCheckpoint(token=token, store_generation=store_generation),
    )


def load_sync_checkpoint(storage_path: Path, agent_name: str) -> SyncCheckpoint | None:
    """Load one checkpoint through the production continuity owner."""
    return SyncContinuityStore(storage_path, agent_name).load().checkpoint
