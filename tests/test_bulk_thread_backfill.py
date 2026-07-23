"""Tests for the bulk thread-cache backfill scan."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import nio
import pytest

from mindroom.matrix.client_thread_history import bulk_refresh_room_thread_histories

_ROOM_ID = "!room:localhost"


def _message_event(
    event_id: str,
    body: str,
    *,
    timestamp: int,
    thread_root_id: str | None = None,
) -> nio.RoomMessageText:
    content: dict[str, object] = {"body": body, "msgtype": "m.text"}
    if thread_root_id is not None:
        content["m.relates_to"] = {"rel_type": "m.thread", "event_id": thread_root_id}
    return nio.RoomMessageText.from_dict(
        {
            "event_id": event_id,
            "sender": "@alice:localhost",
            "origin_server_ts": timestamp,
            "room_id": _ROOM_ID,
            "type": "m.room.message",
            "content": content,
        },
    )


def _edit_event(
    event_id: str,
    original_event_id: str,
    *,
    timestamp: int,
    thread_root_id: str,
) -> nio.RoomMessageText:
    return nio.RoomMessageText.from_dict(
        {
            "event_id": event_id,
            "sender": "@alice:localhost",
            "origin_server_ts": timestamp,
            "room_id": _ROOM_ID,
            "type": "m.room.message",
            "content": {
                "body": "* edited reply",
                "msgtype": "m.text",
                "m.relates_to": {"rel_type": "m.replace", "event_id": original_event_id},
                "m.new_content": {
                    "body": "edited reply",
                    "msgtype": "m.text",
                    "m.relates_to": {"rel_type": "m.thread", "event_id": thread_root_id},
                },
            },
        },
    )


def _messages_response(chunk: list[nio.Event], *, end: str | None) -> nio.RoomMessagesResponse:
    return nio.RoomMessagesResponse(room_id=_ROOM_ID, chunk=chunk, start="", end=end)


@pytest.mark.asyncio
async def test_bulk_refresh_scans_room_once_and_stores_each_thread() -> None:
    """One backward walk should recover and store every requested thread's rows root-first."""
    client = AsyncMock()
    client.room_messages = AsyncMock(
        side_effect=[
            _messages_response(
                [
                    _edit_event(
                        "$a1-edit:localhost",
                        "$a1:localhost",
                        timestamp=5000,
                        thread_root_id="$a:localhost",
                    ),
                    _message_event("$b1:localhost", "reply b", timestamp=4000, thread_root_id="$b:localhost"),
                    _message_event("$a1:localhost", "reply a", timestamp=3000, thread_root_id="$a:localhost"),
                ],
                end="t1",
            ),
            _messages_response(
                [
                    _message_event("$b:localhost", "root b", timestamp=2000),
                    _message_event("$a:localhost", "root a", timestamp=1000),
                    _message_event("$solo:localhost", "no thread", timestamp=500),
                ],
                end="t2",
            ),
        ],
    )
    event_cache = AsyncMock()
    event_cache.room_membership_epoch = AsyncMock(return_value=7)
    event_cache.replace_thread_if_not_newer = AsyncMock(return_value=True)

    stats = await bulk_refresh_room_thread_histories(
        client,
        _ROOM_ID,
        event_cache,
        thread_root_ids=["$a:localhost", "$b:localhost"],
        caller_label="test",
    )

    assert client.room_messages.await_count == 2
    assert stats.requested_threads == 2
    assert stats.stored_threads == 2
    assert stats.missing_root_ids == frozenset()
    assert stats.room_scan_pages == 2

    stored = {
        call.args[1]: [source["event_id"] for source in call.args[2]]
        for call in event_cache.replace_thread_if_not_newer.await_args_list
    }
    assert stored == {
        "$a:localhost": ["$a:localhost", "$a1:localhost", "$a1-edit:localhost"],
        "$b:localhost": ["$b:localhost", "$b1:localhost"],
    }
    assert all(
        call.kwargs["expected_membership_epoch"] == 7
        for call in event_cache.replace_thread_if_not_newer.await_args_list
    )


@pytest.mark.asyncio
async def test_bulk_refresh_reports_missing_roots_without_storing_partial_threads() -> None:
    """Roots absent from a drained scan must be reported and never stored."""
    client = AsyncMock()
    client.room_messages = AsyncMock(
        side_effect=[
            _messages_response(
                [
                    _message_event("$a1:localhost", "reply a", timestamp=3000, thread_root_id="$a:localhost"),
                    _message_event("$a:localhost", "root a", timestamp=1000),
                ],
                end=None,
            ),
        ],
    )
    event_cache = AsyncMock()
    event_cache.room_departure_epoch = Mock(return_value=3)
    event_cache.replace_thread_if_not_newer = AsyncMock(return_value=True)

    stats = await bulk_refresh_room_thread_histories(
        client,
        _ROOM_ID,
        event_cache,
        thread_root_ids=["$a:localhost", "$ghost:localhost"],
        caller_label="test",
    )

    assert stats.stored_threads == 1
    assert stats.missing_root_ids == frozenset({"$ghost:localhost"})
    event_cache.replace_thread_if_not_newer.assert_awaited_once()
    assert event_cache.replace_thread_if_not_newer.await_args.args[1] == "$a:localhost"
