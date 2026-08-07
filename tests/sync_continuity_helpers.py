"""Test helpers for unified Matrix sync continuity."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from mindroom.event_journal import RoomHistoryDebt
from mindroom.matrix.sync_continuity import SyncContinuityStore
from mindroom.matrix.sync_token_values import SyncCheckpoint

if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class RecordedHistoryDebts:
    """A history-debt recorder that keeps what certification wrote down.

    Cache trust must be unable to certify past a gap without recording it, so
    the collaborator is required rather than optional. Tests about the transport
    still need one; this is the smallest thing that honestly is one.
    """

    anchor_ts: int = 1_000
    rooms: list[str] = field(default_factory=list)

    async def record_room_history_debt(self, room_id: str) -> RoomHistoryDebt | None:
        """Record the history a skipped gap left one room owing."""
        self.rooms.append(room_id)
        return RoomHistoryDebt(room_id=room_id, owed_through_ts=self.anchor_ts)


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
