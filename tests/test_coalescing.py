"""Tests for live inbound message coalescing."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import nio
import pytest

from mindroom.cancellation import SYNC_RESTART_CANCEL_MSG
from mindroom.coalescing import (
    CoalescingGate,
    IngressAdmissionClosedError,
    ReadyPendingEvent,
    is_coalescing_exempt_source_kind,
)
from mindroom.coalescing_batch import (
    CoalescingKey,
    PendingEvent,
    active_follow_up_coalescing_key,
    build_coalesced_batch,
)
from mindroom.dispatch_handoff import PendingDispatchMetadata, PreparedTextEvent, build_dispatch_handoff
from mindroom.dispatch_source import (
    ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND,
    IMAGE_SOURCE_KIND,
    MESSAGE_SOURCE_KIND,
    VOICE_SOURCE_KIND,
)
from mindroom.ingress_lanes import LaneDelivery
from mindroom.runtime_shutdown import SYNC_RESTART_SHUTDOWN

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


@pytest.mark.asyncio
async def test_enter_lane_stamps_local_monotonic_receipt_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lane slots should capture local monotonic receipt time."""
    fake_clock = FakeMonotonicClock(10.0)
    monkeypatch.setattr(time, "monotonic", fake_clock)
    gate = CoalescingGate(
        dispatch_batch=AsyncMock(),
        debounce_seconds=lambda: 0.3,
        is_shutting_down=lambda: False,
    )

    first = gate.enter_lane(room_id="!room:localhost", sender_id="@user:localhost")
    fake_clock.advance(0.5)
    second = gate.enter_lane(room_id="!room:localhost", sender_id="@user:localhost")

    assert first.receipt_time == 10.0
    assert second.receipt_time == 10.5

    gate.release_lane_slot(first)
    gate.release_lane_slot(second)
    await first.settled.wait()
    await second.settled.wait()


@pytest.mark.asyncio
async def test_submit_rejects_released_lane_slot() -> None:
    """Late submission must not recreate work after the lane slot was released."""
    gate = CoalescingGate(
        dispatch_batch=AsyncMock(),
        debounce_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    slot = gate.enter_lane(room_id="!room:localhost", sender_id="@user:localhost")
    gate.release_lane_slot(slot)

    with pytest.raises(IngressAdmissionClosedError):
        gate.submit_lane_slot(
            slot,
            key=CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost"),
            source_event_id="$late:localhost",
            source_kind=MESSAGE_SOURCE_KIND,
            ready_result=ReadyPendingEvent(
                pending_event=_pending(_text_event("$late:localhost", "late", 1000)),
            ),
        )

    await gate.drain_all()
    assert _coalescing_gate_is_idle(gate)


@pytest.mark.asyncio
async def test_late_lane_delivery_combines_queued_text_backlog_in_receipt_order() -> None:
    """Text queued behind late lane delivery dispatches as one combined turn in receipt order."""
    batches: list[CoalescedBatch] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 0.3,
        is_shutting_down=lambda: False,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")
    first_slot = gate.enter_lane(room_id=key.room_id, sender_id=key.requester_user_id, receipt_time=1.0)
    second_slot = gate.enter_lane(room_id=key.room_id, sender_id=key.requester_user_id, receipt_time=1.5)

    gate.submit_lane_slot(
        second_slot,
        key=key,
        source_event_id="$second:localhost",
        source_kind=MESSAGE_SOURCE_KIND,
        ready_result=ReadyPendingEvent(pending_event=_pending(_text_event("$second:localhost", "second", 1_000_500))),
    )
    await asyncio.sleep(0)
    assert batches == []

    gate.submit_lane_slot(
        first_slot,
        key=key,
        source_event_id="$first:localhost",
        source_kind=MESSAGE_SOURCE_KIND,
        ready_result=ReadyPendingEvent(pending_event=_pending(_text_event("$first:localhost", "first", 1_000_000))),
    )

    await _wait_for(
        lambda: [batch.source_event_ids for batch in batches] == [["$first:localhost", "$second:localhost"]],
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
async def test_room_level_text_dispatches_before_late_media() -> None:
    """A room-level text root dispatches immediately; late media becomes its own turn."""
    batches: list[CoalescedBatch] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 1.0,
        is_shutting_down=lambda: False,
    )
    key = CoalescingKey("!room:localhost", None, "@user:localhost")

    await _admit_ready(gate, key, _pending(_text_event("$text:localhost", "describe this", 1_000_000)))
    await _wait_for(lambda: [batch.source_event_ids for batch in batches] == [["$text:localhost"]])

    await _admit_ready(gate, key, _pending(_image_event("$image:localhost", 1_000_600)))
    await gate.drain_all()

    assert [batch.source_event_ids for batch in batches] == [["$text:localhost"], ["$image:localhost"]]


@pytest.mark.asyncio
async def test_text_dispatch_waits_for_same_window_unready_media_lane_slot() -> None:
    """An immediate text flush must not run before an in-window unready media slot delivers."""
    batches: list[CoalescedBatch] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 1.0,
        is_shutting_down=lambda: False,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")

    await _admit_ready(gate, key, _pending(_text_event("$text:localhost", "describe this", 1_000_000)))
    slot = gate.enter_lane(room_id=key.room_id, sender_id=key.requester_user_id)
    await asyncio.sleep(0.05)

    assert batches == []

    gate.submit_lane_slot(
        slot,
        key=key,
        source_event_id="$image:localhost",
        source_kind=IMAGE_SOURCE_KIND,
        ready_result=ReadyPendingEvent(pending_event=_image_pending("$image:localhost", 1_000_600)),
    )
    await gate.drain_all()

    assert [batch.source_event_ids for batch in batches] == [["$text:localhost", "$image:localhost"]]


@pytest.mark.asyncio
async def test_bypass_barrier_does_not_wait_for_later_undelivered_lane_slot() -> None:
    """A solo-bypass admission dispatches without waiting for a later undelivered lane slot."""
    batches: list[CoalescedBatch] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 60.0,
        is_shutting_down=lambda: False,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")
    bypass = _pending(_text_event("$bypass:localhost", "solo", 1_000_000))
    bypass.dispatch_metadata = (
        PendingDispatchMetadata(kind="solo", payload=object(), close=lambda: None, requires_solo_batch=True),
    )

    await _admit_ready(gate, key, bypass)
    slot = gate.enter_lane(room_id=key.room_id, sender_id=key.requester_user_id)

    await _wait_for(lambda: [batch.source_event_ids for batch in batches] == [["$bypass:localhost"]])
    assert not slot.settled.is_set()

    gate.release_lane_slot(slot)
    await gate.drain_all()


@pytest.mark.asyncio
async def test_voice_transcript_dispatches_without_debounce_wait() -> None:
    """Voice transcripts are complete utterances and skip the media debounce wait."""
    batches: list[CoalescedBatch] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 60.0,
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
        is_shutting_down=lambda: False,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")

    await _admit_ready(gate, key, _pending(_text_event("$first:localhost", "first", 1_000_000)))
    await _admit_ready(gate, key, _pending(_text_event("$second:localhost", "second", 1_000_600)))

    await gate.drain_all()

    assert [batch.source_event_ids for batch in batches] == [["$first:localhost", "$second:localhost"]]
    assert "quick succession" in batches[0].prompt


@pytest.mark.asyncio
async def test_threaded_media_debounce_uses_trailing_quiet_time() -> None:
    """A later media upload inside the debounce window should extend the quiet deadline."""
    batches: list[CoalescedBatch] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 0.05,
        is_shutting_down=lambda: False,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")

    await _admit_ready(gate, key, _image_pending("$first:localhost", 1_000_000))
    await asyncio.sleep(0.01)
    await _admit_ready(gate, key, _image_pending("$second:localhost", 1_000_040))
    await asyncio.sleep(0.02)

    assert batches == []

    await _wait_for(
        lambda: [batch.source_event_ids for batch in batches] == [["$first:localhost", "$second:localhost"]],
    )


@pytest.mark.asyncio
async def test_lone_text_dispatches_without_debounce_wait() -> None:
    """A lone text message is a complete utterance and never waits for the debounce window."""
    batches: list[CoalescedBatch] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 60.0,
        is_shutting_down=lambda: False,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")

    await _admit_ready(gate, key, _pending(_text_event("$text:localhost", "instant", 1_000_000)))

    await _wait_for(lambda: [batch.source_event_ids for batch in batches] == [["$text:localhost"]])
    await gate.drain_all()


@pytest.mark.asyncio
async def test_trailing_caption_closes_media_batch_immediately() -> None:
    """A trailing text caption completes a media batch and flushes before the window expires."""
    batches: list[CoalescedBatch] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 60.0,
        is_shutting_down=lambda: False,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")

    await _admit_ready(gate, key, _image_pending("$one:localhost", 1_000_000))
    await _admit_ready(gate, key, _image_pending("$two:localhost", 1_000_100))
    await asyncio.sleep(0.05)

    assert batches == []

    await _admit_ready(gate, key, _pending(_text_event("$caption:localhost", "caption", 1_000_200)))

    await _wait_for(
        lambda: (
            [batch.source_event_ids for batch in batches]
            == [["$one:localhost", "$two:localhost", "$caption:localhost"]]
        ),
    )
    await gate.drain_all()


@pytest.mark.asyncio
async def test_active_follow_up_backlog_ignores_debounce_gaps_after_idle() -> None:
    """Same-target follow-ups queued behind one active response flush as one ordered backlog."""
    calls: list[tuple[list[str], str]] = []
    idle = asyncio.Event()
    key = active_follow_up_coalescing_key("!room:localhost", "$thread:localhost")

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        calls.append((list(batch.source_event_ids), batch.prompt))

    async def wait_until_dispatch_allowed(wait_key: CoalescingKey) -> None:
        if wait_key == key:
            await idle.wait()

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 0.01,
        is_shutting_down=lambda: False,
        wait_until_dispatch_allowed=wait_until_dispatch_allowed,
    )

    for event_id, body, requester_user_id in (
        ("$a1:localhost", "first follow-up", "@alice:localhost"),
        ("$b1:localhost", "extra context", "@bob:localhost"),
        ("$a2:localhost", "reply to bob", "@alice:localhost"),
    ):
        await _admit_ready(
            gate,
            key,
            PendingEvent(
                event=_text_event(event_id, body, 1_000_000),
                room=nio.MatrixRoom("!room:localhost", "@mindroom:localhost"),
                source_kind=MESSAGE_SOURCE_KIND,
                requester_user_id=requester_user_id,
                dispatch_policy_source_kind=ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND,
            ),
        )
        await asyncio.sleep(0.03)

    assert calls == []

    idle.set()
    await _wait_for(lambda: calls != [])

    assert calls == [
        (
            ["$a1:localhost", "$b1:localhost", "$a2:localhost"],
            "Messages arrived while the previous response was still running. "
            "They are in chat timeline order. Respond once to the combined context:\n\n"
            "<queued_messages>\n"
            '<msg event_id="$a1:localhost" from="@alice:localhost"><![CDATA[first follow-up]]></msg>\n'
            '<msg event_id="$b1:localhost" from="@bob:localhost"><![CDATA[extra context]]></msg>\n'
            '<msg event_id="$a2:localhost" from="@alice:localhost"><![CDATA[reply to bob]]></msg>\n'
            "</queued_messages>",
        ),
    ]


@pytest.mark.asyncio
async def test_media_tailed_follow_up_backlog_flushes_immediately_at_idle() -> None:
    """A follow-up backlog ending in media flushes at idle without a debounce wait.

    Once the conversation idles, later ingress is admitted under the live key
    and could never join the held backlog, so holding it would only add latency.
    """
    calls: list[list[str]] = []
    idle = asyncio.Event()
    key = active_follow_up_coalescing_key("!room:localhost", "$thread:localhost")

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        calls.append(list(batch.source_event_ids))

    async def wait_until_dispatch_allowed(wait_key: CoalescingKey) -> None:
        if wait_key == key:
            await idle.wait()

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 60.0,
        is_shutting_down=lambda: False,
        wait_until_dispatch_allowed=wait_until_dispatch_allowed,
    )

    await _admit_ready(
        gate,
        key,
        PendingEvent(
            event=_image_event("$img:localhost", 1_000_000),
            room=nio.MatrixRoom("!room:localhost", "@mindroom:localhost"),
            source_kind=IMAGE_SOURCE_KIND,
            requester_user_id="@user:localhost",
            dispatch_policy_source_kind=ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND,
        ),
    )
    await asyncio.sleep(0.01)
    assert calls == []

    idle.set()
    await _wait_for(lambda: calls == [["$img:localhost"]])
    assert _coalescing_gate_is_idle(gate)


@pytest.mark.asyncio
async def test_different_thread_normal_gate_does_not_wait_behind_older_active_backlog() -> None:
    """Other-thread work must dispatch while this target's active backlog still waits."""
    batches: list[list[str]] = []
    active_wait_started = asyncio.Event()
    release_active_wait = asyncio.Event()
    active_key = active_follow_up_coalescing_key("!room:localhost", "$thread:localhost")
    normal_key = CoalescingKey("!room:localhost", "$other-thread:localhost", "@bob:localhost")

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(list(batch.source_event_ids))

    async def wait_until_dispatch_allowed(wait_key: CoalescingKey) -> None:
        if wait_key == active_key:
            active_wait_started.set()
            await release_active_wait.wait()

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
        wait_until_dispatch_allowed=wait_until_dispatch_allowed,
    )

    await _admit_ready(
        gate,
        active_key,
        PendingEvent(
            event=_text_event("$active:localhost", "queued while active", 1_000_000),
            room=nio.MatrixRoom("!room:localhost", "@mindroom:localhost"),
            source_kind=MESSAGE_SOURCE_KIND,
            requester_user_id="@alice:localhost",
            dispatch_policy_source_kind=ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND,
        ),
    )
    await active_wait_started.wait()

    slot = gate.enter_lane(room_id=normal_key.room_id, sender_id=normal_key.requester_user_id)
    gate.submit_lane_slot(
        slot,
        key=normal_key,
        source_event_id="$normal:localhost",
        source_kind=MESSAGE_SOURCE_KIND,
        ready_result=ReadyPendingEvent(
            pending_event=_pending(_text_event("$normal:localhost", "later normal", 1_000_001)),
        ),
    )
    await _wait_for(lambda: batches == [["$normal:localhost"]])

    release_active_wait.set()
    await gate.drain_all()

    assert batches == [["$normal:localhost"], ["$active:localhost"]]


@pytest.mark.asyncio
async def test_unready_lane_slot_backlog_combines_into_one_turn() -> None:
    """Text queued behind an unready lane slot dispatches as one combined turn on release."""
    batches: list[CoalescedBatch] = []
    release_first = asyncio.Event()

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 0.02,
        is_shutting_down=lambda: False,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")
    first_pending = _pending(_text_event("$first:localhost", "first", 1_000_000))
    first_slot = gate.enter_lane(room_id=key.room_id, sender_id=key.requester_user_id)
    gate.submit_lane_slot(
        first_slot,
        key=key,
        source_event_id="$first:localhost",
        source_kind=MESSAGE_SOURCE_KIND,
        ready_task=asyncio.create_task(_ready_after(release_first, first_pending)),
    )

    await asyncio.sleep(0.05)
    second_slot = gate.enter_lane(room_id=key.room_id, sender_id=key.requester_user_id)
    gate.submit_lane_slot(
        second_slot,
        key=key,
        source_event_id="$second:localhost",
        source_kind=MESSAGE_SOURCE_KIND,
        ready_result=ReadyPendingEvent(pending_event=_pending(_text_event("$second:localhost", "second", 1_000_001))),
    )
    await asyncio.sleep(0.05)
    third_slot = gate.enter_lane(room_id=key.room_id, sender_id=key.requester_user_id)
    gate.submit_lane_slot(
        third_slot,
        key=key,
        source_event_id="$third:localhost",
        source_kind=MESSAGE_SOURCE_KIND,
        ready_result=ReadyPendingEvent(pending_event=_pending(_text_event("$third:localhost", "third", 1_000_002))),
    )
    await asyncio.sleep(0.05)

    assert batches == []

    release_first.set()
    await _wait_for(lambda: len(batches) >= 1)

    assert [batch.source_event_ids for batch in batches] == [
        ["$first:localhost", "$second:localhost", "$third:localhost"],
    ]


@pytest.mark.asyncio
async def test_voice_readiness_delay_combines_backlog_in_receipt_order() -> None:
    """A slow STT result holds later text in the lane window, then both flush as one turn."""
    batches: list[CoalescedBatch] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 0.03,
        is_shutting_down=lambda: False,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")
    voice_ready = asyncio.Event()

    voice_pending = _voice_pending("$voice:localhost", "voice transcript", 1_000_000)
    voice_slot = gate.enter_lane(room_id=key.room_id, sender_id=key.requester_user_id)
    gate.submit_lane_slot(
        voice_slot,
        key=key,
        source_event_id="$voice:localhost",
        source_kind=VOICE_SOURCE_KIND,
        ready_task=asyncio.create_task(_ready_after(voice_ready, voice_pending)),
        received_at=1_000.0,
    )
    await asyncio.sleep(0.08)
    await _admit_ready(gate, key, _pending(_text_event("$typed:localhost", "typed follow-up", 1_000_800)))

    assert batches == []

    voice_ready.set()
    await _wait_for(
        lambda: [batch.source_event_ids for batch in batches] == [["$voice:localhost", "$typed:localhost"]],
    )


@pytest.mark.asyncio
async def test_failed_lane_ready_task_does_not_block_later_lane_work() -> None:
    """A raising ready task settles its slot so later same-lane work still dispatches."""
    batches: list[CoalescedBatch] = []
    fail_voice = asyncio.Event()

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    async def failed_voice() -> ReadyPendingEvent:
        await fail_voice.wait()
        msg = "voice failed"
        raise RuntimeError(msg)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")
    voice_slot = gate.enter_lane(room_id=key.room_id, sender_id=key.requester_user_id)
    gate.submit_lane_slot(
        voice_slot,
        key=key,
        source_event_id="$voice:localhost",
        source_kind=VOICE_SOURCE_KIND,
        ready_task=asyncio.create_task(failed_voice()),
    )
    later_slot = gate.enter_lane(room_id=key.room_id, sender_id=key.requester_user_id)
    gate.submit_lane_slot(
        later_slot,
        key=key,
        source_event_id="$later:localhost",
        source_kind=MESSAGE_SOURCE_KIND,
        ready_result=ReadyPendingEvent(pending_event=_pending(_text_event("$later:localhost", "later", 1_000_002))),
    )
    fail_voice.set()

    await _wait_for(lambda: [batch.source_event_ids for batch in batches] == [["$later:localhost"]])
    assert voice_slot.settled.is_set()


@pytest.mark.asyncio
async def test_lane_admission_does_not_wait_for_its_own_unsettled_slot() -> None:
    """A lane-admitted event is already ready and must not wait for its own slot to settle."""
    batches: list[CoalescedBatch] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")
    slot = gate.enter_lane(room_id=key.room_id, sender_id=key.requester_user_id)
    ready = ReadyPendingEvent(
        pending_event=_pending(_text_event("$lane:localhost", "lane text", 1_000_002)),
    )
    delivery = LaneDelivery(
        key=key,
        source_event_id="$lane:localhost",
        source_kind=MESSAGE_SOURCE_KIND,
        ready_result=ready,
        ready_task=None,
        received_at=1_000.0,
    )

    try:
        await gate._admit_from_lane(slot, delivery, ready)
        await _wait_for(lambda: [batch.source_event_ids for batch in batches] == [["$lane:localhost"]])
        assert not slot.settled.is_set()
    finally:
        gate.release_lane_slot(slot)
        await gate.drain_all()


@pytest.mark.asyncio
async def test_bounded_shutdown_marks_internal_drain_failure_incomplete() -> None:
    """Unexpected drain failures during shutdown must make checkpointing unsafe."""
    gate = CoalescingGate(
        dispatch_batch=AsyncMock(),
        debounce_seconds=lambda: 60.0,
        is_shutting_down=lambda: True,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")
    await _admit_ready(gate, key, _pending(_text_event("$text:localhost", "typed", 1_000_000)))

    async def fail_dispatch_claim(
        _key: CoalescingKey,
        _gate: object,
        _admissions: object,
    ) -> None:
        msg = "internal drain failed"
        raise RuntimeError(msg)

    gate._dispatch_claim = fail_dispatch_claim

    result = await gate.drain_all(ready_timeout_seconds=0.01)

    assert result.completed is False
    assert result.dispatch_failure_count == 1


@pytest.mark.asyncio
async def test_bounded_shutdown_times_out_stuck_in_flight_dispatch() -> None:
    """Bounded shutdown must return unsafe instead of hanging on a stuck dispatch."""
    dispatch_started = asyncio.Event()
    release_dispatch = asyncio.Event()
    cancelled_args: list[tuple[object, ...]] = []

    async def dispatch_batch(_batch: CoalescedBatch) -> None:
        dispatch_started.set()
        try:
            await release_dispatch.wait()
        except asyncio.CancelledError as exc:
            cancelled_args.append(exc.args)
            raise

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 0.0,
        is_shutting_down=lambda: True,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")
    await _admit_ready(gate, key, _pending(_text_event("$text:localhost", "typed", 1_000_000)))
    await asyncio.wait_for(dispatch_started.wait(), timeout=0.5)

    drain_task = asyncio.create_task(gate.drain_all(ready_timeout_seconds=0.01))
    try:
        result = await asyncio.wait_for(asyncio.shield(drain_task), timeout=0.2)
    except TimeoutError:  # pragma: no cover - documents the failure mode on regression
        pytest.fail("bounded drain hung behind in-flight dispatch")
    finally:
        release_dispatch.set()
        if not drain_task.done():
            drain_task.cancel()
            await asyncio.gather(drain_task, return_exceptions=True)

    assert result.completed is False
    assert result.dispatch_cancelled_count == 1
    assert cancelled_args == [()]


@pytest.mark.asyncio
async def test_bounded_shutdown_preserves_shutdown_intent_for_drain_tasks() -> None:
    """Bounded sync-restart drains should preserve restart provenance for in-flight dispatch."""
    dispatch_started = asyncio.Event()
    release_dispatch = asyncio.Event()
    cancelled_args: list[tuple[object, ...]] = []

    async def dispatch_batch(_batch: CoalescedBatch) -> None:
        dispatch_started.set()
        try:
            await release_dispatch.wait()
        except asyncio.CancelledError as exc:
            cancelled_args.append(exc.args)
            raise

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 0.0,
        is_shutting_down=lambda: True,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")
    await _admit_ready(gate, key, _pending(_text_event("$text:localhost", "typed", 1_000_000)))
    await asyncio.wait_for(dispatch_started.wait(), timeout=0.5)

    drain_task = asyncio.create_task(
        gate.drain_all(
            ready_timeout_seconds=0.01,
            shutdown_intent=SYNC_RESTART_SHUTDOWN,
        ),
    )
    try:
        result = await asyncio.wait_for(asyncio.shield(drain_task), timeout=0.2)
    except TimeoutError:  # pragma: no cover - documents the failure mode on regression
        pytest.fail("bounded drain hung behind in-flight dispatch")
    finally:
        release_dispatch.set()
        if not drain_task.done():
            drain_task.cancel()
            await asyncio.gather(drain_task, return_exceptions=True)

    assert result.completed is False
    assert result.dispatch_cancelled_count == 1
    assert cancelled_args == [(SYNC_RESTART_CANCEL_MSG,)]


@pytest.mark.asyncio
async def test_bounded_drain_does_not_wait_forever_on_external_dispatch_gate() -> None:
    """A bounded drain must not wait indefinitely for an active-follow-up idle gate."""
    calls: list[list[str]] = []
    dispatch_wait_started = asyncio.Event()
    release_dispatch_wait = asyncio.Event()
    key = active_follow_up_coalescing_key("!room:localhost", "$thread:localhost")

    async def wait_until_dispatch_allowed(wait_key: CoalescingKey) -> None:
        if wait_key == key:
            dispatch_wait_started.set()
            await release_dispatch_wait.wait()

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        calls.append(list(batch.source_event_ids))

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
        wait_until_dispatch_allowed=wait_until_dispatch_allowed,
    )
    await _admit_ready(
        gate,
        key,
        PendingEvent(
            event=_text_event("$text:localhost", "typed", 1_000_000),
            room=nio.MatrixRoom("!room:localhost", "@mindroom:localhost"),
            source_kind=MESSAGE_SOURCE_KIND,
            requester_user_id="@user:localhost",
            dispatch_policy_source_kind=ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND,
        ),
    )
    await dispatch_wait_started.wait()

    drain_task = asyncio.create_task(gate.drain_all(ready_timeout_seconds=0.01))
    try:
        result = await asyncio.wait_for(asyncio.shield(drain_task), timeout=0.2)
    except TimeoutError:  # pragma: no cover - documents the failure mode on regression
        pytest.fail("bounded drain hung behind external dispatch gate")
    finally:
        release_dispatch_wait.set()
        if not drain_task.done():
            drain_task.cancel()
            await asyncio.gather(drain_task, return_exceptions=True)

    assert result.completed is True
    assert calls == [["$text:localhost"]]


@pytest.mark.asyncio
async def test_bounded_shutdown_closes_metadata_for_abandoned_ready_work() -> None:
    """Gate-owned metadata must close before bounded shutdown discards failed queued work."""
    close_count = 0

    def close_metadata() -> None:
        nonlocal close_count
        close_count += 1

    gate = CoalescingGate(
        dispatch_batch=AsyncMock(),
        debounce_seconds=lambda: 60.0,
        is_shutting_down=lambda: True,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")
    pending = _pending(_text_event("$text:localhost", "typed", 1_000_000))
    pending.dispatch_metadata = (PendingDispatchMetadata(kind="test", payload=object(), close=close_metadata),)
    await gate.admit(key, ready_result=ReadyPendingEvent(pending_event=pending))

    async def fail_dispatch_claim(
        _key: CoalescingKey,
        _gate: object,
        _admissions: object,
    ) -> None:
        msg = "internal drain failed"
        raise RuntimeError(msg)

    gate._dispatch_claim = fail_dispatch_claim

    result = await gate.drain_all(ready_timeout_seconds=0.01)

    assert result.completed is False
    assert result.dispatch_failure_count == 1
    assert result.dropped_ready_count == 1
    assert close_count == 1
    assert gate._gates == {}


@pytest.mark.asyncio
async def test_drain_all_waits_for_lane_slot_to_admit() -> None:
    """Unbounded drains must treat undelivered lane slots as pending ingress work."""
    batches: list[CoalescedBatch] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 0.0,
        is_shutting_down=lambda: True,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")
    slot = gate.enter_lane(room_id=key.room_id, sender_id=key.requester_user_id)

    drain_task = asyncio.create_task(gate.drain_all())
    await _wait_for(lambda: gate._active_drain_context is not None and not drain_task.done())
    assert drain_task.done() is False
    assert slot.released is False

    gate.submit_lane_slot(
        slot,
        key=key,
        source_event_id="$voice:localhost",
        source_kind=VOICE_SOURCE_KIND,
        ready_result=ReadyPendingEvent(
            pending_event=_voice_pending("$voice:localhost", "voice transcript", 1_000_000),
        ),
    )
    await asyncio.wait_for(drain_task, timeout=10.0)

    assert [batch.source_event_ids for batch in batches] == [["$voice:localhost"]]


@pytest.mark.asyncio
async def test_debounce_does_not_wait_for_later_lane_slot_outside_window() -> None:
    """A slot entered after the quiet window should not delay the already-ready prompt."""
    batches: list[CoalescedBatch] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 0.01,
        is_shutting_down=lambda: False,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")

    await _admit_ready(gate, key, _pending(_text_event("$text:localhost", "typed first", 1_000_000)))
    await asyncio.sleep(0.03)
    slot = gate.enter_lane(room_id=key.room_id, sender_id=key.requester_user_id)
    await asyncio.sleep(0.01)

    assert [batch.source_event_ids for batch in batches] == [["$text:localhost"]]

    gate.submit_lane_slot(
        slot,
        key=key,
        source_event_id="$voice:localhost",
        source_kind=VOICE_SOURCE_KIND,
        ready_result=ReadyPendingEvent(pending_event=_voice_pending("$voice:localhost", "voice later", 1_000_050)),
    )
    await gate.drain_all()

    assert [batch.source_event_ids for batch in batches] == [["$text:localhost"], ["$voice:localhost"]]


@pytest.mark.asyncio
async def test_ready_text_waits_behind_unready_older_voice_lane_slot() -> None:
    """Ready text behind an unready voice slot must not deliver until the voice resolves."""
    batches: list[CoalescedBatch] = []
    release_voice = asyncio.Event()

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")
    voice_pending = _voice_pending("$voice:localhost", "voice first", 1_000_000)
    voice_slot = gate.enter_lane(room_id=key.room_id, sender_id=key.requester_user_id)
    gate.submit_lane_slot(
        voice_slot,
        key=key,
        source_event_id="$voice:localhost",
        source_kind=VOICE_SOURCE_KIND,
        ready_task=asyncio.create_task(_ready_after(release_voice, voice_pending)),
    )
    text_slot = gate.enter_lane(room_id=key.room_id, sender_id=key.requester_user_id)
    gate.submit_lane_slot(
        text_slot,
        key=key,
        source_event_id="$text:localhost",
        source_kind=MESSAGE_SOURCE_KIND,
        ready_result=ReadyPendingEvent(pending_event=_pending(_text_event("$text:localhost", "text", 1_000_002))),
    )

    await asyncio.sleep(0.01)
    assert batches == []
    assert _coalescing_gate_is_idle(gate)
    assert not text_slot.settled.is_set()

    release_voice.set()
    await gate.drain_all()

    dispatched_ids = [event_id for batch in batches for event_id in batch.source_event_ids]
    assert dispatched_ids == ["$voice:localhost", "$text:localhost"]


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
async def test_none_resolving_lane_slot_settles_without_residue() -> None:
    """A ready task resolving to None settles its slot so later same-lane work dispatches."""
    batches: list[CoalescedBatch] = []
    release_none = asyncio.Event()

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")
    none_slot = gate.enter_lane(room_id=key.room_id, sender_id=key.requester_user_id)
    gate.submit_lane_slot(
        none_slot,
        key=key,
        source_event_id="$none:localhost",
        source_kind=MESSAGE_SOURCE_KIND,
        ready_task=asyncio.create_task(_none_after(release_none)),
    )
    later_slot = gate.enter_lane(room_id=key.room_id, sender_id=key.requester_user_id)
    gate.submit_lane_slot(
        later_slot,
        key=key,
        source_event_id="$later:localhost",
        source_kind=MESSAGE_SOURCE_KIND,
        ready_result=ReadyPendingEvent(pending_event=_pending(_text_event("$later:localhost", "later", 1_000_001))),
    )

    release_none.set()
    await gate.drain_all()

    assert [batch.source_event_ids for batch in batches] == [["$later:localhost"]]
    assert none_slot.settled.is_set()
    assert _coalescing_gate_is_idle(gate)


@pytest.mark.asyncio
async def test_partial_ready_failure_dispatches_ready_events_and_clears_claim() -> None:
    """One failing member of a same-window burst is skipped while survivors dispatch."""
    batches: list[CoalescedBatch] = []
    release_none = asyncio.Event()

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 0.02,
        is_shutting_down=lambda: False,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")
    ready_slot = gate.enter_lane(room_id=key.room_id, sender_id=key.requester_user_id)
    gate.submit_lane_slot(
        ready_slot,
        key=key,
        source_event_id="$ready:localhost",
        source_kind=MESSAGE_SOURCE_KIND,
        ready_result=ReadyPendingEvent(pending_event=_pending(_text_event("$ready:localhost", "ready", 1_000_000))),
    )
    none_slot = gate.enter_lane(room_id=key.room_id, sender_id=key.requester_user_id)
    gate.submit_lane_slot(
        none_slot,
        key=key,
        source_event_id="$none:localhost",
        source_kind=MESSAGE_SOURCE_KIND,
        ready_task=asyncio.create_task(_none_after(release_none)),
    )

    await asyncio.sleep(0.05)
    assert batches == []

    release_none.set()
    await gate.drain_all()

    assert [batch.source_event_ids for batch in batches] == [["$ready:localhost"]]
    assert _coalescing_gate_is_idle(gate)


@pytest.mark.asyncio
async def test_same_window_lane_slot_resolving_to_different_thread_waits_then_splits() -> None:
    """An in-window unready same-sender slot holds debounce, then dispatches under its resolved key."""
    batches: list[tuple[CoalescingKey, list[str]]] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append((batch.coalescing_key, batch.source_event_ids))

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 0.03,
        is_shutting_down=lambda: False,
    )
    first_key = CoalescingKey("!room:localhost", "$thread-a:localhost", "@user:localhost")
    second_key = CoalescingKey("!room:localhost", "$thread-b:localhost", "@user:localhost")

    await _admit_ready(gate, first_key, _image_pending("$first:localhost", 1_000_000))
    await asyncio.sleep(0.005)
    slot = gate.enter_lane(room_id=first_key.room_id, sender_id=first_key.requester_user_id)
    await asyncio.sleep(0.05)

    assert batches == []

    gate.submit_lane_slot(
        slot,
        key=second_key,
        source_event_id="$second:localhost",
        source_kind=MESSAGE_SOURCE_KIND,
        ready_result=ReadyPendingEvent(
            pending_event=_pending(_text_event("$second:localhost", "second", 1_000_010)),
        ),
    )
    await gate.drain_all()

    assert sorted(batches) == sorted(
        [
            (first_key, ["$first:localhost"]),
            (second_key, ["$second:localhost"]),
        ],
    )


@pytest.mark.asyncio
async def test_batch_order_follows_lane_receipt_order_not_readiness_order() -> None:
    """One coalesced batch must keep lane receipt order even when readiness completes in reverse."""
    batches: list[CoalescedBatch] = []
    release_first = asyncio.Event()

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 0.5,
        is_shutting_down=lambda: False,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")
    first_pending = _pending(_text_event("$first:localhost", "first", 1_000_000))
    first_slot = gate.enter_lane(room_id=key.room_id, sender_id=key.requester_user_id, receipt_time=1.0)
    second_slot = gate.enter_lane(room_id=key.room_id, sender_id=key.requester_user_id, receipt_time=1.2)

    gate.submit_lane_slot(
        first_slot,
        key=key,
        source_event_id="$first:localhost",
        source_kind=MESSAGE_SOURCE_KIND,
        ready_task=asyncio.create_task(_ready_after(release_first, first_pending)),
    )
    gate.submit_lane_slot(
        second_slot,
        key=key,
        source_event_id="$second:localhost",
        source_kind=MESSAGE_SOURCE_KIND,
        ready_result=ReadyPendingEvent(pending_event=_pending(_text_event("$second:localhost", "second", 1_000_200))),
    )
    release_first.set()

    await gate.drain_all()

    assert [batch.source_event_ids for batch in batches] == [["$first:localhost", "$second:localhost"]]


@pytest.mark.asyncio
async def test_messages_in_different_rooms_do_not_coalesce() -> None:
    """Same-user messages in different rooms stay independent batches."""
    batches: list[CoalescedBatch] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 1.0,
        is_shutting_down=lambda: False,
    )
    first_key = CoalescingKey("!room-a:localhost", "$thread:localhost", "@user:localhost")
    second_key = CoalescingKey("!room-b:localhost", "$thread:localhost", "@user:localhost")

    await _admit_ready(gate, first_key, _pending(_text_event("$a:localhost", "room a", 1_000_000)))
    await _admit_ready(gate, second_key, _pending(_text_event("$b:localhost", "room b", 1_000_100)))

    await gate.drain_all()

    assert sorted(batch.source_event_ids for batch in batches) == [["$a:localhost"], ["$b:localhost"]]


@pytest.mark.asyncio
async def test_messages_in_different_threads_do_not_coalesce() -> None:
    """Same-room messages in different threads stay independent batches."""
    batches: list[CoalescedBatch] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 1.0,
        is_shutting_down=lambda: False,
    )
    first_key = CoalescingKey("!room:localhost", "$thread-a:localhost", "@user:localhost")
    second_key = CoalescingKey("!room:localhost", "$thread-b:localhost", "@user:localhost")

    await _admit_ready(gate, first_key, _pending(_text_event("$a:localhost", "thread a", 1_000_000)))
    await _admit_ready(gate, second_key, _pending(_text_event("$b:localhost", "thread b", 1_000_100)))

    await gate.drain_all()

    assert sorted(batch.source_event_ids for batch in batches) == [["$a:localhost"], ["$b:localhost"]]


@pytest.mark.asyncio
async def test_drain_all_flushes_pending_debounced_work_and_idles_gate() -> None:
    """Shutdown drain dispatches queued work without waiting out the debounce window."""
    batches: list[CoalescedBatch] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 60.0,
        is_shutting_down=lambda: False,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")
    await _admit_ready(gate, key, _pending(_text_event("$pending:localhost", "pending", 1_000_000)))
    assert batches == []

    result = await gate.drain_all()

    assert result.completed is True
    assert [batch.source_event_ids for batch in batches] == [["$pending:localhost"]]
    assert _coalescing_gate_is_idle(gate)
