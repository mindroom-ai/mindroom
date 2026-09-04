"""Typed records shared across thread-export collaborators."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    import nio

    from mindroom.matrix.users import AgentMatrixUser
    from mindroom.thread_export.projected_history import ProjectedThreadReader


@dataclass(frozen=True)
class ThreadExportRoom:
    """One Matrix room selected for thread export."""

    key: str
    room_id: str
    alias: str
    name: str
    invited: bool = False
    source_entity_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class _ThreadExportFailure:
    """One target, room, or thread export failure."""

    room_key: str | None
    room_id: str | None
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


def failure_for_target(error: str) -> _ThreadExportFailure:
    """Build one target-scoped export failure."""
    return _ThreadExportFailure(
        room_key=None,
        room_id=None,
        thread_id=None,
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
        """Return failed target, room, or thread count."""
        return len(self.failed_items)


@dataclass(frozen=True)
class ThreadExportTarget:
    """One export destination and its required source-entity scope."""

    output_dir: Path
    source_entity_names: tuple[str, ...] | None
    required_member_user_ids: tuple[str, ...] = ()
    include_invited_rooms: bool = True
    trusted_root: Path | None = None


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
class ThreadExportSource:
    """Rooms readable through one live Matrix client and its projection view."""

    client: nio.AsyncClient
    reader: ProjectedThreadReader
    rooms: tuple[ThreadExportRoom, ...]


@dataclass(frozen=True)
class ThreadExportGroupFailure:
    """Rooms that could not be assigned a usable Matrix account."""

    rooms: tuple[ThreadExportRoom, ...]
    error: str


@dataclass(frozen=True)
class InvitedRoomConflict:
    """One invited room whose persisted claims cannot identify a safe owner."""

    room: ThreadExportRoom
    claimant_labels: tuple[str, ...]


@dataclass(frozen=True)
class InvitedRoomSelection:
    """Currently claimed invited rooms plus retired-only ownership conflicts."""

    groups: tuple[tuple[str, tuple[ThreadExportRoom, ...]], ...]
    conflicts: tuple[InvitedRoomConflict, ...]


type ThreadExportGroupResult = ThreadExportGroup | ThreadExportGroupFailure
