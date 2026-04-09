"""Helpers for reading nio's in-memory room cache safely."""

from __future__ import annotations

from typing import TYPE_CHECKING

import nio

from mindroom.logging_config import get_logger
from mindroom.matrix.event_cache import EventCache, normalize_event_source_for_cache

if TYPE_CHECKING:
    from nio.responses import RoomGetEventError


logger = get_logger(__name__)


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
    event_cache: EventCache | None,
    room_id: str,
    event_id: str,
) -> nio.RoomGetEventResponse | RoomGetEventError:
    """Return one event through the persistent cache when available."""
    normalized_event_id = event_id.strip()
    if event_cache is not None and normalized_event_id:
        try:
            cached_event = await event_cache.get_event(normalized_event_id)
        except Exception as exc:
            logger.warning(
                "Failed to read cached Matrix event",
                room_id=room_id,
                event_id=normalized_event_id,
                error=str(exc),
            )
        else:
            if cached_event is not None:
                cached_response = nio.RoomGetEventResponse.from_dict(cached_event)
                if isinstance(cached_response, nio.RoomGetEventResponse):
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
    return response
