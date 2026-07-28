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

from mindroom.matrix.client_thread_history import fetch_thread_event_sources_via_room_messages
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.media import event_source_supports_valid_explicit_thread_relation
from mindroom.matrix.thread_membership import ThreadMembershipAccess, room_scan_thread_membership_access

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence

type _EventLookupResult = nio.RoomGetEventResponse | RoomGetEventError


class RoomScanConversationCache(Protocol):
    """Minimal cache reads needed to resolve room-scan-backed thread membership."""

    async def get_event(self, room_id: str, event_id: str) -> _EventLookupResult:
        """Resolve one Matrix event by ID."""

    async def get_thread_id_for_event(self, room_id: str, event_id: str) -> str | None:
        """Resolve one cached thread root when known."""


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
    room_id: str,
    event_id: str,
    strict: bool,
) -> EventInfo | None:
    """Normalize one room-get-event style response into EventInfo when available."""
    event_source = validated_event_source_from_lookup_response(
        response,
        room_id=room_id,
        event_id=event_id,
    )
    if event_source is not None:
        return EventInfo.from_event(event_source)
    if isinstance(response, nio.RoomGetEventResponse):
        return None
    if not strict:
        return None
    if isinstance(response, nio.RoomGetEventError) and response.status_code == "M_NOT_FOUND":
        return None
    detail = response.message if isinstance(response, nio.RoomGetEventError) else "unknown error"
    msg = f"Failed to resolve Matrix event {event_id}: {detail}"
    raise RuntimeError(msg)


def validated_event_source_from_lookup_response(
    response: _EventLookupResult,
    *,
    room_id: str,
    event_id: str,
) -> dict[str, object] | None:
    """Return one exact, room-scoped event lookup result with a valid relation envelope."""
    if not isinstance(response, nio.RoomGetEventResponse):
        return None
    event_source = response.event.source
    if event_source.get("event_id") != event_id or not event_source_supports_valid_explicit_thread_relation(
        event_source,
        room_id,
    ):
        return None
    return {key: value for key, value in event_source.items() if isinstance(key, str)}


async def lookup_thread_id_from_conversation_cache(
    conversation_cache: RoomScanConversationCache | None,
    room_id: str,
    event_id: str,
) -> str | None:
    """Return one cached thread root when a conversation cache is available."""
    if conversation_cache is None:
        return None
    return await conversation_cache.get_thread_id_for_event(room_id, event_id)


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
        room_id=room_id,
        event_id=event_id,
        strict=strict,
    )


async def fetch_event_info_from_conversation_cache(
    conversation_cache: RoomScanConversationCache,
    room_id: str,
    event_id: str,
    *,
    strict: bool,
) -> EventInfo | None:
    """Fetch one event through the conversation cache and parse its relation metadata."""
    response = await conversation_cache.get_event(room_id, event_id)
    return _event_info_from_lookup_response(
        response,
        room_id=room_id,
        event_id=event_id,
        strict=strict,
    )


def room_scan_membership_access_for_client(
    client: nio.AsyncClient,
    *,
    conversation_cache: RoomScanConversationCache | None,
    fetch_event_info: Callable[[str, str], Awaitable[EventInfo | None]] | None = None,
    known_event_sources: Mapping[str, dict[str, object]] | None = None,
) -> ThreadMembershipAccess:
    """Build client-backed membership access without widening the cache protocol."""
    event_sources = {} if known_event_sources is None else dict(known_event_sources)

    async def lookup_thread_id(lookup_room_id: str, lookup_event_id: str) -> str | None:
        return await lookup_thread_id_from_conversation_cache(
            conversation_cache,
            lookup_room_id,
            lookup_event_id,
        )

    async def resolved_fetch_event_info(lookup_room_id: str, lookup_event_id: str) -> EventInfo | None:
        known_source = event_sources.get(lookup_event_id)
        if known_source is not None:
            return EventInfo.from_event(known_source)
        if fetch_event_info is not None:
            return await fetch_event_info(lookup_room_id, lookup_event_id)
        if conversation_cache is None:
            return None
        return await fetch_event_info_from_conversation_cache(
            conversation_cache,
            lookup_room_id,
            lookup_event_id,
            strict=True,
        )

    async def fetch_event_source(lookup_room_id: str, lookup_event_id: str) -> dict[str, object] | None:
        known_source = event_sources.get(lookup_event_id)
        if known_source is not None:
            return known_source
        response = (
            await conversation_cache.get_event(lookup_room_id, lookup_event_id)
            if conversation_cache is not None
            else await client.room_get_event(lookup_room_id, lookup_event_id)
        )
        if not isinstance(response, nio.RoomGetEventResponse):
            return None
        normalized_source = validated_event_source_from_lookup_response(
            response,
            room_id=lookup_room_id,
            event_id=lookup_event_id,
        )
        if normalized_source is None:
            return None
        event_sources[lookup_event_id] = normalized_source
        return normalized_source

    return room_scan_thread_membership_access(
        lookup_thread_id=lookup_thread_id,
        fetch_event_info=resolved_fetch_event_info,
        fetch_thread_event_sources=lambda room_id, thread_root_id: _scan_thread_event_sources(
            client,
            room_id,
            thread_root_id,
        ),
        fetch_event_source=fetch_event_source,
    )


__all__ = [
    "RoomScanConversationCache",
    "fetch_event_info_for_client",
    "fetch_event_info_from_conversation_cache",
    "lookup_thread_id_from_conversation_cache",
    "room_scan_membership_access_for_client",
    "validated_event_source_from_lookup_response",
]
