"""Client-backed room-scan helpers for Matrix thread membership resolution.

This module is the seam between pure resolution (``thread_membership``) and the homeserver transport
(``room_history_reads``): it builds ``ThreadMembershipAccess`` adapters whose root proofs run real
room scans.
It exists as its own module because ``room_history_reads`` imports ``thread_membership`` (via
``thread_projection`` and for ``ThreadRoomScanRootNotFoundError``), so ``thread_membership`` itself can
never depend on the transport.
Journal reads here are accelerators only; the authoritative root proof is always the room scan.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from mindroom.logging_config import get_logger
from mindroom.matrix.room_history_reads import fetch_thread_event_sources_via_room_messages
from mindroom.matrix.thread_membership import (
    ThreadMembershipAccess,
    resolve_event_thread_membership,
    room_scan_thread_membership_access,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence

    import nio

    from mindroom.matrix.event_info import EventInfo

logger = get_logger(__name__)


class RoomScanRelations(Protocol):
    """The two relation facts a room scan needs, however they are answered."""

    async def admitted_thread_id(self, room_id: str, event_id: str) -> str | None:
        """Resolve the thread from local state only, degrading to nothing on failure."""

    async def strict_thread_id(self, room_id: str, event_id: str) -> str | None:
        """Resolve the thread one event belongs to, raising if that cannot be established."""

    async def event_info(self, room_id: str, event_id: str) -> EventInfo | None:
        """Resolve one event's relation metadata, raising on an unusable lookup."""


async def _scan_thread_event_sources(
    client: nio.AsyncClient,
    room_id: str,
    thread_root_id: str,
) -> tuple[Sequence[Mapping[str, object]], bool]:
    """Fetch authoritative room-scan event sources for one candidate thread root."""
    scan_result = await fetch_thread_event_sources_via_room_messages(client, room_id, thread_root_id)
    return scan_result.event_sources, True


async def _degradable_event_info(
    relations: RoomScanRelations,
    room_id: str,
    event_id: str,
) -> EventInfo | None:
    """Return one event's relations, or nothing when the lookup could not say.

    ``relations`` owns this read and memoizes it for the turn, so one turn
    resolving the same event from several places pays for one round trip. What
    it will not do is guess: it raises when it cannot tell a deleted event from
    one the homeserver refused to serve, because a caller resolving a reply
    target would otherwise attach the turn to the wrong conversation.

    Normalizing a thread ID is not that caller. It has a local answer for an
    event the homeserver cannot describe, so a failed lookup degrades to that
    rather than taking the tool down -- but it is logged, because the fallback
    can produce a plausible wrong thread rather than an obvious failure.
    """
    try:
        return await relations.event_info(room_id, event_id)
    except Exception:
        logger.warning(
            "Failed to resolve an event's relations for a room scan; falling back to local state",
            room_id=room_id,
            event_id=event_id,
            exc_info=True,
        )
        return None


def room_scan_membership_access_for_client(
    client: nio.AsyncClient,
    *,
    relations: RoomScanRelations,
    fetch_event_info: Callable[[str, str], Awaitable[EventInfo | None]] | None = None,
) -> ThreadMembershipAccess:
    """Build client-backed membership access over the journal relation view.

    Both lookups used to run through the event cache, which answered from rows
    written under whatever membership was current when they were stored. A room
    left and rejoined after a history-visibility change would still be served
    the old membership's copy, because nothing invalidated those rows on
    departure. The journal fences its own rows on the membership epoch and asks
    the homeserver for anything it has not admitted, so neither answer can
    outlive the membership that produced it.
    """

    async def resolved_fetch_event_info(lookup_room_id: str, lookup_event_id: str) -> EventInfo | None:
        if fetch_event_info is not None:
            return await fetch_event_info(lookup_room_id, lookup_event_id)
        return await relations.event_info(lookup_room_id, lookup_event_id)

    return room_scan_thread_membership_access(
        lookup_thread_id=relations.strict_thread_id,
        fetch_event_info=resolved_fetch_event_info,
        fetch_thread_event_sources=lambda room_id, thread_root_id: _scan_thread_event_sources(
            client,
            room_id,
            thread_root_id,
        ),
    )


async def resolve_thread_root_event_id_for_client(
    client: nio.AsyncClient,
    room_id: str,
    event_id: str,
    *,
    relations: RoomScanRelations,
) -> str | None:
    """Resolve one event ID into a canonical thread root when thread membership can prove one."""
    normalized_event_id = event_id.strip() if isinstance(event_id, str) else ""
    if not normalized_event_id:
        return None

    event_info = await _degradable_event_info(relations, room_id, normalized_event_id)
    if event_info is None:
        # Local state only. The homeserver was just asked for this exact
        # event and could not answer; asking it again resolves nothing.
        return await relations.admitted_thread_id(room_id, normalized_event_id)

    resolution = await resolve_event_thread_membership(
        room_id,
        event_info,
        event_id=normalized_event_id,
        allow_current_root=True,
        access=room_scan_membership_access_for_client(
            client,
            relations=relations,
            fetch_event_info=lambda lookup_room_id, lookup_event_id: _degradable_event_info(
                relations,
                lookup_room_id,
                lookup_event_id,
            ),
        ),
    )
    return resolution.thread_id


__all__ = [
    "RoomScanRelations",
    "resolve_thread_root_event_id_for_client",
    "room_scan_membership_access_for_client",
]
