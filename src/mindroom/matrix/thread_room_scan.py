"""Client-backed room-scan helpers for Matrix thread membership resolution."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

import nio
from nio.responses import RoomGetEventError

from mindroom.matrix.client_thread_history import _fetch_thread_event_sources_via_room_messages
from mindroom.matrix.event_info import EventInfo
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
    scan_result = await _fetch_thread_event_sources_via_room_messages(client, room_id, thread_root_id)
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


async def _lookup_thread_id_from_conversation_cache(
    conversation_cache: RoomScanConversationCache | None,
    room_id: str,
    event_id: str,
) -> str | None:
    """Return one cached thread root when a conversation cache is available."""
    if conversation_cache is None:
        return None
    return await conversation_cache.get_thread_id_for_event(room_id, event_id)


async def _fetch_event_info_from_conversation_cache(
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
        event_id=event_id,
        strict=strict,
    )


def _room_scan_membership_access_for_client(
    client: nio.AsyncClient,
    *,
    conversation_cache: RoomScanConversationCache | None,
    fetch_event_info: Callable[[str, str], Awaitable[EventInfo | None]] | None = None,
) -> ThreadMembershipAccess:
    """Build client-backed membership access without widening the cache protocol."""

    async def lookup_thread_id(lookup_room_id: str, lookup_event_id: str) -> str | None:
        return await _lookup_thread_id_from_conversation_cache(
            conversation_cache,
            lookup_room_id,
            lookup_event_id,
        )

    async def resolved_fetch_event_info(lookup_room_id: str, lookup_event_id: str) -> EventInfo | None:
        if fetch_event_info is not None:
            return await fetch_event_info(lookup_room_id, lookup_event_id)
        if conversation_cache is None:
            return None
        return await _fetch_event_info_from_conversation_cache(
            conversation_cache,
            lookup_room_id,
            lookup_event_id,
            strict=True,
        )

    return room_scan_thread_membership_access(
        lookup_thread_id=lookup_thread_id,
        fetch_event_info=resolved_fetch_event_info,
        fetch_thread_event_sources=lambda room_id, thread_root_id: _scan_thread_event_sources(
            client,
            room_id,
            thread_root_id,
        ),
    )


__all__ = [
    "RoomScanConversationCache",
    "_fetch_event_info_from_conversation_cache",
    "_room_scan_membership_access_for_client",
]
