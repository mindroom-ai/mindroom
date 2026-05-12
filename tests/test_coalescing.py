"""Tests for live inbound message coalescing."""

from __future__ import annotations

from typing import TYPE_CHECKING

import nio
import pytest

from mindroom.coalescing import CoalescingGate
from mindroom.coalescing_batch import PendingEvent

if TYPE_CHECKING:
    from mindroom.coalescing_batch import CoalescedBatch


def _text_event(event_id: str, body: str, origin_server_ts: int) -> nio.RoomMessageText:
    """Build one plain Matrix text event."""
    return nio.RoomMessageText.from_dict(
        {
            "content": {"body": body, "msgtype": "m.text"},
            "event_id": event_id,
            "sender": "@user:localhost",
            "origin_server_ts": origin_server_ts,
            "room_id": "!room:localhost",
            "type": "m.room.message",
        },
    )


def _pending(event: nio.RoomMessageText) -> PendingEvent:
    """Wrap one Matrix event as pending user ingress."""
    return PendingEvent(
        event=event,
        room=nio.MatrixRoom("!room:localhost", "@mindroom:localhost"),
        source_kind="message",
    )


@pytest.mark.asyncio
async def test_room_level_messages_do_not_coalesce() -> None:
    """Independent room-level messages must stay as separate model turns."""
    batches: list[CoalescedBatch] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 1.0,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    key = ("!room:localhost", None, "@user:localhost")

    await gate.enqueue(key, _pending(_text_event("$gmail:localhost", "gmail setup", 1_000_000)))
    await gate.enqueue(key, _pending(_text_event("$extras:localhost", "message extras", 1_000_600)))

    await gate.drain_all()

    assert [batch.source_event_ids for batch in batches] == [
        ["$gmail:localhost"],
        ["$extras:localhost"],
    ]
    assert all("quick succession" not in batch.prompt for batch in batches)


@pytest.mark.asyncio
async def test_thread_messages_inside_debounce_window_still_coalesce() -> None:
    """Thread-scoped follow-ups close in time should remain one coalesced turn."""
    batches: list[CoalescedBatch] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 1.0,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    key = ("!room:localhost", "$thread:localhost", "@user:localhost")

    await gate.enqueue(key, _pending(_text_event("$first:localhost", "first", 1_000_000)))
    await gate.enqueue(key, _pending(_text_event("$second:localhost", "second", 1_000_600)))

    await gate.drain_all()

    assert [batch.source_event_ids for batch in batches] == [["$first:localhost", "$second:localhost"]]
    assert "quick succession" in batches[0].prompt
