"""Tests for live inbound message coalescing."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import nio
import pytest

from mindroom.coalescing import CoalescingGate
from mindroom.coalescing_batch import PendingEvent
from mindroom.dispatch_source import VOICE_SOURCE_KIND

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.coalescing_batch import CoalescedBatch


async def _wait_for(condition: Callable[[], bool], *, deadline_seconds: float = 0.5) -> None:
    """Poll until a test condition becomes true."""
    ready = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _mark_ready() -> None:
        if condition():
            ready.set()
            return
        loop.call_later(0.001, _mark_ready)

    _mark_ready()
    try:
        async with asyncio.timeout(deadline_seconds):
            await ready.wait()
    except TimeoutError as exc:
        msg = "Timed out waiting for async test condition"
        raise AssertionError(msg) from exc


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


def _image_event(event_id: str, origin_server_ts: int) -> nio.RoomMessageImage:
    """Build one plain Matrix image event."""
    return nio.RoomMessageImage.from_dict(
        {
            "content": {
                "body": "photo.jpg",
                "filename": "photo.jpg",
                "info": {"mimetype": "image/jpeg"},
                "msgtype": "m.image",
                "url": "mxc://localhost/photo",
            },
            "event_id": event_id,
            "sender": "@user:localhost",
            "origin_server_ts": origin_server_ts,
            "room_id": "!room:localhost",
            "type": "m.room.message",
        },
    )


def _pending(event: nio.RoomMessageText | nio.RoomMessageImage) -> PendingEvent:
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
async def test_room_level_messages_do_not_coalesce_during_upload_grace() -> None:
    """Room-level text roots must stay separate even when upload grace is enabled."""
    batches: list[CoalescedBatch] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 0.0,
        upload_grace_seconds=lambda: 0.05,
        is_shutting_down=lambda: False,
    )
    key = ("!room:localhost", None, "@user:localhost")

    await gate.enqueue(key, _pending(_text_event("$first:localhost", "first", 1_000_000)))
    await gate.enqueue(key, _pending(_text_event("$second:localhost", "second", 1_000_600)))

    await gate.drain_all()

    assert [batch.source_event_ids for batch in batches] == [
        ["$first:localhost"],
        ["$second:localhost"],
    ]
    assert all("quick succession" not in batch.prompt for batch in batches)


@pytest.mark.asyncio
async def test_room_level_text_waits_for_late_media_upload_grace() -> None:
    """One room-level text root may still collect a late media upload."""
    batches: list[CoalescedBatch] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 0.0,
        upload_grace_seconds=lambda: 0.05,
        is_shutting_down=lambda: False,
    )
    key = ("!room:localhost", None, "@user:localhost")

    await gate.enqueue(key, _pending(_text_event("$text:localhost", "describe this", 1_000_000)))
    await asyncio.sleep(0.01)
    await gate.enqueue(key, _pending(_image_event("$image:localhost", 1_000_600)))

    await _wait_for(lambda: len(batches) >= 1)

    assert [batch.source_event_ids for batch in batches] == [["$text:localhost", "$image:localhost"]]


@pytest.mark.asyncio
async def test_voice_class_text_does_not_wait_for_upload_grace() -> None:
    """Voice transcripts are text-shaped but should not wait for image upload grace."""
    batches: list[CoalescedBatch] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 0.0,
        upload_grace_seconds=lambda: 0.5,
        is_shutting_down=lambda: False,
    )
    key = ("!room:localhost", None, "@user:localhost")

    await gate.enqueue(
        key,
        PendingEvent(
            event=_text_event("$voice:localhost", "voice transcript", 1_000_000),
            room=nio.MatrixRoom("!room:localhost", "@mindroom:localhost"),
            source_kind=VOICE_SOURCE_KIND,
        ),
    )

    await _wait_for(lambda: len(batches) == 1, deadline_seconds=0.1)

    assert [batch.source_event_ids for batch in batches] == [["$voice:localhost"]]


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
