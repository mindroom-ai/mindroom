"""Cache readable pre-join history omitted from nio callback recovery."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import Never

import nio

type _HistoricalEventCache = Callable[[nio.MatrixRoom, nio.Event], Awaitable[None]]
type _RoomFence = Callable[[str], bool]


def _raise_history_fetch_failure(room_id: str, response: nio.RoomMessagesError) -> Never:
    msg = f"Matrix joined-room history pagination failed for {room_id}: {response}"
    raise RuntimeError(msg)


def _joined_room(client: nio.AsyncClient, room_id: str) -> nio.MatrixRoom:
    room = client.rooms.get(room_id)
    if isinstance(room, nio.MatrixRoom):
        return room
    if not client.user_id:
        msg = "Matrix joined-room history cache requires an authenticated user"
        raise RuntimeError(msg)
    return nio.MatrixRoom(room_id, client.user_id)


def _history_visibility(room: nio.MatrixRoom, state_events: Sequence[nio.Event]) -> str:
    """Prefer the response's current state before nio has made the room current."""
    return next(
        (
            event.history_visibility
            for event in reversed(state_events)
            if isinstance(event, nio.RoomHistoryVisibilityEvent)
        ),
        room.history_visibility,
    )


async def _fetch_room_history(
    client: nio.AsyncClient,
    *,
    room_id: str,
    start: str,
) -> tuple[nio.Event, ...]:
    """Fetch one bounded backward history walk or fail without opening the join fence."""
    config = client.config
    deadline = asyncio.get_running_loop().time() + config.backfill_timeout
    cursor = start
    seen_cursors: set[str] = set()
    seen_event_ids: set[str] = set()
    events: list[nio.Event] = []

    for _page in range(config.backfill_max_pages):
        if cursor in seen_cursors:
            msg = f"Matrix joined-room history pagination repeated cursor for {room_id}"
            raise RuntimeError(msg)
        seen_cursors.add(cursor)
        remaining_seconds = deadline - asyncio.get_running_loop().time()
        if remaining_seconds <= 0:
            msg = f"Matrix joined-room history pagination timed out for {room_id}"
            raise TimeoutError(msg)
        response = await asyncio.wait_for(
            client.room_messages(
                room_id,
                start=cursor,
                direction=nio.MessageDirection.back,
                limit=config.backfill_page_size,
            ),
            timeout=remaining_seconds,
        )
        if isinstance(response, nio.RoomMessagesError):
            _raise_history_fetch_failure(room_id, response)

        for event in response.chunk:
            if not isinstance(event, nio.Event) or event.event_id in seen_event_ids:
                continue
            seen_event_ids.add(event.event_id)
            events.append(event)
            if len(events) >= config.backfill_max_events:
                return tuple(reversed(events))

        if not response.chunk or response.end is None:
            return tuple(reversed(events))
        cursor = response.end

    return tuple(reversed(events))


async def cache_fenced_world_readable_join_history(
    client: nio.AsyncClient,
    response: nio.SyncResponse,
    *,
    room_is_fenced: _RoomFence,
    cache_event: _HistoricalEventCache,
) -> None:
    """Cache pre-join history that nio intentionally excludes at an own-join boundary."""
    for room_id, room_info in response.rooms.join.items():
        timeline = room_info.timeline
        room = _joined_room(client, room_id)
        if (
            not room_is_fenced(room_id)
            or not timeline.limited
            or _history_visibility(room, room_info.state) != "world_readable"
        ):
            continue
        if not timeline.prev_batch:
            msg = f"Limited world-readable joined room has no history cursor: {room_id}"
            raise RuntimeError(msg)
        events = await _fetch_room_history(
            client,
            room_id=room_id,
            start=timeline.prev_batch,
        )
        for event in events:
            await cache_event(room, event)
