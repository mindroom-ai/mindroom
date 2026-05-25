"""Tests for live inbound message coalescing."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import nio
import pytest

from mindroom.coalescing import CoalescingGate, IngressAdmissionClosedError, ReadyPendingEvent
from mindroom.coalescing_batch import CoalescingKey, PendingEvent
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


def _voice_pending(event_id: str, body: str, origin_server_ts: int) -> PendingEvent:
    """Wrap one normalized voice transcript as pending voice ingress."""
    return PendingEvent(
        event=_text_event(event_id, body, origin_server_ts),
        room=nio.MatrixRoom("!room:localhost", "@mindroom:localhost"),
        source_kind=VOICE_SOURCE_KIND,
    )


async def _ready_after(
    release: asyncio.Event,
    pending_event: PendingEvent,
) -> ReadyPendingEvent:
    await release.wait()
    return ReadyPendingEvent(pending_event=pending_event)


class FakeMonotonicClock:
    """Mutable monotonic clock for reservation timing tests."""

    def __init__(self, value: float) -> None:
        self.value = value

    def __call__(self) -> float:
        """Return the current fake monotonic time."""
        return self.value

    def advance(self, seconds: float) -> None:
        """Advance the fake monotonic clock."""
        self.value += seconds


def test_reserve_order_uses_local_monotonic_receipt_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reservations should capture local monotonic receipt time."""
    fake_clock = FakeMonotonicClock(10.0)
    monkeypatch.setattr(time, "monotonic", fake_clock)
    gate = CoalescingGate(
        dispatch_batch=AsyncMock(),
        debounce_seconds=lambda: 0.3,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )

    first = gate.reserve_order(room_id="!room:localhost", requester_user_id="@user:localhost")
    fake_clock.advance(0.5)
    second = gate.reserve_order(room_id="!room:localhost", requester_user_id="@user:localhost")

    assert first.receipt_time == 10.0
    assert second.receipt_time == 10.5
    assert first.received_order < second.received_order


@pytest.mark.asyncio
async def test_admit_rejects_released_reservation() -> None:
    """Late admission must not recreate work after the reservation was released."""
    gate = CoalescingGate(
        dispatch_batch=AsyncMock(),
        debounce_seconds=lambda: 0.0,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    reservation = gate.reserve_order(room_id="!room:localhost", requester_user_id="@user:localhost")
    gate.release_order_reservation(reservation)

    with pytest.raises(IngressAdmissionClosedError):
        await gate.admit(
            CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost"),
            ready_result=ReadyPendingEvent(
                pending_event=_pending(_text_event("$late:localhost", "late", 1000)),
            ),
            order_reservation=reservation,
        )


def test_release_order_removes_unadmitted_reservation_from_owner_work() -> None:
    """Reservation release should clear owner-work tracking and be idempotent."""
    gate = CoalescingGate(
        dispatch_batch=AsyncMock(),
        debounce_seconds=lambda: 0.3,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")
    reservation = gate.reserve_order(room_id=key.room_id, requester_user_id=key.requester_user_id)

    assert gate._has_older_owner_work(key, reservation.received_order + 1)

    reservation.release()
    reservation.release()

    assert not gate._has_older_owner_work(key, reservation.received_order + 1)
    assert reservation.released
    assert reservation.settled.is_set()


@pytest.mark.asyncio
async def test_admit_with_reservation_keeps_wall_clock_enqueue_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reservation monotonic receipt time must not be stored as event enqueue time."""
    fake_clock = FakeMonotonicClock(10.0)
    monkeypatch.setattr(time, "monotonic", fake_clock)
    monkeypatch.setattr(time, "time", lambda: 1_000.0)
    gate = CoalescingGate(
        dispatch_batch=AsyncMock(),
        debounce_seconds=lambda: 60.0,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")
    pending = _pending(_text_event("$typed:localhost", "typed", 1_000_000))
    reservation = gate.reserve_order(room_id=key.room_id, requester_user_id=key.requester_user_id)

    await gate.admit(
        key,
        ready_result=ReadyPendingEvent(pending_event=pending),
        order_reservation=reservation,
    )

    queued = gate._gates[key].queue[0]
    assert reservation.receipt_time == 10.0
    assert queued.received_at == 1_000.0
    assert pending.enqueue_time != reservation.receipt_time

    await gate.drain_all()


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
    key = CoalescingKey("!room:localhost", None, "@user:localhost")

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
    key = CoalescingKey("!room:localhost", None, "@user:localhost")

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
    key = CoalescingKey("!room:localhost", None, "@user:localhost")

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
    key = CoalescingKey("!room:localhost", None, "@user:localhost")

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
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")

    await gate.enqueue(key, _pending(_text_event("$first:localhost", "first", 1_000_000)))
    await gate.enqueue(key, _pending(_text_event("$second:localhost", "second", 1_000_600)))

    await gate.drain_all()

    assert [batch.source_event_ids for batch in batches] == [["$first:localhost", "$second:localhost"]]
    assert "quick succession" in batches[0].prompt


@pytest.mark.asyncio
async def test_voice_readiness_delay_does_not_extend_receive_time_debounce() -> None:
    """A slow STT result must not let later text join an expired voice debounce window."""
    batches: list[CoalescedBatch] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 0.03,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")
    voice_ready = asyncio.Event()

    voice_pending = _voice_pending("$voice:localhost", "voice transcript", 1_000_000)
    voice_task = asyncio.create_task(_ready_after(voice_ready, voice_pending))

    await gate.admit(
        key,
        ready_task=voice_task,
        source_event_id="$voice:localhost",
        source_kind=VOICE_SOURCE_KIND,
        received_at=1_000.0,
    )
    await asyncio.sleep(0.08)
    await gate.enqueue(key, _pending(_text_event("$typed:localhost", "typed follow-up", 1_000_800)))

    assert batches == []

    voice_ready.set()
    await gate.drain_all()

    assert [batch.source_event_ids for batch in batches] == [
        ["$voice:localhost"],
        ["$typed:localhost"],
    ]


@pytest.mark.asyncio
async def test_failed_older_owner_admission_wakes_newer_thread_gate() -> None:
    """A failed older voice admission must not deadlock newer same-user thread work."""
    batches: list[CoalescedBatch] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 0.0,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    older_key = CoalescingKey("!room:localhost", "$thread-a:localhost", "@user:localhost")
    newer_key = CoalescingKey("!room:localhost", "$thread-b:localhost", "@user:localhost")
    fail_voice = asyncio.Event()

    async def failed_voice() -> ReadyPendingEvent:
        await fail_voice.wait()
        msg = "voice failed"
        raise RuntimeError(msg)

    await gate.admit(
        older_key,
        ready_task=asyncio.create_task(failed_voice()),
        source_event_id="$voice:localhost",
        source_kind=VOICE_SOURCE_KIND,
    )
    older_gate = gate._gates[older_key]
    await _wait_for(lambda: bool(older_gate.claimed_admissions))

    await gate.enqueue(newer_key, _pending(_text_event("$newer:localhost", "newer", 1_000_001)))
    await gate.enqueue(older_key, _pending(_text_event("$older-later:localhost", "older later", 1_000_002)))
    fail_voice.set()

    await _wait_for(
        lambda: [batch.source_event_ids for batch in batches] == [["$newer:localhost"], ["$older-later:localhost"]],
    )


@pytest.mark.asyncio
async def test_drain_all_waits_for_order_reservation_to_admit() -> None:
    """Shutdown drains must treat receive-order reservations as pending ingress work."""
    batches: list[CoalescedBatch] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 60.0,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: True,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")
    reservation = gate.reserve_order(
        room_id=key.room_id,
        requester_user_id=key.requester_user_id,
        receipt_time=1_000.0,
    )

    drain_task = asyncio.create_task(gate.drain_all())
    await asyncio.sleep(0)

    assert drain_task.done() is False

    await gate.admit(
        key,
        ready_result=ReadyPendingEvent(
            pending_event=_voice_pending("$voice:localhost", "voice transcript", 1_000_000),
        ),
        source_kind=VOICE_SOURCE_KIND,
        order_reservation=reservation,
    )
    await drain_task

    assert [batch.source_event_ids for batch in batches] == [["$voice:localhost"]]
