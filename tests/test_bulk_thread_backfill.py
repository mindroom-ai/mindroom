"""Tests for the bulk thread-cache backfill scan."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import nio
import pytest

from mindroom.matrix.cache import thread_cache_rejection_reason
from mindroom.matrix.cache.sqlite_event_cache import SqliteEventCache
from mindroom.matrix.client_thread_history import bulk_refresh_room_thread_histories
from tests.event_cache_test_support import replace_thread_unconditionally as _replace_thread

if TYPE_CHECKING:
    from pathlib import Path

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


@pytest.mark.asyncio
async def test_bulk_refresh_opaque_missing_root_marks_existing_snapshot_stale(tmp_path: Path) -> None:
    """Ciphertext in a root-missing bulk scan must invalidate an existing populated snapshot."""
    thread_id = "$root:localhost"
    root_source = {
        "event_id": thread_id,
        "sender": "@alice:localhost",
        "origin_server_ts": 1000,
        "room_id": _ROOM_ID,
        "type": "m.room.message",
        "content": {"body": "cached root", "msgtype": "m.text"},
    }
    opaque_event = nio.MegolmEvent.from_dict(
        {
            "event_id": "$opaque:localhost",
            "sender": "@alice:localhost",
            "origin_server_ts": 2000,
            "room_id": _ROOM_ID,
            "type": "m.room.encrypted",
            "content": {
                "algorithm": "m.megolm.v1.aes-sha2",
                "ciphertext": "opaque ciphertext",
                "device_id": "DEVICE",
                "sender_key": "sender-key",
                "session_id": "session",
            },
        },
    )
    client = AsyncMock()
    client.room_messages = AsyncMock(
        return_value=_messages_response([opaque_event], end=None),
    )
    event_cache = SqliteEventCache(tmp_path / "event_cache.db")
    await event_cache.initialize()

    try:
        await _replace_thread(event_cache, _ROOM_ID, thread_id, [root_source])
        stats = await bulk_refresh_room_thread_histories(
            client,
            _ROOM_ID,
            event_cache,
            thread_root_ids=[thread_id],
            caller_label="test",
        )
        cache_state = await event_cache.get_thread_cache_state(_ROOM_ID, thread_id)
    finally:
        await event_cache.close()

    assert stats.stored_threads == 0
    assert stats.missing_root_ids == frozenset({thread_id})
    assert cache_state is not None
    assert cache_state.invalidation_reason == "thread_history_opaque_encrypted_event"
    assert thread_cache_rejection_reason(cache_state) == "thread_invalidated_after_validation"
