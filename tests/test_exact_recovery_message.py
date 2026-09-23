"""Exact recovery must retain the latest complete canonical response."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import nio
import pytest

from mindroom.constants import STREAM_STATUS_KEY
from mindroom.matrix import client_visible_messages as visible
from tests.test_stale_stream_cleanup import (
    BOT_USER_ID,
    ROOM_ID,
    _aiter,
    _make_client,
    _make_message_event,
    _room_get_event_response,
    _thread_reply_relation,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

pytestmark = pytest.mark.asyncio


async def test_exact_response_uses_latest_same_sender_edit_and_original_thread() -> None:
    """An edit replaces text, never the original conversation or requester link."""
    client = _make_client()
    original = _make_message_event(
        event_id="$initial",
        body="Thinking...",
        timestamp_ms=10,
        relates_to=_thread_reply_relation("$thread", "$request"),
    )
    edits = [
        _make_message_event(
            event_id=event_id,
            body="* replacement",
            timestamp_ms=timestamp,
            sender=sender,
            relates_to={"rel_type": "m.replace", "event_id": "$initial"},
            new_content={
                "msgtype": "m.text",
                "body": body,
                STREAM_STATUS_KEY: "streaming",
                "m.relates_to": _thread_reply_relation("$forged", "$other"),
            },
        )
        for event_id, timestamp, sender, body in (
            ("$foreign", 40, "@other:localhost", "foreign"),
            ("$latest", 30, BOT_USER_ID, "latest complete body"),
            ("$old", 20, BOT_USER_ID, "old body"),
        )
    ]
    client.room_get_event.side_effect = None
    client.room_get_event.return_value = _room_get_event_response(original)
    client.room_get_event_relations = MagicMock(side_effect=lambda *_args, **_kwargs: _aiter(*edits))
    message = await visible.fetch_latest_visible_message(client, room_id=ROOM_ID, event_id="$initial")
    assert message is not None
    assert (message.event_id, message.latest_event_id, message.body) == (
        "$initial",
        "$latest",
        "latest complete body",
    )
    assert (message.thread_id, message.timestamp, message.edited_timestamp) == ("$thread", 10, 30)
    assert message.reply_to_event_id == "$request"
    client.room_messages.assert_not_awaited()


async def test_failed_exact_replacement_read_does_not_fall_back_to_original() -> None:
    """A failed relation page cannot authorize overwriting unseen latest text."""
    client = _make_client()
    client.room_get_event.side_effect = None
    client.room_get_event.return_value = _room_get_event_response(
        _make_message_event(event_id="$initial", body="Thinking...", timestamp_ms=10),
    )

    async def failed(*_args: object, **_kwargs: object) -> AsyncIterator[nio.Event]:
        message = "replacement history unavailable"
        raise nio.RemoteProtocolError(message)
        yield  # pragma: no cover

    client.room_get_event_relations = failed
    with pytest.raises(nio.RemoteProtocolError):
        await visible.fetch_latest_visible_message(client, room_id=ROOM_ID, event_id="$initial")


async def test_malformed_latest_edit_does_not_fall_back_to_an_older_body() -> None:
    """An unreadable newest edit leaves recovery debt instead of losing content."""
    client = _make_client()
    client.room_get_event.side_effect = None
    client.room_get_event.return_value = _room_get_event_response(
        _make_message_event(event_id="$initial", body="Thinking...", timestamp_ms=10),
    )
    malformed = _make_message_event(
        event_id="$edit",
        body="* partial preview",
        timestamp_ms=20,
        relates_to={"rel_type": "m.replace", "event_id": "$initial"},
        new_content={"msgtype": "m.text"},
    )
    client.room_get_event_relations = MagicMock(side_effect=lambda *_args, **_kwargs: _aiter(malformed))
    assert await visible.fetch_latest_visible_message(client, room_id=ROOM_ID, event_id="$initial") is None
