"""Test helpers for unified Matrix sync continuity."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from mindroom.event_journal import HistoryRecoveryState, RoomHistoryRecovery
from mindroom.matrix.sync_continuity import SyncContinuityStore
from mindroom.matrix.sync_token_values import SyncCheckpoint

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.matrix.sync_certification import SyncCertificationDecision, SyncRecoveryOutcome
    from mindroom.matrix.sync_checkpoint_trust import SyncCheckpointTrust


@dataclass
class RecordedHistoryRecoveries:
    """A history-recovery recorder that keeps what certification wrote down.

    A checkpoint must be unable to certify past a gap without recording it, so
    the collaborator is required rather than optional. Tests about the transport
    still need one; this is the smallest thing that honestly is one.
    """

    rooms: list[str] = field(default_factory=list)

    async def record_room_history_recovery(self, room_id: str) -> RoomHistoryRecovery:
        """Record that an unknown skipped gap left one room owing history."""
        self.rooms.append(room_id)
        return RoomHistoryRecovery(
            room_id=room_id,
            state=HistoryRecoveryState.REPAIRABLE,
        )


def save_sync_token(
    storage_path: Path,
    agent_name: str,
    token: str,
    *,
    store_generation: str,
) -> None:
    """Persist one checkpoint through the production continuity owner."""
    SyncContinuityStore(storage_path, agent_name).replace_checkpoint(
        SyncCheckpoint(token=token, store_generation=store_generation),
    )


def clear_sync_token(storage_path: Path, agent_name: str) -> None:
    """Clear one checkpoint through the production continuity owner."""
    SyncContinuityStore(storage_path, agent_name).clear_checkpoint()


def load_sync_checkpoint(storage_path: Path, agent_name: str) -> SyncCheckpoint | None:
    """Load one checkpoint through the production continuity owner."""
    return SyncContinuityStore(storage_path, agent_name).load().checkpoint


async def certify_response(
    trust: SyncCheckpointTrust,
    *,
    next_batch: str | None,
    recovery: SyncRecoveryOutcome,
) -> SyncCertificationDecision:
    """Plan and apply one response back to back.

    Production keeps these two steps apart on purpose -- the durable work a
    response owes happens between them -- so this convenience lives here rather
    than on ``SyncCheckpointTrust``, where it would be an API nothing ships.
    """
    decision = trust.plan_response(next_batch=next_batch, recovery=recovery)
    return await trust.apply_response(decision, recovery=recovery)
