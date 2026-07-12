"""Typed records shared across thread-export collaborators."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.matrix.users import AgentMatrixUser


@dataclass(frozen=True)
class ThreadExportRoom:
    """One Matrix room selected for thread export."""

    key: str
    room_id: str
    alias: str
    name: str
    invited: bool = False


@dataclass(frozen=True)
class _ThreadExportFailure:
    """One room or thread export failure."""

    room_key: str
    room_id: str
    thread_id: str | None
    error: str


def failure_for_room(
    room: ThreadExportRoom,
    error: str,
    *,
    thread_id: str | None = None,
) -> _ThreadExportFailure:
    """Build one room- or thread-scoped export failure."""
    return _ThreadExportFailure(
        room_key=room.key,
        room_id=room.room_id,
        thread_id=thread_id,
        error=error,
    )


@dataclass(frozen=True)
class ThreadExportStats:
    """Summary for one export pass."""

    output_dir: Path
    rooms_exported: int = 0
    threads_seen: int = 0
    threads_exported: int = 0
    threads_unchanged: int = 0
    truncated_rooms: int = 0
    failed_items: tuple[_ThreadExportFailure, ...] = field(default_factory=tuple)

    @property
    def failures(self) -> int:
        """Return failed room/thread count."""
        return len(self.failed_items)


@dataclass(frozen=True)
class ThreadExportTarget:
    """One export destination and its optional room-membership scope."""

    output_dir: Path
    required_member_user_id: str | None = None
    include_invited_rooms: bool = True


@dataclass
class ThreadExportAccumulator:
    """Mutable statistics and reconciliation state for one export target."""

    target: ThreadExportTarget
    rooms_exported: int = 0
    threads_seen: int = 0
    threads_exported: int = 0
    threads_unchanged: int = 0
    truncated_rooms: int = 0
    failed_items: list[_ThreadExportFailure] = field(default_factory=list)
    retained_room_keys: set[str] = field(default_factory=set)

    def stats(self) -> ThreadExportStats:
        """Return the immutable public statistics for this target."""
        return ThreadExportStats(
            output_dir=self.target.output_dir,
            rooms_exported=self.rooms_exported,
            threads_seen=self.threads_seen,
            threads_exported=self.threads_exported,
            threads_unchanged=self.threads_unchanged,
            truncated_rooms=self.truncated_rooms,
            failed_items=tuple(self.failed_items),
        )


@dataclass(frozen=True)
class ThreadExportGroup:
    """Rooms ready to be read with one persisted Matrix account."""

    rooms: tuple[ThreadExportRoom, ...]
    user: AgentMatrixUser


@dataclass(frozen=True)
class ThreadExportGroupFailure:
    """Rooms that could not be assigned a usable Matrix account."""

    rooms: tuple[ThreadExportRoom, ...]
    error: str


type ThreadExportGroupResult = ThreadExportGroup | ThreadExportGroupFailure
