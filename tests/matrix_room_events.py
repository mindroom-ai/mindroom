"""Matrix room events and read responses for tests that replay what a room shows."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock

import nio

from tests.conftest import make_matrix_client_mock

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

BOT_USER_ID = "@actual_test_agent:localhost"
ROOM_ID = "!room:example.com"
USER_ID = "@user:example.com"
NOW_MS = 1_000_000


def make_message_event(
    *,
    event_id: str,
    body: str,
    timestamp_ms: int,
    sender: str = BOT_USER_ID,
    room_id: str = ROOM_ID,
    relates_to: dict[str, object] | None = None,
    extra_content: dict[str, object] | None = None,
    new_content: dict[str, object] | None = None,
) -> nio.RoomMessageText:
    """Return one text message event as the homeserver serves it."""
    content: dict[str, object] = {
        "body": body,
        "msgtype": "m.text",
    }
    if relates_to is not None:
        content["m.relates_to"] = relates_to
    if extra_content is not None:
        content.update(extra_content)
    if new_content is not None:
        content["m.new_content"] = new_content

    event = nio.RoomMessageText.from_dict(
        {
            "content": content,
            "event_id": event_id,
            "sender": sender,
            "origin_server_ts": timestamp_ms,
            "type": "m.room.message",
            "room_id": room_id,
        },
    )
    event.source = event.__dict__["source"]
    return cast("nio.RoomMessageText", event)


def make_client() -> AsyncMock:
    """Return an AsyncClient-shaped mock signed in as the bot."""
    return make_matrix_client_mock(user_id=BOT_USER_ID)


def room_messages_response(*events: object, end: str | None = None) -> nio.RoomMessagesResponse:
    """Return one page of room history."""
    response = MagicMock()
    response.__class__ = nio.RoomMessagesResponse
    response.chunk = list(events)
    response.end = end
    return response


def room_get_event_response(event: object) -> nio.RoomGetEventResponse:
    """Return the homeserver's answer to reading one event."""
    response = MagicMock()
    response.__class__ = nio.RoomGetEventResponse
    response.event = event
    return response


def thread_reply_relation(thread_id: str, reply_to_event_id: str) -> dict[str, object]:
    """Return the relation of a threaded reply."""
    return {
        "rel_type": "m.thread",
        "event_id": thread_id,
        "m.in_reply_to": {"event_id": reply_to_event_id},
    }


async def aiter_events(*events: object) -> AsyncIterator[object]:
    """Yield events as an async relations iterator does."""
    for event in events:
        yield event
