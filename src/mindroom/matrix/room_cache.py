"""Helpers for reading nio's in-memory room cache safely."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import nio

from mindroom.logging_config import get_logger
from mindroom.matrix.event_cache import ConversationEventCache, normalize_event_source_for_cache
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.message_content import extract_edit_body

if TYPE_CHECKING:
    from nio.responses import RoomGetEventError


logger = get_logger(__name__)


async def _apply_cached_latest_edit(
    event_source: dict[str, Any],
    *,
    room_id: str,
    client: nio.AsyncClient,
    event_cache: ConversationEventCache | None,
) -> dict[str, Any]:
    """Project one cached original event into its latest visible edited state."""
    if event_cache is None or event_source.get("type") != "m.room.message":
        return event_source

    event_info = EventInfo.from_event(event_source)
    event_id = event_source.get("event_id")
    if event_info.is_edit or not isinstance(event_id, str) or not event_id:
        return event_source

    latest_edit_source = await event_cache.get_latest_edit(room_id, event_id)
    if latest_edit_source is None:
        return event_source

    edited_body, edited_content = await extract_edit_body(latest_edit_source, client)
    if edited_body is None or edited_content is None:
        return event_source

    original_content = event_source.get("content", {})
    merged_content = (
        {key: value for key, value in original_content.items() if isinstance(key, str)}
        if isinstance(original_content, dict)
        else {}
    )
    merged_content.update(edited_content)
    merged_content.setdefault("body", edited_body)

    updated_event_source = {key: value for key, value in event_source.items() if isinstance(key, str)}
    updated_event_source["content"] = merged_content

    latest_edit_timestamp = latest_edit_source.get("origin_server_ts")
    if isinstance(latest_edit_timestamp, int) and not isinstance(latest_edit_timestamp, bool):
        updated_event_source["origin_server_ts"] = latest_edit_timestamp
    return updated_event_source


async def _cached_room_get_event_response(
    client: nio.AsyncClient,
    event_cache: ConversationEventCache | None,
    *,
    room_id: str,
    event_source: dict[str, Any],
) -> nio.RoomGetEventResponse | None:
    """Reconstruct one cached room-get-event response, applying visible edits when present."""
    visible_event_source = await _apply_cached_latest_edit(
        event_source,
        room_id=room_id,
        client=client,
        event_cache=event_cache,
    )
    cached_response = nio.RoomGetEventResponse.from_dict(visible_event_source)
    return cached_response if isinstance(cached_response, nio.RoomGetEventResponse) else None


def cached_rooms(client: nio.AsyncClient) -> dict[str, nio.MatrixRoom]:
    """Return the client room cache when nio has initialized it.

    ``AsyncClient.rooms`` is the source of truth. Non-dict values are treated
    as an empty cache so simple test doubles can opt in by assigning a real
    ``rooms`` dict.
    """
    rooms = client.rooms
    return rooms if isinstance(rooms, dict) else {}


def cached_room(client: nio.AsyncClient, room_id: str) -> nio.MatrixRoom | None:
    """Return one cached room when it is available."""
    return cached_rooms(client).get(room_id)


async def cached_room_get_event(
    client: nio.AsyncClient,
    event_cache: ConversationEventCache | None,
    room_id: str,
    event_id: str,
) -> nio.RoomGetEventResponse | RoomGetEventError:
    """Return one event through the persistent cache when available."""
    normalized_event_id = event_id.strip()
    if event_cache is not None and normalized_event_id:
        try:
            cached_event = await event_cache.get_event(room_id, normalized_event_id)
        except Exception as exc:
            logger.warning(
                "Failed to read cached Matrix event",
                room_id=room_id,
                event_id=normalized_event_id,
                error=str(exc),
            )
        else:
            if cached_event is not None:
                cached_response = await _cached_room_get_event_response(
                    client,
                    event_cache,
                    room_id=room_id,
                    event_source=cached_event,
                )
                if cached_response is not None:
                    return cached_response
                logger.warning(
                    "Cached Matrix event could not be reconstructed",
                    room_id=room_id,
                    event_id=normalized_event_id,
                    error=str(cached_response),
                )

    response = await client.room_get_event(room_id, normalized_event_id)

    if event_cache is not None and isinstance(response, nio.RoomGetEventResponse):
        event = response.event
        event_source = event.source if isinstance(event.source, dict) else {}
        server_timestamp = event.server_timestamp
        try:
            await event_cache.store_event(
                normalized_event_id,
                room_id,
                normalize_event_source_for_cache(
                    event_source,
                    event_id=event.event_id if isinstance(event.event_id, str) else normalized_event_id,
                    sender=event.sender if isinstance(event.sender, str) else None,
                    origin_server_ts=server_timestamp
                    if isinstance(server_timestamp, int) and not isinstance(server_timestamp, bool)
                    else None,
                ),
            )
        except Exception as exc:
            logger.warning(
                "Failed to cache Matrix event lookup",
                room_id=room_id,
                event_id=normalized_event_id,
                error=str(exc),
            )
        reconstructed_response = await _cached_room_get_event_response(
            client,
            event_cache,
            room_id=room_id,
            event_source=normalize_event_source_for_cache(
                event_source,
                event_id=event.event_id if isinstance(event.event_id, str) else normalized_event_id,
                sender=event.sender if isinstance(event.sender, str) else None,
                origin_server_ts=server_timestamp
                if isinstance(server_timestamp, int) and not isinstance(server_timestamp, bool)
                else None,
            ),
        )
        if reconstructed_response is not None:
            return reconstructed_response
    return response
