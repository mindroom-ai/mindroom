"""Focused tests for bounded readable history hydration after a room join."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, call

import nio
import pytest

from mindroom.matrix.joined_room_history import (
    _fetch_room_history,
    cache_fenced_world_readable_join_history,
)

if TYPE_CHECKING:
    from collections.abc import Sequence


def _message(event_id: str) -> nio.Event:
    event = nio.Event.parse_event(
        {
            "event_id": event_id,
            "sender": "@user:localhost",
            "origin_server_ts": 1,
            "type": "m.room.message",
            "content": {"msgtype": "m.text", "body": event_id},
        },
    )
    assert isinstance(event, nio.Event)
    return event


def _page(
    room_id: str,
    event_ids: Sequence[str],
    *,
    start: str,
    end: str | None,
) -> nio.RoomMessagesResponse:
    return nio.RoomMessagesResponse(
        room_id=room_id,
        chunk=[_message(event_id) for event_id in event_ids],
        start=start,
        end=end,
    )


@dataclass
class _HistoryClient:
    config: nio.AsyncClientConfig
    room_messages: AsyncMock
    rooms: dict[str, nio.MatrixRoom]
    user_id: str | None = "@bot:localhost"


def _joined_response(
    room_id: str,
    *,
    history_visibility: str,
    prev_batch: str | None = "s0",
) -> nio.SyncResponse:
    timeline: dict[str, object] = {
        "events": [],
        "limited": True,
    }
    if prev_batch is not None:
        timeline["prev_batch"] = prev_batch
    response = nio.SyncResponse.from_dict(
        {
            "next_batch": "s1",
            "device_one_time_keys_count": {},
            "device_lists": {"changed": [], "left": []},
            "rooms": {
                "invite": {},
                "leave": {},
                "join": {
                    room_id: {
                        "timeline": timeline,
                        "state": {
                            "events": [
                                {
                                    "type": "m.room.history_visibility",
                                    "event_id": "$history-visibility",
                                    "sender": "@user:localhost",
                                    "state_key": "",
                                    "origin_server_ts": 1,
                                    "content": {"history_visibility": history_visibility},
                                },
                            ],
                        },
                        "ephemeral": {"events": []},
                        "account_data": {"events": []},
                    },
                },
            },
            "to_device": {"events": []},
            "presence": {"events": []},
            "account_data": {"events": []},
        },
    )
    assert isinstance(response, nio.SyncResponse)
    return response


@pytest.mark.asyncio
async def test_fetch_room_history_returns_newest_event_budget_in_chronological_order() -> None:
    """The event budget is a successful window, not a permanent recovery failure."""
    room_id = "!room:localhost"
    room_messages = AsyncMock(
        side_effect=[
            _page(room_id, ("$newest", "$newer"), start="s0", end="s1"),
            _page(room_id, ("$older", "$outside-budget"), start="s1", end="s2"),
        ],
    )
    client = _HistoryClient(
        config=nio.AsyncClientConfig(
            backfill_max_events=3,
            backfill_max_pages=10,
            backfill_page_size=2,
            backfill_timeout=1,
        ),
        room_messages=room_messages,
        rooms={},
    )

    events = await _fetch_room_history(
        cast("nio.AsyncClient", client),
        room_id=room_id,
        start="s0",
    )

    assert [event.event_id for event in events] == ["$older", "$newer", "$newest"]
    assert room_messages.await_args_list == [
        call(room_id, start="s0", direction=nio.MessageDirection.back, limit=2),
        call(room_id, start="s1", direction=nio.MessageDirection.back, limit=2),
    ]


@pytest.mark.asyncio
async def test_fetch_room_history_returns_page_budget_without_restarting_sync() -> None:
    """Exhausting the page budget returns its bounded window instead of livelocking."""
    room_id = "!room:localhost"
    room_messages = AsyncMock(
        side_effect=[
            _page(room_id, ("$newest", "$newer"), start="s0", end="s1"),
            _page(room_id, ("$older", "$oldest"), start="s1", end="s2"),
        ],
    )
    client = _HistoryClient(
        config=nio.AsyncClientConfig(
            backfill_max_events=100,
            backfill_max_pages=2,
            backfill_page_size=2,
            backfill_timeout=1,
        ),
        room_messages=room_messages,
        rooms={},
    )

    events = await _fetch_room_history(
        cast("nio.AsyncClient", client),
        room_id=room_id,
        start="s0",
    )

    assert [event.event_id for event in events] == ["$oldest", "$older", "$newer", "$newest"]
    assert room_messages.await_args_list == [
        call(room_id, start="s0", direction=nio.MessageDirection.back, limit=2),
        call(room_id, start="s1", direction=nio.MessageDirection.back, limit=2),
    ]


@pytest.mark.asyncio
async def test_join_history_does_not_fetch_history_without_world_readable_access() -> None:
    """A fenced join cannot hydrate history the room did not expose before joining."""
    room_id = "!room:localhost"
    client = _HistoryClient(
        config=nio.AsyncClientConfig(),
        room_messages=AsyncMock(),
        rooms={},
    )
    cache_event = AsyncMock()

    await cache_fenced_world_readable_join_history(
        cast("nio.AsyncClient", client),
        _joined_response(room_id, history_visibility="shared"),
        room_is_fenced=lambda candidate: candidate == room_id,
        cache_event=cache_event,
    )

    client.room_messages.assert_not_awaited()
    cache_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_join_history_uses_response_state_before_nio_has_created_room() -> None:
    """Own-join history uses current response state even before nio owns the room."""
    room_id = "!room:localhost"
    event = _message("$historical")
    client = _HistoryClient(
        config=nio.AsyncClientConfig(),
        room_messages=AsyncMock(
            return_value=nio.RoomMessagesResponse(
                room_id=room_id,
                chunk=[event],
                start="s0",
                end=None,
            ),
        ),
        rooms={},
    )
    cache_event = AsyncMock()

    await cache_fenced_world_readable_join_history(
        cast("nio.AsyncClient", client),
        _joined_response(room_id, history_visibility="world_readable"),
        room_is_fenced=lambda candidate: candidate == room_id,
        cache_event=cache_event,
    )

    cached_room, cached_event = cache_event.await_args.args
    assert cached_room.room_id == room_id
    assert cached_event is event


@pytest.mark.asyncio
async def test_fetch_room_history_rejects_matrix_error() -> None:
    """Pagination errors keep the join fence closed."""
    room_id = "!room:localhost"
    client = _HistoryClient(
        config=nio.AsyncClientConfig(),
        room_messages=AsyncMock(return_value=nio.RoomMessagesError("denied", room_id=room_id)),
        rooms={},
    )

    with pytest.raises(RuntimeError, match="pagination failed"):
        await _fetch_room_history(cast("nio.AsyncClient", client), room_id=room_id, start="s0")


@pytest.mark.asyncio
async def test_fetch_room_history_rejects_repeated_cursor() -> None:
    """A homeserver cursor loop fails closed instead of spinning."""
    room_id = "!room:localhost"
    client = _HistoryClient(
        config=nio.AsyncClientConfig(backfill_max_pages=2),
        room_messages=AsyncMock(return_value=_page(room_id, ("$event",), start="s0", end="s0")),
        rooms={},
    )

    with pytest.raises(RuntimeError, match="repeated cursor"):
        await _fetch_room_history(cast("nio.AsyncClient", client), room_id=room_id, start="s0")


@pytest.mark.asyncio
async def test_fetch_room_history_rejects_expired_budget() -> None:
    """An exhausted timeout budget fails before making a Matrix request."""
    room_id = "!room:localhost"
    client = _HistoryClient(
        config=nio.AsyncClientConfig(backfill_timeout=0),
        room_messages=AsyncMock(),
        rooms={},
    )

    with pytest.raises(TimeoutError, match="timed out"):
        await _fetch_room_history(cast("nio.AsyncClient", client), room_id=room_id, start="s0")
    client.room_messages.assert_not_awaited()


@pytest.mark.asyncio
async def test_join_history_rejects_limited_world_readable_room_without_cursor() -> None:
    """A limited readable join without a cursor cannot certify continuity."""
    room_id = "!room:localhost"
    client = _HistoryClient(
        config=nio.AsyncClientConfig(),
        room_messages=AsyncMock(),
        rooms={},
    )

    with pytest.raises(RuntimeError, match="no history cursor"):
        await cache_fenced_world_readable_join_history(
            cast("nio.AsyncClient", client),
            _joined_response(room_id, history_visibility="world_readable", prev_batch=None),
            room_is_fenced=lambda candidate: candidate == room_id,
            cache_event=AsyncMock(),
        )


@pytest.mark.asyncio
async def test_join_history_requires_authenticated_fallback_room() -> None:
    """A missing nio room cannot be fabricated without an authenticated user."""
    room_id = "!room:localhost"
    client = _HistoryClient(
        config=nio.AsyncClientConfig(),
        room_messages=AsyncMock(),
        rooms={},
        user_id=None,
    )

    with pytest.raises(RuntimeError, match="authenticated user"):
        await cache_fenced_world_readable_join_history(
            cast("nio.AsyncClient", client),
            _joined_response(room_id, history_visibility="world_readable"),
            room_is_fenced=lambda candidate: candidate == room_id,
            cache_event=AsyncMock(),
        )


@pytest.mark.asyncio
async def test_join_history_propagates_cache_failure() -> None:
    """A historical cache failure keeps sync continuity uncertified."""
    room_id = "!room:localhost"
    client = _HistoryClient(
        config=nio.AsyncClientConfig(),
        room_messages=AsyncMock(
            return_value=nio.RoomMessagesResponse(
                room_id=room_id,
                chunk=[_message("$historical")],
                start="s0",
                end=None,
            ),
        ),
        rooms={},
    )

    with pytest.raises(OSError, match="cache unavailable"):
        await cache_fenced_world_readable_join_history(
            cast("nio.AsyncClient", client),
            _joined_response(room_id, history_visibility="world_readable"),
            room_is_fenced=lambda candidate: candidate == room_id,
            cache_event=AsyncMock(side_effect=OSError("cache unavailable")),
        )
