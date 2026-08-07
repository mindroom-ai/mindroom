"""Test helpers for unified Matrix sync continuity."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.matrix.sync_continuity import SyncContinuityStore
from mindroom.matrix.sync_token_values import SyncCheckpoint

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.matrix.sync_cache_trust import SyncCacheTrust
    from mindroom.matrix.sync_certification import SyncCertificationDecision, SyncRecoveryOutcome


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


async def certify_response(
    trust: SyncCacheTrust,
    *,
    next_batch: str | None,
    recovery: SyncRecoveryOutcome,
) -> SyncCertificationDecision:
    """Plan and apply one response back to back.

    Production keeps these two steps apart on purpose -- the durable work a
    response owes happens between them -- so this convenience lives here rather
    than on ``SyncCacheTrust``, where it would be an API nothing ships.
    """
    decision = trust.plan_response(next_batch=next_batch, recovery=recovery)
    return await trust.apply_response(decision, recovery=recovery)
