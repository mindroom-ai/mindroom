"""Tests for live inbound message coalescing."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import nio
import pytest

from mindroom.coalescing import (
    CoalescingGate,
    GatePhase,
    IngressAdmissionClosedError,
    ReadyPendingEvent,
    is_coalescing_exempt_source_kind,
)
from mindroom.coalescing_batch import CoalescingKey, PendingEvent, build_coalesced_batch
from mindroom.dispatch_handoff import PendingDispatchMetadata, PreparedTextEvent, build_dispatch_handoff
from mindroom.dispatch_source import (
    ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND,
    IMAGE_SOURCE_KIND,
    MESSAGE_SOURCE_KIND,
    VOICE_SOURCE_KIND,
)

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


def _image_pending(event_id: str, origin_server_ts: int) -> PendingEvent:
    """Wrap one image event as pending media ingress."""
    return PendingEvent(
        event=_image_event(event_id, origin_server_ts),
        room=nio.MatrixRoom("!room:localhost", "@mindroom:localhost"),
        source_kind=IMAGE_SOURCE_KIND,
    )


def _coalescing_gate_is_idle(gate: CoalescingGate) -> bool:
    return not gate._gates


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


async def _none_after(release: asyncio.Event) -> ReadyPendingEvent | None:
    await release.wait()
    return None


async def _admit_ready(
    gate: CoalescingGate,
    key: CoalescingKey,
    pending_event: PendingEvent,
) -> None:
    """Admit one already-ready event through the canonical gate API."""
    await gate.admit(
        key,
        source_event_id=pending_event.event.event_id,
        source_kind=pending_event.source_kind,
        ready_result=ReadyPendingEvent(pending_event=pending_event),
    )


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

    assert gate._older_owner_reservations(key, before_order=reservation.received_order + 1) == [reservation]

    reservation.release()
    reservation.release()

    assert gate._older_owner_reservations(key, before_order=reservation.received_order + 1) == []
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
    assert queued.receipt_time == reservation.receipt_time
    assert queued.received_at == 1_000.0
    assert pending.enqueue_time != reservation.receipt_time

    await gate.drain_all()


@pytest.mark.asyncio
async def test_reservation_receipt_time_bounds_debounce_claim_window() -> None:
    """Late admission must not widen a receive-time debounce window."""
    batches: list[CoalescedBatch] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 0.3,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")
    first_reservation = gate.reserve_order(
        room_id=key.room_id,
        requester_user_id=key.requester_user_id,
        receipt_time=1.0,
    )
    second_reservation = gate.reserve_order(
        room_id=key.room_id,
        requester_user_id=key.requester_user_id,
        receipt_time=1.5,
    )

    await gate.admit(
        key,
        ready_result=ReadyPendingEvent(pending_event=_pending(_text_event("$second:localhost", "second", 1_000_500))),
        order_reservation=second_reservation,
    )
    await asyncio.sleep(0)
    assert batches == []

    await gate.admit(
        key,
        ready_result=ReadyPendingEvent(pending_event=_pending(_text_event("$first:localhost", "first", 1_000_000))),
        order_reservation=first_reservation,
    )

    await _wait_for(
        lambda: [batch.source_event_ids for batch in batches] == [["$first:localhost"], ["$second:localhost"]],
    )
    await gate.drain_all()


@pytest.mark.asyncio
async def test_command_barrier_does_not_widen_receive_time_debounce_window() -> None:
    """A late barrier should not make old same-thread prompts coalesce."""
    batches: list[CoalescedBatch] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 0.3,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")
    first_reservation = gate.reserve_order(
        room_id=key.room_id,
        requester_user_id=key.requester_user_id,
        receipt_time=1.0,
    )
    second_reservation = gate.reserve_order(
        room_id=key.room_id,
        requester_user_id=key.requester_user_id,
        receipt_time=1.5,
    )
    command_reservation = gate.reserve_order(
        room_id=key.room_id,
        requester_user_id=key.requester_user_id,
        receipt_time=1.6,
    )

    await gate.admit(
        key,
        ready_result=ReadyPendingEvent(pending_event=_pending(_text_event("$second:localhost", "second", 1_000_500))),
        order_reservation=second_reservation,
    )
    await gate.admit(
        key,
        ready_result=ReadyPendingEvent(pending_event=_pending(_text_event("$command:localhost", "!help", 1_000_600))),
        order_reservation=command_reservation,
    )
    await asyncio.sleep(0)
    assert batches == []

    await gate.admit(
        key,
        ready_result=ReadyPendingEvent(pending_event=_pending(_text_event("$first:localhost", "first", 1_000_000))),
        order_reservation=first_reservation,
    )

    await _wait_for(
        lambda: (
            [batch.source_event_ids for batch in batches]
            == [["$first:localhost"], ["$second:localhost"], ["$command:localhost"]]
        ),
    )
    await gate.drain_all()


def test_active_follow_up_source_kind_is_not_coalescing_exempt() -> None:
    """Active-follow-up is dispatch policy, not a source-kind bypass."""
    event = _text_event("$active:localhost", "follow-up", 1_000_000)

    assert not is_coalescing_exempt_source_kind(event, ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND)


def test_batch_construction_does_not_close_mixed_solo_metadata() -> None:
    """Batch construction must be pure; claimed segment owner owns cleanup."""
    close_count = 0

    def close() -> None:
        nonlocal close_count
        close_count += 1

    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")
    solo = _pending(_text_event("$solo:localhost", "solo", 1_000_000))
    solo.dispatch_metadata = (
        PendingDispatchMetadata(kind="solo", payload=object(), close=close, requires_solo_batch=True),
    )
    normal = _pending(_text_event("$normal:localhost", "normal", 1_000_001))

    with pytest.raises(ValueError, match="requires solo batches"):
        build_coalesced_batch(key, [solo, normal])

    assert close_count == 0


def test_single_prepared_event_handoff_synthesizes_canonical_thread_relation() -> None:
    """A canonical batch key must control dispatch target for non-voice prepared events too."""
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")
    prepared = PreparedTextEvent(
        sender="@user:localhost",
        event_id="$sidecar:localhost",
        body="sidecar preview",
        source={
            "event_id": "$sidecar:localhost",
            "sender": "@user:localhost",
            "origin_server_ts": 1_000_000,
            "room_id": "!room:localhost",
            "type": "m.room.message",
            "content": {"msgtype": "m.text", "body": "sidecar preview"},
        },
        server_timestamp=1_000_000,
        source_kind_override=MESSAGE_SOURCE_KIND,
    )
    pending = PendingEvent(
        event=prepared,
        room=nio.MatrixRoom("!room:localhost", "@mindroom:localhost"),
        source_kind=MESSAGE_SOURCE_KIND,
    )

    handoff = build_dispatch_handoff(build_coalesced_batch(key, [pending]))

    assert handoff.event.source["content"]["m.relates_to"] == {
        "rel_type": "m.thread",
        "event_id": "$thread:localhost",
    }


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

    await _admit_ready(gate, key, _pending(_text_event("$gmail:localhost", "gmail setup", 1_000_000)))
    await _admit_ready(gate, key, _pending(_text_event("$extras:localhost", "message extras", 1_000_600)))

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

    await _admit_ready(gate, key, _pending(_text_event("$first:localhost", "first", 1_000_000)))
    await _admit_ready(gate, key, _pending(_text_event("$second:localhost", "second", 1_000_600)))

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

    await _admit_ready(gate, key, _pending(_text_event("$text:localhost", "describe this", 1_000_000)))
    await asyncio.sleep(0.01)
    await _admit_ready(gate, key, _pending(_image_event("$image:localhost", 1_000_600)))

    await _wait_for(lambda: len(batches) >= 1)

    assert [batch.source_event_ids for batch in batches] == [["$text:localhost", "$image:localhost"]]


@pytest.mark.asyncio
async def test_upload_grace_waits_for_same_window_unresolved_media_reservation() -> None:
    """Upload grace must not flush text before a reserved media ingress can admit."""
    batches: list[CoalescedBatch] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 0.0,
        upload_grace_seconds=lambda: 0.01,
        is_shutting_down=lambda: False,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")

    await _admit_ready(gate, key, _pending(_text_event("$text:localhost", "describe this", 1_000_000)))
    gate_entry = gate._gates[key]
    await _wait_for(lambda: gate_entry.phase is GatePhase.GRACE)
    reservation = gate.reserve_order(room_id=key.room_id, requester_user_id=key.requester_user_id)
    await asyncio.sleep(0.06)

    assert batches == []

    await gate.admit(
        key,
        ready_result=ReadyPendingEvent(pending_event=_image_pending("$image:localhost", 1_000_600)),
        source_kind=IMAGE_SOURCE_KIND,
        order_reservation=reservation,
    )
    await gate.drain_all()

    assert [batch.source_event_ids for batch in batches] == [["$text:localhost", "$image:localhost"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("barrier_kind", ["command", "bypass"])
async def test_upload_grace_barrier_does_not_wait_for_later_unresolved_reservation(barrier_kind: str) -> None:
    """A command/bypass after upload-grace text must not wait for later unresolved ingress."""
    batches: list[CoalescedBatch] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 0.0,
        upload_grace_seconds=lambda: 0.01,
        is_shutting_down=lambda: False,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")

    await _admit_ready(gate, key, _pending(_text_event("$text:localhost", "describe this", 1_000_000)))
    gate_entry = gate._gates[key]
    await _wait_for(lambda: gate_entry.phase is GatePhase.GRACE)

    if barrier_kind == "command":
        barrier = _pending(_text_event("$barrier:localhost", "!help", 1_000_001))
    else:
        barrier = _pending(_text_event("$barrier:localhost", "solo", 1_000_001))
        barrier.dispatch_metadata = (
            PendingDispatchMetadata(kind="solo", payload=object(), close=lambda: None, requires_solo_batch=True),
        )
    await _admit_ready(gate, key, barrier)
    later_reservation = gate.reserve_order(room_id=key.room_id, requester_user_id=key.requester_user_id)

    await _wait_for(
        lambda: [batch.source_event_ids for batch in batches] == [["$text:localhost"], ["$barrier:localhost"]],
    )

    later_reservation.release()
    await gate.drain_all()


@pytest.mark.asyncio
async def test_upload_grace_does_not_flatten_late_solo_ready_segment() -> None:
    """Solo metadata discovered during upload grace must remain in its own segment."""
    batches: list[CoalescedBatch] = []
    release_solo = asyncio.Event()
    close_count = 0

    def close() -> None:
        nonlocal close_count
        close_count += 1

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 0.0,
        upload_grace_seconds=lambda: 0.01,
        is_shutting_down=lambda: False,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")
    solo = _pending(_text_event("$solo:localhost", "solo", 1_000_001))
    solo.dispatch_metadata = (
        PendingDispatchMetadata(kind="solo", payload=object(), close=close, requires_solo_batch=True),
    )

    await _admit_ready(gate, key, _pending(_text_event("$text:localhost", "describe this", 1_000_000)))
    gate_entry = gate._gates[key]
    await _wait_for(lambda: gate_entry.phase is GatePhase.GRACE)
    await gate.admit(
        key,
        ready_task=asyncio.create_task(_ready_after(release_solo, solo)),
        source_event_id="$solo:localhost",
        source_kind=MESSAGE_SOURCE_KIND,
    )
    release_solo.set()
    await gate.drain_all()

    assert [batch.source_event_ids for batch in batches] == [["$text:localhost"], ["$solo:localhost"]]
    assert close_count == 0


@pytest.mark.asyncio
async def test_unresolved_solo_ready_event_dispatches_solo_without_metadata_close() -> None:
    """A ready task may reveal solo metadata after claim; split it before batch construction."""
    batches: list[CoalescedBatch] = []
    release_ready = asyncio.Event()
    close_count = 0

    def close() -> None:
        nonlocal close_count
        close_count += 1

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 0.0,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")
    solo = _pending(_text_event("$solo:localhost", "solo", 1_000_000))
    solo.dispatch_metadata = (
        PendingDispatchMetadata(kind="solo", payload=object(), close=close, requires_solo_batch=True),
    )

    await gate.admit(
        key,
        ready_task=asyncio.create_task(_ready_after(release_ready, solo)),
        source_event_id="$solo:localhost",
        source_kind="message",
    )
    await _admit_ready(gate, key, _pending(_text_event("$normal:localhost", "normal", 1_000_001)))
    release_ready.set()

    await _wait_for(
        lambda: [batch.source_event_ids for batch in batches] == [["$solo:localhost"], ["$normal:localhost"]],
    )

    assert close_count == 0
    assert batches[0].dispatch_metadata == solo.dispatch_metadata


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

    await _admit_ready(
        gate,
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

    await _admit_ready(gate, key, _pending(_text_event("$first:localhost", "first", 1_000_000)))
    await _admit_ready(gate, key, _pending(_text_event("$second:localhost", "second", 1_000_600)))

    await gate.drain_all()

    assert [batch.source_event_ids for batch in batches] == [["$first:localhost", "$second:localhost"]]
    assert "quick succession" in batches[0].prompt


@pytest.mark.asyncio
async def test_threaded_debounce_uses_trailing_quiet_time() -> None:
    """A later threaded message inside the debounce window should extend the quiet deadline."""
    batches: list[CoalescedBatch] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 0.05,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")

    await _admit_ready(gate, key, _pending(_text_event("$first:localhost", "first", 1_000_000)))
    await asyncio.sleep(0.04)
    await _admit_ready(gate, key, _pending(_text_event("$second:localhost", "second", 1_000_040)))
    await asyncio.sleep(0.02)

    assert batches == []

    await _wait_for(
        lambda: [batch.source_event_ids for batch in batches] == [["$first:localhost", "$second:localhost"]],
    )


@pytest.mark.asyncio
async def test_unresolved_reservation_wait_does_not_mark_queued_events_as_in_flight_buffered() -> None:
    """Events queued before dispatch starts must still obey debounce gaps after the blocker resolves."""
    batches: list[CoalescedBatch] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 0.02,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")
    blocker = gate.reserve_order(room_id=key.room_id, requester_user_id=key.requester_user_id)

    await asyncio.sleep(0.05)
    await _admit_ready(gate, key, _pending(_text_event("$second:localhost", "second", 1_000_001)))
    await asyncio.sleep(0.05)
    await _admit_ready(gate, key, _pending(_text_event("$third:localhost", "third", 1_000_002)))
    await asyncio.sleep(0.05)

    assert batches == []

    await gate.admit(
        key,
        ready_result=ReadyPendingEvent(
            pending_event=_pending(_text_event("$first:localhost", "first", 1_000_000)),
        ),
        order_reservation=blocker,
    )
    await _wait_for(lambda: len(batches) >= 3)

    assert [batch.source_event_ids for batch in batches] == [
        ["$first:localhost"],
        ["$second:localhost"],
        ["$third:localhost"],
    ]


@pytest.mark.asyncio
async def test_in_flight_unresolved_reservations_stay_buffered_after_admission() -> None:
    """Reservations created during dispatch should stay in the in-flight follow-up window."""
    batches: list[CoalescedBatch] = []
    release_dispatch = asyncio.Event()

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)
        if batch.source_event_ids == ["$first:localhost"]:
            await release_dispatch.wait()

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 0.02,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")

    await _admit_ready(gate, key, _pending(_text_event("$first:localhost", "first", 1_000_000)))
    await _wait_for(lambda: [batch.source_event_ids for batch in batches] == [["$first:localhost"]])

    second_reservation = gate.reserve_order(room_id=key.room_id, requester_user_id=key.requester_user_id)
    await asyncio.sleep(0.05)
    third_reservation = gate.reserve_order(room_id=key.room_id, requester_user_id=key.requester_user_id)
    release_dispatch.set()
    await asyncio.sleep(0.01)
    await gate.admit(
        key,
        ready_result=ReadyPendingEvent(pending_event=_pending(_text_event("$second:localhost", "second", 1_000_001))),
        order_reservation=second_reservation,
    )
    await gate.admit(
        key,
        ready_result=ReadyPendingEvent(pending_event=_pending(_text_event("$third:localhost", "third", 1_000_002))),
        order_reservation=third_reservation,
    )
    await gate.drain_all()

    assert [batch.source_event_ids for batch in batches] == [
        ["$first:localhost"],
        ["$second:localhost", "$third:localhost"],
    ]


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
    await _admit_ready(gate, key, _pending(_text_event("$typed:localhost", "typed follow-up", 1_000_800)))

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

    await _admit_ready(gate, newer_key, _pending(_text_event("$newer:localhost", "newer", 1_000_001)))
    await _admit_ready(gate, older_key, _pending(_text_event("$older-later:localhost", "older later", 1_000_002)))
    fail_voice.set()

    await _wait_for(
        lambda: [batch.source_event_ids for batch in batches] == [["$newer:localhost"], ["$older-later:localhost"]],
        deadline_seconds=2.0,
    )


@pytest.mark.asyncio
async def test_bounded_shutdown_marks_internal_drain_failure_incomplete() -> None:
    """Unexpected drain failures during shutdown must make checkpointing unsafe."""
    gate = CoalescingGate(
        dispatch_batch=AsyncMock(),
        debounce_seconds=lambda: 60.0,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: True,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")
    await _admit_ready(gate, key, _pending(_text_event("$text:localhost", "typed", 1_000_000)))

    async def fail_resolve(_gate: object, _admissions: object) -> list[object]:
        msg = "internal drain failed"
        raise RuntimeError(msg)

    gate._resolve_claimed_admissions = fail_resolve

    result = await gate.drain_all(ready_timeout_seconds=0.01)

    assert result.completed is False
    assert result.dispatch_failure_count == 1


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


@pytest.mark.asyncio
async def test_debounce_waits_for_later_same_owner_reservation_inside_window() -> None:
    """Debounce should wait for unresolved same-owner ingress inside the quiet window."""
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

    await _admit_ready(gate, key, _pending(_text_event("$text:localhost", "typed first", 1_000_000)))
    await asyncio.sleep(0.005)
    reservation = gate.reserve_order(room_id=key.room_id, requester_user_id=key.requester_user_id)
    await asyncio.sleep(0.05)

    assert batches == []

    await gate.admit(
        key,
        ready_result=ReadyPendingEvent(pending_event=_voice_pending("$voice:localhost", "voice second", 1_000_005)),
        source_kind=VOICE_SOURCE_KIND,
        order_reservation=reservation,
    )
    await gate.drain_all()

    assert [batch.source_event_ids for batch in batches] == [["$text:localhost", "$voice:localhost"]]


@pytest.mark.asyncio
async def test_debounce_does_not_wait_for_later_reservation_outside_window() -> None:
    """Reservations after the quiet window should not delay the already-ready prompt."""
    batches: list[CoalescedBatch] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 0.01,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")

    await _admit_ready(gate, key, _pending(_text_event("$text:localhost", "typed first", 1_000_000)))
    await asyncio.sleep(0.03)
    reservation = gate.reserve_order(room_id=key.room_id, requester_user_id=key.requester_user_id)
    await asyncio.sleep(0.01)

    assert [batch.source_event_ids for batch in batches] == [["$text:localhost"]]

    await gate.admit(
        key,
        ready_result=ReadyPendingEvent(pending_event=_voice_pending("$voice:localhost", "voice later", 1_000_050)),
        source_kind=VOICE_SOURCE_KIND,
        order_reservation=reservation,
    )
    await gate.drain_all()

    assert [batch.source_event_ids for batch in batches] == [["$text:localhost"], ["$voice:localhost"]]


@pytest.mark.asyncio
async def test_debounce_still_releases_prompt_when_command_barrier_arrives() -> None:
    """A command barrier should still cut short a long normal debounce."""
    batches: list[CoalescedBatch] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 60.0,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")

    await _admit_ready(gate, key, _pending(_text_event("$text:localhost", "normal", 1_000_000)))
    await _admit_ready(gate, key, _pending(_text_event("$command:localhost", "!help", 1_000_001)))

    await _wait_for(
        lambda: [batch.source_event_ids for batch in batches] == [["$text:localhost"], ["$command:localhost"]],
    )


@pytest.mark.asyncio
async def test_command_barrier_does_not_wait_for_unresolved_reservation_after_barrier() -> None:
    """A reservation after a command barrier must not delay work before the barrier."""
    batches: list[CoalescedBatch] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 60.0,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")

    await _admit_ready(gate, key, _pending(_text_event("$text:localhost", "normal", 1_000_000)))
    await _admit_ready(gate, key, _pending(_text_event("$command:localhost", "!help", 1_000_001)))
    reservation = gate.reserve_order(room_id=key.room_id, requester_user_id=key.requester_user_id)

    await _wait_for(
        lambda: [batch.source_event_ids for batch in batches] == [["$text:localhost"], ["$command:localhost"]],
    )

    await gate.admit(
        key,
        ready_result=ReadyPendingEvent(pending_event=_voice_pending("$voice:localhost", "voice", 1_000_002)),
        source_kind=VOICE_SOURCE_KIND,
        order_reservation=reservation,
    )
    await gate.drain_all()

    assert [batch.source_event_ids for batch in batches] == [
        ["$text:localhost"],
        ["$command:localhost"],
        ["$voice:localhost"],
    ]


@pytest.mark.asyncio
async def test_bypass_barrier_does_not_wait_for_unresolved_reservation_after_barrier() -> None:
    """A reservation after a bypass barrier must not delay work before the barrier."""
    batches: list[CoalescedBatch] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 60.0,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")
    bypass = _pending(_text_event("$bypass:localhost", "solo", 1_000_001))
    bypass.dispatch_metadata = (
        PendingDispatchMetadata(kind="test", payload=object(), close=lambda: None, requires_solo_batch=True),
    )

    await _admit_ready(gate, key, _pending(_text_event("$text:localhost", "normal", 1_000_000)))
    await _admit_ready(gate, key, bypass)
    reservation = gate.reserve_order(room_id=key.room_id, requester_user_id=key.requester_user_id)

    await _wait_for(
        lambda: [batch.source_event_ids for batch in batches] == [["$text:localhost"], ["$bypass:localhost"]],
    )

    await gate.admit(
        key,
        ready_result=ReadyPendingEvent(pending_event=_voice_pending("$voice:localhost", "voice", 1_000_002)),
        source_kind=VOICE_SOURCE_KIND,
        order_reservation=reservation,
    )
    await gate.drain_all()

    assert [batch.source_event_ids for batch in batches] == [
        ["$text:localhost"],
        ["$bypass:localhost"],
        ["$voice:localhost"],
    ]


@pytest.mark.asyncio
async def test_front_command_does_not_wait_for_later_unresolved_reservation() -> None:
    """A front command should wait for older unresolved work only."""
    batches: list[CoalescedBatch] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 60.0,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")

    await _admit_ready(gate, key, _pending(_text_event("$command:localhost", "!help", 1_000_000)))
    reservation = gate.reserve_order(room_id=key.room_id, requester_user_id=key.requester_user_id)

    await _wait_for(lambda: [batch.source_event_ids for batch in batches] == [["$command:localhost"]])

    await gate.admit(
        key,
        ready_result=ReadyPendingEvent(pending_event=_voice_pending("$voice:localhost", "voice", 1_000_001)),
        source_kind=VOICE_SOURCE_KIND,
        order_reservation=reservation,
    )
    await gate.drain_all()

    assert [batch.source_event_ids for batch in batches] == [["$command:localhost"], ["$voice:localhost"]]


@pytest.mark.asyncio
async def test_front_bypass_does_not_wait_for_later_unresolved_reservation() -> None:
    """A front bypass should wait for older unresolved work only."""
    batches: list[CoalescedBatch] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 60.0,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")
    bypass = _pending(_text_event("$bypass:localhost", "solo", 1_000_000))
    bypass.dispatch_metadata = (
        PendingDispatchMetadata(kind="test", payload=object(), close=lambda: None, requires_solo_batch=True),
    )

    await _admit_ready(gate, key, bypass)
    reservation = gate.reserve_order(room_id=key.room_id, requester_user_id=key.requester_user_id)

    await _wait_for(lambda: [batch.source_event_ids for batch in batches] == [["$bypass:localhost"]])

    await gate.admit(
        key,
        ready_result=ReadyPendingEvent(pending_event=_voice_pending("$voice:localhost", "voice", 1_000_001)),
        source_kind=VOICE_SOURCE_KIND,
        order_reservation=reservation,
    )
    await gate.drain_all()

    assert [batch.source_event_ids for batch in batches] == [["$bypass:localhost"], ["$voice:localhost"]]


@pytest.mark.asyncio
async def test_claim_count_stops_before_unresolved_older_reservation() -> None:
    """A normal claim must not cross an unresolved older same-owner reservation."""
    batches: list[CoalescedBatch] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 0.0,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")

    await _admit_ready(gate, key, _pending(_text_event("$first:localhost", "first", 1_000_000)))
    reservation = gate.reserve_order(
        room_id=key.room_id,
        requester_user_id=key.requester_user_id,
        receipt_time=time.monotonic() + 60.0,
    )
    await _admit_ready(gate, key, _pending(_text_event("$third:localhost", "third", 1_000_002)))

    await _wait_for(lambda: [batch.source_event_ids for batch in batches] == [["$first:localhost"]])
    assert gate._gates[key].queue[0].source_event_id == "$third:localhost"

    reservation.release()
    await gate.drain_all()

    assert [batch.source_event_ids for batch in batches] == [["$first:localhost"], ["$third:localhost"]]


@pytest.mark.asyncio
async def test_different_canonical_threads_do_not_serialize_after_admission() -> None:
    """Same-owner canonical thread gates should dispatch independently after admission."""
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    batches: list[list[str]] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch.source_event_ids)
        if batch.coalescing_key.thread_id == "$thread-a:localhost":
            first_started.set()
            await release_first.wait()

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 0.0,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    first_key = CoalescingKey("!room:localhost", "$thread-a:localhost", "@user:localhost")
    second_key = CoalescingKey("!room:localhost", "$thread-b:localhost", "@user:localhost")

    await _admit_ready(gate, first_key, _pending(_text_event("$first:localhost", "first", 1_000_000)))
    await _wait_for(first_started.is_set)
    await _admit_ready(gate, second_key, _pending(_text_event("$second:localhost", "second", 1_000_001)))

    await _wait_for(
        lambda: [ids for ids in batches if ids == ["$second:localhost"]] == [["$second:localhost"]],
    )
    release_first.set()
    await gate.drain_all()

    assert batches == [["$first:localhost"], ["$second:localhost"]]


@pytest.mark.asyncio
async def test_zero_ready_claim_clears_claimed_state_and_wakes_waiters() -> None:
    """A claimed admission that resolves to None should clear and let later work drain."""
    batches: list[CoalescedBatch] = []
    release_none = asyncio.Event()

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 0.0,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    first_key = CoalescingKey("!room:localhost", "$thread-a:localhost", "@user:localhost")
    second_key = CoalescingKey("!room:localhost", "$thread-b:localhost", "@user:localhost")

    await gate.admit(
        first_key,
        ready_task=asyncio.create_task(_none_after(release_none)),
        source_event_id="$none:localhost",
    )
    first_gate = gate._gates[first_key]
    await _wait_for(lambda: bool(first_gate.claimed_admissions))

    release_none.set()
    await _wait_for(lambda: not first_gate.claimed_admissions)
    await _admit_ready(gate, second_key, _pending(_text_event("$later:localhost", "later", 1_000_001)))
    await gate.drain_all()

    assert [batch.source_event_ids for batch in batches] == [["$later:localhost"]]


@pytest.mark.asyncio
async def test_partial_ready_failure_dispatches_ready_events_and_clears_claim() -> None:
    """A partial ready failure should dispatch surviving events and clear the claim."""
    batches: list[CoalescedBatch] = []
    release_none = asyncio.Event()

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 0.0,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")

    await _admit_ready(gate, key, _pending(_text_event("$ready:localhost", "ready", 1_000_000)))
    await gate.admit(
        key,
        ready_task=asyncio.create_task(_none_after(release_none)),
        source_event_id="$none:localhost",
    )
    gate_entry = gate._gates[key]
    await _wait_for(lambda: len(gate_entry.claimed_admissions) == 2)

    release_none.set()
    await gate.drain_all()

    assert [batch.source_event_ids for batch in batches] == [["$ready:localhost"]]
    assert _coalescing_gate_is_idle(gate)


@pytest.mark.asyncio
async def test_cancelled_resolve_requeues_claimed_admissions() -> None:
    """Cancelling while resolving readiness should put the claimed admission back."""
    release_ready = asyncio.Event()

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        _ = batch

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 0.0,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: True,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")
    pending = _pending(_text_event("$slow:localhost", "slow", 1_000_000))

    await gate.admit(key, ready_task=asyncio.create_task(_ready_after(release_ready, pending)))
    gate_entry = gate._gates[key]
    await _wait_for(lambda: bool(gate_entry.claimed_admissions))
    [claimed] = gate_entry.claimed_admissions

    assert gate_entry.drain_task is not None
    gate_entry.drain_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await gate_entry.drain_task

    assert list(gate_entry.queue) == [claimed]
    assert gate_entry.claimed_admissions == []

    release_ready.set()
    await gate.drain_all()


@pytest.mark.asyncio
async def test_upload_grace_requeue_removes_admissions_from_claimed_state() -> None:
    """Upload grace should requeue claimed text before awaiting the grace timer."""
    batches: list[CoalescedBatch] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 0.0,
        upload_grace_seconds=lambda: 0.2,
        is_shutting_down=lambda: False,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")

    await _admit_ready(gate, key, _pending(_text_event("$text:localhost", "text", 1_000_000)))
    gate_entry = gate._gates[key]

    await _wait_for(lambda: gate_entry.phase is GatePhase.GRACE)
    assert gate_entry.claimed_admissions == []
    assert [queued.source_event_id for queued in gate_entry.queue] == ["$text:localhost"]

    await gate.drain_all()
    assert [batch.source_event_ids for batch in batches] == [["$text:localhost"]]


@pytest.mark.asyncio
async def test_failed_room_media_signal_does_not_merge_surviving_room_text_roots() -> None:
    """A dropped media-like admission must not make room-level text roots coalesce."""
    batches: list[CoalescedBatch] = []
    release_media = asyncio.Event()

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 0.0,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    key = CoalescingKey("!room:localhost", None, "@user:localhost")

    await _admit_ready(gate, key, _pending(_text_event("$first:localhost", "first", 1_000_000)))
    await gate.admit(
        key,
        ready_task=asyncio.create_task(_none_after(release_media)),
        source_event_id="$image:localhost",
        source_kind=IMAGE_SOURCE_KIND,
    )
    await _admit_ready(gate, key, _pending(_text_event("$second:localhost", "second", 1_000_002)))

    gate_entry = gate._gates[key]
    await _wait_for(lambda: len(gate_entry.claimed_admissions) == 3)
    release_media.set()
    await gate.drain_all()

    assert [batch.source_event_ids for batch in batches] == [
        ["$first:localhost"],
        ["$second:localhost"],
    ]


@pytest.mark.asyncio
async def test_same_window_reservation_resolving_to_different_thread_waits_then_splits() -> None:
    """A same-window unresolved event should hold debounce, then dispatch under its resolved key."""
    batches: list[tuple[CoalescingKey, list[str]]] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append((batch.coalescing_key, batch.source_event_ids))

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 0.03,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    first_key = CoalescingKey("!room:localhost", "$thread-a:localhost", "@user:localhost")
    second_key = CoalescingKey("!room:localhost", "$thread-b:localhost", "@user:localhost")

    await _admit_ready(gate, first_key, _pending(_text_event("$first:localhost", "first", 1_000_000)))
    await asyncio.sleep(0.005)
    reservation = gate.reserve_order(room_id=first_key.room_id, requester_user_id=first_key.requester_user_id)
    await asyncio.sleep(0.05)

    assert batches == []

    await gate.admit(
        second_key,
        ready_result=ReadyPendingEvent(
            pending_event=_pending(_text_event("$second:localhost", "second", 1_000_010)),
        ),
        order_reservation=reservation,
    )
    await gate.drain_all()

    assert sorted(batches) == sorted(
        [
            (first_key, ["$first:localhost"]),
            (second_key, ["$second:localhost"]),
        ],
    )


@pytest.mark.asyncio
async def test_multi_segment_claim_remains_visible_until_last_segment_finishes() -> None:
    """Claimed admissions should remain visible while split dispatch segments run."""
    release_media = asyncio.Event()
    first_dispatch_started = asyncio.Event()
    release_first_dispatch = asyncio.Event()
    batches: list[list[str]] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch.source_event_ids)
        if batch.source_event_ids == ["$first:localhost"]:
            first_dispatch_started.set()
            await release_first_dispatch.wait()

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 0.0,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    key = CoalescingKey("!room:localhost", None, "@user:localhost")

    await _admit_ready(gate, key, _pending(_text_event("$first:localhost", "first", 1_000_000)))
    await gate.admit(
        key,
        ready_task=asyncio.create_task(_none_after(release_media)),
        source_event_id="$image:localhost",
        source_kind=IMAGE_SOURCE_KIND,
    )
    await _admit_ready(gate, key, _pending(_text_event("$second:localhost", "second", 1_000_002)))

    gate_entry = gate._gates[key]
    await _wait_for(lambda: len(gate_entry.claimed_admissions) == 3)
    release_media.set()
    await _wait_for(first_dispatch_started.is_set)

    assert len(gate_entry.claimed_admissions) == 3

    release_first_dispatch.set()
    await gate.drain_all()

    assert batches == [["$first:localhost"], ["$second:localhost"]]
