"""Client-backed room-scan helpers for Matrix thread membership resolution.

This module is the seam between pure resolution (``thread_membership``) and the homeserver transport
(``client_thread_history``): it builds ``ThreadMembershipAccess`` adapters whose root proofs run real
room scans.
It exists as its own module because ``client_thread_history`` imports ``thread_membership`` (via
``thread_projection`` and for ``ThreadRoomScanRootNotFoundError``), so ``thread_membership`` itself can
never depend on the transport.
Cache reads here are advisory accelerators only; the authoritative root proof is always the room scan.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

import nio
from nio.responses import RoomGetEventError

from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.room_history_reads import fetch_thread_event_sources_via_room_messages
from mindroom.matrix.thread_membership import (
    ThreadMembershipAccess,
    resolve_event_thread_membership,
    room_scan_thread_membership_access,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence

type _EventLookupResult = nio.RoomGetEventResponse | RoomGetEventError


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


def _event_info_from_lookup_response(
    response: _EventLookupResult,
    *,
    event_id: str,
    strict: bool,
) -> EventInfo | None:
    """Normalize one room-get-event style response into EventInfo when available."""
    if isinstance(response, nio.RoomGetEventResponse):
        return EventInfo.from_event(response.event.source)
    if not strict:
        return None
    if isinstance(response, nio.RoomGetEventError) and response.status_code == "M_NOT_FOUND":
        return None
    detail = response.message if isinstance(response, nio.RoomGetEventError) else "unknown error"
    msg = f"Failed to resolve Matrix event {event_id}: {detail}"
    raise RuntimeError(msg)


async def fetch_event_info_for_client(
    client: nio.AsyncClient,
    room_id: str,
    event_id: str,
    *,
    strict: bool,
) -> EventInfo | None:
    """Fetch one event directly from Matrix and parse its relation metadata."""
    response = await client.room_get_event(room_id, event_id)
    return _event_info_from_lookup_response(
        response,
        event_id=event_id,
        strict=strict,
    )


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

    event_info = await fetch_event_info_for_client(
        client,
        room_id,
        normalized_event_id,
        strict=False,
    )
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
            fetch_event_info=lambda lookup_room_id, lookup_event_id: fetch_event_info_for_client(
                client,
                lookup_room_id,
                lookup_event_id,
                strict=False,
            ),
        ),
    )
    return resolution.thread_id


__all__ = [
    "RoomScanRelations",
    "fetch_event_info_for_client",
    "resolve_thread_root_event_id_for_client",
    "room_scan_membership_access_for_client",
]
