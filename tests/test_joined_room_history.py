"""Focused tests for bounded readable history hydration after a room join."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, call

import nio
import pytest

from mindroom.matrix.joined_room_history import _fetch_room_history

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
