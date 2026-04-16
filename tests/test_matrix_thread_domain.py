"""Direct tests for Matrix thread resolution and bookkeeping helpers."""

from __future__ import annotations

from unittest.mock import AsyncMock

import nio
import pytest

from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.thread_bookkeeping import (
    event_requires_thread_bookkeeping,
    redaction_requires_thread_bookkeeping,
)
from mindroom.matrix.thread_membership import (
    ThreadResolution,
    map_backed_thread_membership_access,
    resolve_event_thread_membership,
)


def _message_event_info(content: dict[str, object]) -> EventInfo:
    return EventInfo.from_event(
        {
            "type": "m.room.message",
            "content": content,
        },
    )


@pytest.mark.asyncio
async def test_resolve_event_thread_membership_promotes_plain_reply_transitively() -> None:
    """Plain replies should inherit thread membership from a threaded ancestor."""
    event_infos = {
        "$plain-two:localhost": _message_event_info(
            {
                "body": "plain two",
                "msgtype": "m.text",
                "m.relates_to": {"m.in_reply_to": {"event_id": "$plain-one:localhost"}},
            },
        ),
        "$plain-one:localhost": _message_event_info(
            {
                "body": "plain one",
                "msgtype": "m.text",
                "m.relates_to": {"m.in_reply_to": {"event_id": "$thread-reply:localhost"}},
            },
        ),
        "$thread-reply:localhost": _message_event_info(
            {
                "body": "thread reply",
                "msgtype": "m.text",
                "m.relates_to": {
                    "rel_type": "m.thread",
                    "event_id": "$thread-root:localhost",
                },
            },
        ),
    }

    resolution = await resolve_event_thread_membership(
        "!room:localhost",
        event_infos["$plain-two:localhost"],
        access=map_backed_thread_membership_access(
            event_infos=event_infos,
            resolved_thread_ids={},
        ),
    )

    assert resolution == ThreadResolution.threaded("$thread-root:localhost")


@pytest.mark.asyncio
async def test_resolve_event_thread_membership_proves_current_root_when_allowed() -> None:
    """A root event should normalize to itself only when descendants prove it is threaded."""
    event_infos = {
        "$thread-root:localhost": _message_event_info(
            {
                "body": "root",
                "msgtype": "m.text",
            },
        ),
        "$thread-reply:localhost": _message_event_info(
            {
                "body": "thread reply",
                "msgtype": "m.text",
                "m.relates_to": {
                    "rel_type": "m.thread",
                    "event_id": "$thread-root:localhost",
                },
            },
        ),
    }

    resolution = await resolve_event_thread_membership(
        "!room:localhost",
        event_infos["$thread-root:localhost"],
        event_id="$thread-root:localhost",
        allow_current_root=True,
        access=map_backed_thread_membership_access(
            event_infos=event_infos,
            resolved_thread_ids={},
        ),
    )

    assert resolution == ThreadResolution.threaded("$thread-root:localhost")


@pytest.mark.asyncio
async def test_event_requires_thread_bookkeeping_uses_shared_thread_resolution() -> None:
    """Outbound plain replies to threaded events should be classified as thread-scoped."""
    client = AsyncMock()
    conversation_cache = AsyncMock()
    conversation_cache.get_thread_id_for_event.side_effect = (
        lambda room_id, event_id: "$thread-root:localhost"
        if (room_id, event_id) == ("!room:localhost", "$thread-reply:localhost")
        else None
    )

    requires_thread_bookkeeping = await event_requires_thread_bookkeeping(
        client,
        "!room:localhost",
        event_type="m.room.message",
        content={
            "body": "bridged reply",
            "msgtype": "m.text",
            "m.relates_to": {"m.in_reply_to": {"event_id": "$thread-reply:localhost"}},
        },
        conversation_cache=conversation_cache,
    )

    assert requires_thread_bookkeeping is True


@pytest.mark.asyncio
async def test_redaction_requires_thread_bookkeeping_ignores_reactions() -> None:
    """Reaction redactions should stay room-level even when the reaction targets a thread."""
    client = AsyncMock()
    conversation_cache = AsyncMock()
    conversation_cache.get_event.return_value = nio.RoomGetEventResponse.from_dict(
        {
            "event_id": "$reaction:localhost",
            "sender": "@user:localhost",
            "origin_server_ts": 1,
            "room_id": "!room:localhost",
            "type": "m.reaction",
            "content": {
                "m.relates_to": {
                    "rel_type": "m.annotation",
                    "event_id": "$thread-reply:localhost",
                    "key": "👍",
                },
            },
        },
    )

    requires_thread_bookkeeping = await redaction_requires_thread_bookkeeping(
        client,
        "!room:localhost",
        event_id="$reaction:localhost",
        conversation_cache=conversation_cache,
    )

    assert requires_thread_bookkeeping is False
