"""Tests for live inbound message coalescing."""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
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
    ActiveFollowUpCoalescingOwner,
    CoalescingKey,
    PendingEvent,
    PreparedTurn,
    RequesterCoalescingOwner,
    active_follow_up_coalescing_key,
    build_prepared_turn,
    is_active_follow_up_coalescing_key,
    requester_coalescing_key,
)
from mindroom.config.main import Config
from mindroom.dispatch_handoff import PendingDispatchMetadata, PreparedIngress
from mindroom.dispatch_recovery_context import turn_dispatch_recovery_active, turn_dispatch_recovery_scope
from mindroom.dispatch_source import (
    ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND,
    IMAGE_SOURCE_KIND,
    MESSAGE_SOURCE_KIND,
    VOICE_SOURCE_KIND,
)
from mindroom.execution_preparation import _messages_with_current_prompt
from mindroom.ingress_lanes import LaneDelivery, ReceiptLaneKey
from mindroom.runtime_shutdown import SYNC_RESTART_SHUTDOWN
from mindroom.timestamp_formatting import format_timestamp_ms
from tests.conftest import make_pending_event

if TYPE_CHECKING:
    from collections.abc import Callable


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
    return make_pending_event(
        event,
        nio.MatrixRoom("!room:localhost", "@mindroom:localhost"),
        source_kind="message",
    )


def _image_pending(event_id: str, origin_server_ts: int) -> PendingEvent:
    """Wrap one image event as pending media ingress."""
    return make_pending_event(
        _image_event(event_id, origin_server_ts),
        nio.MatrixRoom("!room:localhost", "@mindroom:localhost"),
        source_kind=IMAGE_SOURCE_KIND,
    )


def _coalescing_gate_is_idle(gate: CoalescingGate) -> bool:
    return not gate._gates


def _voice_pending(event_id: str, body: str, origin_server_ts: int) -> PendingEvent:
    """Wrap one normalized voice transcript as pending voice ingress."""
    return make_pending_event(
        _text_event(event_id, body, origin_server_ts),
        nio.MatrixRoom("!room:localhost", "@mindroom:localhost"),
        source_kind=VOICE_SOURCE_KIND,
    )


def test_single_message_batch_is_not_structured() -> None:
    """A lone coalesced message stays unstructured and keeps its plain body as the prompt."""
    batch = build_prepared_turn(
        CoalescingKey("!room:localhost", "$thread:localhost", RequesterCoalescingOwner("@user:localhost")),
        [_pending(_text_event("$only:localhost", "just one", 1_774_019_700_000))],
        timestamp_formatter=lambda timestamp_ms: format_timestamp_ms(timestamp_ms, timezone="America/Los_Angeles"),
    )

    assert batch.current_prompt_is_structured is False
    assert batch.event.body == "just one"


def test_prepared_turn_carries_structured_flag_and_metadata() -> None:
    """A structured turn must carry its flag and per-message metadata to dispatch."""
    turn = build_prepared_turn(
        CoalescingKey("!room:localhost", "$thread:localhost", RequesterCoalescingOwner("@user:localhost")),
        [
            _pending(_text_event("$a1:localhost", "first", 1_774_019_700_000)),
            _pending(_text_event("$a2:localhost", "second", 1_774_019_760_000)),
        ],
        timestamp_formatter=lambda timestamp_ms: format_timestamp_ms(timestamp_ms, timezone="America/Los_Angeles"),
    )

    assert turn.current_prompt_is_structured is True
    assert set(turn.handled_turn.source_event_metadata) == {"$a1:localhost", "$a2:localhost"}


def test_prepared_turn_keeps_physical_event_and_carries_logical_batch() -> None:
    """A gate flush should produce one logical turn without synthesizing a Matrix event."""
    key = CoalescingKey("!room:localhost", "$thread:localhost", RequesterCoalescingOwner("@user:localhost"))
    first = _pending(_text_event("$a1:localhost", "first", 1_774_019_700_000))
    primary = _pending(_text_event("$a2:localhost", "second", 1_774_019_760_000))

    turn = build_prepared_turn(
        key,
        [first, primary],
        timestamp_formatter=lambda timestamp_ms: format_timestamp_ms(
            timestamp_ms,
            timezone="America/Los_Angeles",
        ),
    )

    assert turn.event.event_id == primary.event.event_id
    assert turn.event.source is primary.event.source
    assert turn.event.body.startswith("The user sent the following messages in quick succession.")
    assert turn.handled_turn.source_event_ids == ("$a1:localhost", "$a2:localhost")
    assert set(turn.handled_turn.source_event_metadata) == {"$a1:localhost", "$a2:localhost"}
    assert turn.current_prompt_is_structured is True


def test_active_follow_up_prompt_renders_timestamp_attributes() -> None:
    """Queued message tags should carry per-message local timestamps."""
    key = active_follow_up_coalescing_key("!room:localhost", "$thread:localhost")
    batch = build_prepared_turn(
        key,
        [
            make_pending_event(
                _text_event("$a1:localhost", "first", 1_774_019_700_000),
                nio.MatrixRoom("!room:localhost", "@mindroom:localhost"),
                source_kind=MESSAGE_SOURCE_KIND,
                requester_user_id="@alice:localhost",
                dispatch_policy_source_kind=ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND,
            ),
            make_pending_event(
                _text_event("$a2:localhost", "second", 1_774_019_760_000),
                nio.MatrixRoom("!room:localhost", "@mindroom:localhost"),
                source_kind=MESSAGE_SOURCE_KIND,
                requester_user_id="@alice:localhost",
                dispatch_policy_source_kind=ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND,
            ),
        ],
        timestamp_formatter=lambda timestamp_ms: format_timestamp_ms(timestamp_ms, timezone="America/Los_Angeles"),
    )

    assert batch.event.body == (
        "Messages arrived while the previous response was still running. "
        "They are in chat timeline order. Respond once to the combined context:\n\n"
        "<queued_messages>\n"
        '<msg event_id="$a1:localhost" from="@alice:localhost" ts="2026-03-20 08:15 PDT"><![CDATA[first]]></msg>\n'
        '<msg event_id="$a2:localhost" from="@alice:localhost" ts="2026-03-20 08:16 PDT"><![CDATA[second]]></msg>\n'
        "</queued_messages>"
    )


def test_requester_coalescing_key_wraps_requester_owner() -> None:
    """The requester helper derives the same key as the explicit owner construction."""
    assert requester_coalescing_key("!r", "$t", "@u") == CoalescingKey("!r", "$t", RequesterCoalescingOwner("@u"))


def test_is_active_follow_up_coalescing_key_matches_owner_variant_only() -> None:
    """The follow-up classifier reads the owner variant, never requester id text."""
    assert is_active_follow_up_coalescing_key(CoalescingKey("!r", None, ActiveFollowUpCoalescingOwner()))
    assert not is_active_follow_up_coalescing_key(CoalescingKey("!r", None, RequesterCoalescingOwner("@u")))
    legacy_prefixed = CoalescingKey("!r", None, RequesterCoalescingOwner("__mindroom_active_follow_up__:room"))
    assert not is_active_follow_up_coalescing_key(legacy_prefixed)


def test_coalescing_owner_variants_hash_into_distinct_gate_entries() -> None:
    """Owner variants hash differently and occupy distinct coalescing gate entries."""
    requester_key = CoalescingKey("!r", None, RequesterCoalescingOwner("@u"))
    follow_up_key = CoalescingKey("!r", None, ActiveFollowUpCoalescingOwner())
    assert hash(requester_key) != hash(follow_up_key)

    gate = CoalescingGate(
        dispatch_turn=AsyncMock(),
        debounce_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    gate._get_or_create_gate(requester_key)
    gate._get_or_create_gate(follow_up_key)
    assert set(gate._gates) == {requester_key, follow_up_key}


def test_tagged_coalesced_prompt_is_safe_inside_current_message_wrapper() -> None:
    """A structured coalesced prompt should not be wrapped in another message tag."""
    batch = build_prepared_turn(
        CoalescingKey("!room:localhost", "$thread:localhost", RequesterCoalescingOwner("@alice:localhost")),
        [
            make_pending_event(
                _text_event("$a1:localhost", "first <tag>", 1_774_019_700_000),
                nio.MatrixRoom("!room:localhost", "@mindroom:localhost"),
                source_kind=MESSAGE_SOURCE_KIND,
                requester_user_id="@alice:localhost",
            ),
            make_pending_event(
                _text_event("$a2:localhost", "second ]]> message", 1_774_019_760_000),
                nio.MatrixRoom("!room:localhost", "@mindroom:localhost"),
                source_kind=MESSAGE_SOURCE_KIND,
                requester_user_id="@alice:localhost",
            ),
        ],
        timestamp_formatter=lambda timestamp_ms: format_timestamp_ms(timestamp_ms, timezone="America/Los_Angeles"),
    )

    messages = _messages_with_current_prompt(
        batch.event.body,
        current_sender_id="@alice:localhost",
        current_timestamp_ms=1_774_019_760_000,
        current_prompt_is_structured=batch.current_prompt_is_structured,
        config=Config(timezone="America/Los_Angeles"),
    )

    content = messages[0].content
    assert content == (
        "Current message:\n"
        "The user sent the following messages in quick succession. "
        "Treat them as one turn and respond once:\n\n"
        "<messages>\n"
        '<msg event_id="$a1:localhost" from="@alice:localhost" ts="2026-03-20 08:15 PDT">'
        "<![CDATA[first <tag>]]></msg>\n"
        '<msg event_id="$a2:localhost" from="@alice:localhost" ts="2026-03-20 08:16 PDT">'
        "<![CDATA[second ]]]]><![CDATA[> message]]></msg>\n"
        "</messages>"
    )
    assert "&lt;" not in content


def test_structured_coalesced_prompt_with_model_tail_is_not_wrapped() -> None:
    """Trusted structured prompts should not depend on exact prompt suffixes."""
    prompt = (
        "The user sent the following messages in quick succession. "
        "Treat them as one turn and respond once:\n\n"
        "<messages>\n"
        '<msg event_id="$a1:localhost" from="@alice:localhost" ts="2026-03-20 08:15 PDT">'
        "<![CDATA[first]]></msg>\n"
        "</messages>\n\n"
        "Attachment context:\n- file.txt"
    )

    messages = _messages_with_current_prompt(
        prompt,
        current_sender_id="@alice:localhost",
        current_timestamp_ms=1_774_019_760_000,
        current_prompt_is_structured=True,
        config=Config(timezone="America/Los_Angeles"),
    )

    content = messages[0].content
    assert content == f"Current message:\n{prompt}"
    assert '<msg from="@alice:localhost"' not in content


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
        source_kind=pending_event.event.source_kind,
        ready_result=ReadyPendingEvent(pending_event=pending_event),
    )


@pytest.mark.asyncio
async def test_pending_source_event_tracks_queued_and_dispatched_work() -> None:
    """The durable callback boundary must know when coalescing owns one exact source."""
    release = asyncio.Event()

    async def dispatch_batch(_batch: PreparedTurn) -> None:
        await release.wait()

    gate = CoalescingGate(
        dispatch_turn=dispatch_batch,
        debounce_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", RequesterCoalescingOwner("@user:localhost"))
    pending = _pending(_text_event("$owned:localhost", "hello", 1_000_000))

    await _admit_ready(gate, key, pending)
    assert gate.has_pending_source_event("$owned:localhost")

    release.set()
    await _wait_for(lambda: not gate.has_pending_source_event("$owned:localhost"))


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
        dispatch_turn=AsyncMock(),
        debounce_seconds=lambda: 0.3,
        is_shutting_down=lambda: False,
    )

    first = gate.enter_lane(ReceiptLaneKey(room_id="!room:localhost", sender_id="@user:localhost"))
    fake_clock.advance(0.5)
    second = gate.enter_lane(ReceiptLaneKey(room_id="!room:localhost", sender_id="@user:localhost"))

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
        dispatch_turn=AsyncMock(),
        debounce_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    slot = gate.enter_lane(ReceiptLaneKey(room_id="!room:localhost", sender_id="@user:localhost"))
    gate.release_lane_slot(slot)

    with pytest.raises(IngressAdmissionClosedError):
        gate.submit_lane_slot(
            slot,
            key=CoalescingKey("!room:localhost", "$thread:localhost", RequesterCoalescingOwner("@user:localhost")),
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
    turns: list[PreparedTurn] = []

    async def dispatch_turn(turn: PreparedTurn) -> None:
        turns.append(turn)

    gate = CoalescingGate(
        dispatch_turn=dispatch_turn,
        debounce_seconds=lambda: 0.3,
        is_shutting_down=lambda: False,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", RequesterCoalescingOwner("@user:localhost"))
    first_slot = gate.enter_lane(
        ReceiptLaneKey(room_id=key.room_id, sender_id=key.owner.requester_user_id),
        receipt_time=1.0,
    )
    second_slot = gate.enter_lane(
        ReceiptLaneKey(room_id=key.room_id, sender_id=key.owner.requester_user_id),
        receipt_time=1.5,
    )

    gate.submit_lane_slot(
        second_slot,
        key=key,
        source_event_id="$second:localhost",
        source_kind=MESSAGE_SOURCE_KIND,
        ready_result=ReadyPendingEvent(pending_event=_pending(_text_event("$second:localhost", "second", 1_000_500))),
    )
    await asyncio.sleep(0)
    assert turns == []

    gate.submit_lane_slot(
        first_slot,
        key=key,
        source_event_id="$first:localhost",
        source_kind=MESSAGE_SOURCE_KIND,
        ready_result=ReadyPendingEvent(pending_event=_pending(_text_event("$first:localhost", "first", 1_000_000))),
    )

    await _wait_for(
        lambda: [turn.handled_turn.source_event_ids for turn in turns] == [("$first:localhost", "$second:localhost")],
    )
    assert all(isinstance(turn, PreparedTurn) for turn in turns)
    await gate.drain_all()


def test_active_follow_up_source_kind_is_not_coalescing_exempt() -> None:
    """Active-follow-up is dispatch policy, not a source-kind bypass."""
    event = _text_event("$active:localhost", "follow-up", 1_000_000)

    assert not is_coalescing_exempt_source_kind(event, ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND)


def test_pending_dispatch_metadata_closes_at_most_once() -> None:
    """Turn and gate cleanup may converge without releasing ownership twice."""
    close_count = 0

    def close() -> None:
        nonlocal close_count
        close_count += 1

    metadata = PendingDispatchMetadata(kind="test", payload=object(), close=close)

    metadata.close_once()
    metadata.close_once()

    assert close_count == 1


def test_single_prepared_turn_owns_final_dispatch_event_and_turn_record() -> None:
    """Prepared turn should carry final prompt and persistence state without later rebuilding."""
    key = CoalescingKey("!room:localhost", "$thread:localhost", RequesterCoalescingOwner("@user:localhost"))
    prepared = PreparedIngress(
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
    pending = make_pending_event(
        prepared,
        nio.MatrixRoom("!room:localhost", "@mindroom:localhost"),
        source_kind=MESSAGE_SOURCE_KIND,
    )

    turn = build_prepared_turn(key, [pending])

    assert turn.event.body == "sidecar preview"
    assert turn.event.source is pending.event.source
    assert turn.ingress.coalescing_key is not None
    assert turn.ingress.coalescing_key.thread_id == "$thread:localhost"
    assert "m.relates_to" not in turn.event.source["content"]
    assert turn.handled_turn.source_event_ids == ("$sidecar:localhost",)
    assert turn.handled_turn.source_event_prompts == {"$sidecar:localhost": "sidecar preview"}


@pytest.mark.asyncio
async def test_room_level_messages_do_not_coalesce() -> None:
    """Independent room-level messages must stay as separate model turns."""
    batches: list[PreparedTurn] = []

    async def dispatch_batch(batch: PreparedTurn) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_turn=dispatch_batch,
        debounce_seconds=lambda: 1.0,
        is_shutting_down=lambda: False,
    )
    key = CoalescingKey("!room:localhost", None, RequesterCoalescingOwner("@user:localhost"))

    await _admit_ready(gate, key, _pending(_text_event("$gmail:localhost", "gmail setup", 1_000_000)))
    await _admit_ready(gate, key, _pending(_text_event("$extras:localhost", "message extras", 1_000_600)))

    await gate.drain_all()

    assert [list(batch.handled_turn.source_event_ids) for batch in batches] == [
        ["$gmail:localhost"],
        ["$extras:localhost"],
    ]
    assert all("quick succession" not in batch.event.body for batch in batches)


@pytest.mark.asyncio
async def test_room_level_text_dispatches_before_late_media() -> None:
    """A room-level text root dispatches immediately; late media becomes its own turn."""
    batches: list[PreparedTurn] = []

    async def dispatch_batch(batch: PreparedTurn) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_turn=dispatch_batch,
        debounce_seconds=lambda: 1.0,
        is_shutting_down=lambda: False,
    )
    key = CoalescingKey("!room:localhost", None, RequesterCoalescingOwner("@user:localhost"))

    await _admit_ready(gate, key, _pending(_text_event("$text:localhost", "describe this", 1_000_000)))
    await _wait_for(lambda: [list(batch.handled_turn.source_event_ids) for batch in batches] == [["$text:localhost"]])

    await _admit_ready(gate, key, _pending(_image_event("$image:localhost", 1_000_600)))
    await gate.drain_all()

    assert [list(batch.handled_turn.source_event_ids) for batch in batches] == [
        ["$text:localhost"],
        ["$image:localhost"],
    ]


@pytest.mark.asyncio
async def test_text_dispatch_waits_for_same_window_unready_media_lane_slot() -> None:
    """An immediate text flush must not run before an in-window unready media slot delivers."""
    batches: list[PreparedTurn] = []

    async def dispatch_batch(batch: PreparedTurn) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_turn=dispatch_batch,
        debounce_seconds=lambda: 1.0,
        is_shutting_down=lambda: False,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", RequesterCoalescingOwner("@user:localhost"))

    await _admit_ready(gate, key, _pending(_text_event("$text:localhost", "describe this", 1_000_000)))
    slot = gate.enter_lane(ReceiptLaneKey(room_id=key.room_id, sender_id=key.owner.requester_user_id))
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

    assert [list(batch.handled_turn.source_event_ids) for batch in batches] == [["$text:localhost", "$image:localhost"]]


@pytest.mark.asyncio
async def test_voice_transcript_dispatches_without_debounce_wait() -> None:
    """Voice transcripts are complete utterances and skip the media debounce wait."""
    batches: list[PreparedTurn] = []

    async def dispatch_batch(batch: PreparedTurn) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_turn=dispatch_batch,
        debounce_seconds=lambda: 60.0,
        is_shutting_down=lambda: False,
    )
    key = CoalescingKey("!room:localhost", None, RequesterCoalescingOwner("@user:localhost"))

    await _admit_ready(
        gate,
        key,
        make_pending_event(
            _text_event("$voice:localhost", "voice transcript", 1_000_000),
            nio.MatrixRoom("!room:localhost", "@mindroom:localhost"),
            source_kind=VOICE_SOURCE_KIND,
        ),
    )

    await _wait_for(lambda: len(batches) == 1, deadline_seconds=0.1)

    assert [list(batch.handled_turn.source_event_ids) for batch in batches] == [["$voice:localhost"]]


@pytest.mark.asyncio
async def test_thread_messages_inside_debounce_window_still_coalesce() -> None:
    """Thread-scoped follow-ups close in time should remain one coalesced turn."""
    batches: list[PreparedTurn] = []

    async def dispatch_batch(batch: PreparedTurn) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_turn=dispatch_batch,
        debounce_seconds=lambda: 1.0,
        is_shutting_down=lambda: False,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", RequesterCoalescingOwner("@user:localhost"))

    await _admit_ready(gate, key, _pending(_text_event("$first:localhost", "first", 1_000_000)))
    await _admit_ready(gate, key, _pending(_text_event("$second:localhost", "second", 1_000_600)))

    await gate.drain_all()

    assert [list(batch.handled_turn.source_event_ids) for batch in batches] == [
        ["$first:localhost", "$second:localhost"],
    ]
    assert "quick succession" in batches[0].event.body


@pytest.mark.asyncio
async def test_threaded_media_debounce_uses_trailing_quiet_time() -> None:
    """A later media upload inside the debounce window should extend the quiet deadline."""
    batches: list[PreparedTurn] = []

    async def dispatch_batch(batch: PreparedTurn) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_turn=dispatch_batch,
        debounce_seconds=lambda: 0.05,
        is_shutting_down=lambda: False,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", RequesterCoalescingOwner("@user:localhost"))

    await _admit_ready(gate, key, _image_pending("$first:localhost", 1_000_000))
    await asyncio.sleep(0.01)
    await _admit_ready(gate, key, _image_pending("$second:localhost", 1_000_040))
    await asyncio.sleep(0.02)

    assert batches == []

    await _wait_for(
        lambda: (
            [list(batch.handled_turn.source_event_ids) for batch in batches]
            == [["$first:localhost", "$second:localhost"]]
        ),
    )


@pytest.mark.asyncio
async def test_lone_text_dispatches_without_debounce_wait() -> None:
    """A lone text message is a complete utterance and never waits for the debounce window."""
    batches: list[PreparedTurn] = []

    async def dispatch_batch(batch: PreparedTurn) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_turn=dispatch_batch,
        debounce_seconds=lambda: 60.0,
        is_shutting_down=lambda: False,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", RequesterCoalescingOwner("@user:localhost"))

    await _admit_ready(gate, key, _pending(_text_event("$text:localhost", "instant", 1_000_000)))

    await _wait_for(lambda: [list(batch.handled_turn.source_event_ids) for batch in batches] == [["$text:localhost"]])
    await gate.drain_all()


@pytest.mark.asyncio
async def test_trailing_caption_closes_media_batch_immediately() -> None:
    """A trailing text caption completes a media batch and flushes before the window expires."""
    batches: list[PreparedTurn] = []

    async def dispatch_batch(batch: PreparedTurn) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_turn=dispatch_batch,
        debounce_seconds=lambda: 60.0,
        is_shutting_down=lambda: False,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", RequesterCoalescingOwner("@user:localhost"))

    await _admit_ready(gate, key, _image_pending("$one:localhost", 1_000_000))
    await _admit_ready(gate, key, _image_pending("$two:localhost", 1_000_100))
    await asyncio.sleep(0.05)

    assert batches == []

    await _admit_ready(gate, key, _pending(_text_event("$caption:localhost", "caption", 1_000_200)))

    await _wait_for(
        lambda: (
            [list(batch.handled_turn.source_event_ids) for batch in batches]
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

    async def dispatch_batch(batch: PreparedTurn) -> None:
        calls.append((list(batch.handled_turn.source_event_ids), batch.event.body))

    async def wait_until_dispatch_allowed(wait_key: CoalescingKey) -> None:
        if wait_key == key:
            await idle.wait()

    gate = CoalescingGate(
        dispatch_turn=dispatch_batch,
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
            make_pending_event(
                _text_event(event_id, body, 1_000_000),
                nio.MatrixRoom("!room:localhost", "@mindroom:localhost"),
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

    async def dispatch_batch(batch: PreparedTurn) -> None:
        calls.append(list(batch.handled_turn.source_event_ids))

    async def wait_until_dispatch_allowed(wait_key: CoalescingKey) -> None:
        if wait_key == key:
            await idle.wait()

    gate = CoalescingGate(
        dispatch_turn=dispatch_batch,
        debounce_seconds=lambda: 60.0,
        is_shutting_down=lambda: False,
        wait_until_dispatch_allowed=wait_until_dispatch_allowed,
    )

    await _admit_ready(
        gate,
        key,
        make_pending_event(
            _image_event("$img:localhost", 1_000_000),
            nio.MatrixRoom("!room:localhost", "@mindroom:localhost"),
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
    normal_key = CoalescingKey("!room:localhost", "$other-thread:localhost", RequesterCoalescingOwner("@bob:localhost"))

    async def dispatch_batch(batch: PreparedTurn) -> None:
        batches.append(list(batch.handled_turn.source_event_ids))

    async def wait_until_dispatch_allowed(wait_key: CoalescingKey) -> None:
        if wait_key == active_key:
            active_wait_started.set()
            await release_active_wait.wait()

    gate = CoalescingGate(
        dispatch_turn=dispatch_batch,
        debounce_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
        wait_until_dispatch_allowed=wait_until_dispatch_allowed,
    )

    await _admit_ready(
        gate,
        active_key,
        make_pending_event(
            _text_event("$active:localhost", "queued while active", 1_000_000),
            nio.MatrixRoom("!room:localhost", "@mindroom:localhost"),
            source_kind=MESSAGE_SOURCE_KIND,
            requester_user_id="@alice:localhost",
            dispatch_policy_source_kind=ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND,
        ),
    )
    await active_wait_started.wait()

    slot = gate.enter_lane(
        ReceiptLaneKey(room_id=normal_key.room_id, sender_id=normal_key.owner.requester_user_id),
    )
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
    batches: list[PreparedTurn] = []
    release_first = asyncio.Event()

    async def dispatch_batch(batch: PreparedTurn) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_turn=dispatch_batch,
        debounce_seconds=lambda: 0.02,
        is_shutting_down=lambda: False,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", RequesterCoalescingOwner("@user:localhost"))
    first_pending = _pending(_text_event("$first:localhost", "first", 1_000_000))
    first_slot = gate.enter_lane(ReceiptLaneKey(room_id=key.room_id, sender_id=key.owner.requester_user_id))
    gate.submit_lane_slot(
        first_slot,
        key=key,
        source_event_id="$first:localhost",
        source_kind=MESSAGE_SOURCE_KIND,
        ready_task=asyncio.create_task(_ready_after(release_first, first_pending)),
    )

    await asyncio.sleep(0.05)
    second_slot = gate.enter_lane(ReceiptLaneKey(room_id=key.room_id, sender_id=key.owner.requester_user_id))
    gate.submit_lane_slot(
        second_slot,
        key=key,
        source_event_id="$second:localhost",
        source_kind=MESSAGE_SOURCE_KIND,
        ready_result=ReadyPendingEvent(pending_event=_pending(_text_event("$second:localhost", "second", 1_000_001))),
    )
    await asyncio.sleep(0.05)
    third_slot = gate.enter_lane(ReceiptLaneKey(room_id=key.room_id, sender_id=key.owner.requester_user_id))
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

    assert [list(batch.handled_turn.source_event_ids) for batch in batches] == [
        ["$first:localhost", "$second:localhost", "$third:localhost"],
    ]


@pytest.mark.asyncio
async def test_voice_readiness_delay_combines_backlog_in_receipt_order() -> None:
    """A slow STT result holds later text in the lane window, then both flush as one turn."""
    batches: list[PreparedTurn] = []

    async def dispatch_batch(batch: PreparedTurn) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_turn=dispatch_batch,
        debounce_seconds=lambda: 0.03,
        is_shutting_down=lambda: False,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", RequesterCoalescingOwner("@user:localhost"))
    voice_ready = asyncio.Event()

    voice_pending = _voice_pending("$voice:localhost", "voice transcript", 1_000_000)
    voice_slot = gate.enter_lane(ReceiptLaneKey(room_id=key.room_id, sender_id=key.owner.requester_user_id))
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
        lambda: (
            [list(batch.handled_turn.source_event_ids) for batch in batches]
            == [["$voice:localhost", "$typed:localhost"]]
        ),
    )


@pytest.mark.asyncio
async def test_failed_lane_ready_task_does_not_block_later_lane_work() -> None:
    """A raising ready task settles its slot so later same-lane work still dispatches."""
    batches: list[PreparedTurn] = []
    fail_voice = asyncio.Event()

    async def dispatch_batch(batch: PreparedTurn) -> None:
        batches.append(batch)

    async def failed_voice() -> ReadyPendingEvent:
        await fail_voice.wait()
        msg = "voice failed"
        raise RuntimeError(msg)

    gate = CoalescingGate(
        dispatch_turn=dispatch_batch,
        debounce_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", RequesterCoalescingOwner("@user:localhost"))
    voice_slot = gate.enter_lane(ReceiptLaneKey(room_id=key.room_id, sender_id=key.owner.requester_user_id))
    gate.submit_lane_slot(
        voice_slot,
        key=key,
        source_event_id="$voice:localhost",
        source_kind=VOICE_SOURCE_KIND,
        ready_task=asyncio.create_task(failed_voice()),
    )
    later_slot = gate.enter_lane(ReceiptLaneKey(room_id=key.room_id, sender_id=key.owner.requester_user_id))
    gate.submit_lane_slot(
        later_slot,
        key=key,
        source_event_id="$later:localhost",
        source_kind=MESSAGE_SOURCE_KIND,
        ready_result=ReadyPendingEvent(pending_event=_pending(_text_event("$later:localhost", "later", 1_000_002))),
    )
    fail_voice.set()

    await _wait_for(lambda: [list(batch.handled_turn.source_event_ids) for batch in batches] == [["$later:localhost"]])
    assert voice_slot.settled.is_set()


@pytest.mark.asyncio
async def test_lane_admission_does_not_wait_for_its_own_unsettled_slot() -> None:
    """A lane-admitted event is already ready and must not wait for its own slot to settle."""
    batches: list[PreparedTurn] = []

    async def dispatch_batch(batch: PreparedTurn) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_turn=dispatch_batch,
        debounce_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", RequesterCoalescingOwner("@user:localhost"))
    slot = gate.enter_lane(ReceiptLaneKey(room_id=key.room_id, sender_id=key.owner.requester_user_id))
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
        await _wait_for(
            lambda: [list(batch.handled_turn.source_event_ids) for batch in batches] == [["$lane:localhost"]],
        )
        assert not slot.settled.is_set()
    finally:
        gate.release_lane_slot(slot)
        await gate.drain_all()


@pytest.mark.asyncio
async def test_bounded_shutdown_marks_internal_drain_failure_incomplete() -> None:
    """Unexpected drain failures during shutdown must make checkpointing unsafe."""
    gate = CoalescingGate(
        dispatch_turn=AsyncMock(),
        debounce_seconds=lambda: 60.0,
        is_shutting_down=lambda: True,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", RequesterCoalescingOwner("@user:localhost"))
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

    async def dispatch_batch(_batch: PreparedTurn) -> None:
        dispatch_started.set()
        try:
            await release_dispatch.wait()
        except asyncio.CancelledError as exc:
            cancelled_args.append(exc.args)
            raise

    gate = CoalescingGate(
        dispatch_turn=dispatch_batch,
        debounce_seconds=lambda: 0.0,
        is_shutting_down=lambda: True,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", RequesterCoalescingOwner("@user:localhost"))
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

    async def dispatch_batch(_batch: PreparedTurn) -> None:
        dispatch_started.set()
        try:
            await release_dispatch.wait()
        except asyncio.CancelledError as exc:
            cancelled_args.append(exc.args)
            raise

    gate = CoalescingGate(
        dispatch_turn=dispatch_batch,
        debounce_seconds=lambda: 0.0,
        is_shutting_down=lambda: True,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", RequesterCoalescingOwner("@user:localhost"))
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

    async def dispatch_batch(batch: PreparedTurn) -> None:
        calls.append(list(batch.handled_turn.source_event_ids))

    gate = CoalescingGate(
        dispatch_turn=dispatch_batch,
        debounce_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
        wait_until_dispatch_allowed=wait_until_dispatch_allowed,
    )
    await _admit_ready(
        gate,
        key,
        make_pending_event(
            _text_event("$text:localhost", "typed", 1_000_000),
            nio.MatrixRoom("!room:localhost", "@mindroom:localhost"),
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
        dispatch_turn=AsyncMock(),
        debounce_seconds=lambda: 60.0,
        is_shutting_down=lambda: True,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", RequesterCoalescingOwner("@user:localhost"))
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
    batches: list[PreparedTurn] = []

    async def dispatch_batch(batch: PreparedTurn) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_turn=dispatch_batch,
        debounce_seconds=lambda: 0.0,
        is_shutting_down=lambda: True,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", RequesterCoalescingOwner("@user:localhost"))
    slot = gate.enter_lane(ReceiptLaneKey(room_id=key.room_id, sender_id=key.owner.requester_user_id))

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

    assert [list(batch.handled_turn.source_event_ids) for batch in batches] == [["$voice:localhost"]]


@pytest.mark.asyncio
async def test_debounce_does_not_wait_for_later_lane_slot_outside_window() -> None:
    """A slot entered after the quiet window should not delay the already-ready prompt."""
    batches: list[PreparedTurn] = []

    async def dispatch_batch(batch: PreparedTurn) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_turn=dispatch_batch,
        debounce_seconds=lambda: 0.01,
        is_shutting_down=lambda: False,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", RequesterCoalescingOwner("@user:localhost"))

    await _admit_ready(gate, key, _pending(_text_event("$text:localhost", "typed first", 1_000_000)))
    await asyncio.sleep(0.03)
    slot = gate.enter_lane(ReceiptLaneKey(room_id=key.room_id, sender_id=key.owner.requester_user_id))
    await asyncio.sleep(0.01)

    assert [list(batch.handled_turn.source_event_ids) for batch in batches] == [["$text:localhost"]]

    gate.submit_lane_slot(
        slot,
        key=key,
        source_event_id="$voice:localhost",
        source_kind=VOICE_SOURCE_KIND,
        ready_result=ReadyPendingEvent(pending_event=_voice_pending("$voice:localhost", "voice later", 1_000_050)),
    )
    await gate.drain_all()

    assert [list(batch.handled_turn.source_event_ids) for batch in batches] == [
        ["$text:localhost"],
        ["$voice:localhost"],
    ]


@pytest.mark.asyncio
async def test_ready_text_waits_behind_unready_older_voice_lane_slot() -> None:
    """Ready text behind an unready voice slot must not deliver until the voice resolves."""
    batches: list[PreparedTurn] = []
    release_voice = asyncio.Event()

    async def dispatch_batch(batch: PreparedTurn) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_turn=dispatch_batch,
        debounce_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", RequesterCoalescingOwner("@user:localhost"))
    voice_pending = _voice_pending("$voice:localhost", "voice first", 1_000_000)
    voice_slot = gate.enter_lane(ReceiptLaneKey(room_id=key.room_id, sender_id=key.owner.requester_user_id))
    gate.submit_lane_slot(
        voice_slot,
        key=key,
        source_event_id="$voice:localhost",
        source_kind=VOICE_SOURCE_KIND,
        ready_task=asyncio.create_task(_ready_after(release_voice, voice_pending)),
    )
    text_slot = gate.enter_lane(ReceiptLaneKey(room_id=key.room_id, sender_id=key.owner.requester_user_id))
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

    dispatched_ids = [event_id for batch in batches for event_id in batch.handled_turn.source_event_ids]
    assert dispatched_ids == ["$voice:localhost", "$text:localhost"]


@pytest.mark.asyncio
async def test_different_canonical_threads_do_not_serialize_after_admission() -> None:
    """Same-owner canonical thread gates should dispatch independently after admission."""
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    batches: list[list[str]] = []

    async def dispatch_batch(batch: PreparedTurn) -> None:
        batches.append(list(batch.handled_turn.source_event_ids))
        if batch.ingress.coalescing_key.thread_id == "$thread-a:localhost":
            first_started.set()
            await release_first.wait()

    gate = CoalescingGate(
        dispatch_turn=dispatch_batch,
        debounce_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    first_key = CoalescingKey("!room:localhost", "$thread-a:localhost", RequesterCoalescingOwner("@user:localhost"))
    second_key = CoalescingKey("!room:localhost", "$thread-b:localhost", RequesterCoalescingOwner("@user:localhost"))

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
    batches: list[PreparedTurn] = []
    release_none = asyncio.Event()

    async def dispatch_batch(batch: PreparedTurn) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_turn=dispatch_batch,
        debounce_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", RequesterCoalescingOwner("@user:localhost"))
    none_slot = gate.enter_lane(ReceiptLaneKey(room_id=key.room_id, sender_id=key.owner.requester_user_id))
    gate.submit_lane_slot(
        none_slot,
        key=key,
        source_event_id="$none:localhost",
        source_kind=MESSAGE_SOURCE_KIND,
        ready_task=asyncio.create_task(_none_after(release_none)),
    )
    later_slot = gate.enter_lane(ReceiptLaneKey(room_id=key.room_id, sender_id=key.owner.requester_user_id))
    gate.submit_lane_slot(
        later_slot,
        key=key,
        source_event_id="$later:localhost",
        source_kind=MESSAGE_SOURCE_KIND,
        ready_result=ReadyPendingEvent(pending_event=_pending(_text_event("$later:localhost", "later", 1_000_001))),
    )

    release_none.set()
    await gate.drain_all()

    assert [list(batch.handled_turn.source_event_ids) for batch in batches] == [["$later:localhost"]]
    assert none_slot.settled.is_set()
    assert _coalescing_gate_is_idle(gate)


@pytest.mark.asyncio
async def test_recovery_context_survives_existing_lane_and_gate_workers() -> None:
    """Durable recovery intent must reach dispatch workers created outside its context."""
    observed_recovery: list[bool] = []

    async def dispatch_batch(_batch: PreparedTurn) -> None:
        observed_recovery.append(turn_dispatch_recovery_active())

    gate = CoalescingGate(
        dispatch_turn=dispatch_batch,
        debounce_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", RequesterCoalescingOwner("@user:localhost"))
    slot = gate.enter_lane(ReceiptLaneKey(room_id=key.room_id, sender_id=key.owner.requester_user_id))
    await asyncio.sleep(0)

    with turn_dispatch_recovery_scope(active=True):
        pending = _pending(_text_event("$recovered:localhost", "retry", 1_000_000))
        pending.event = replace(pending.event, turn_dispatch_recovery=turn_dispatch_recovery_active())
        gate.submit_lane_slot(
            slot,
            key=key,
            source_event_id=pending.event.event_id,
            source_kind=MESSAGE_SOURCE_KIND,
            ready_result=ReadyPendingEvent(pending_event=pending),
        )

    await gate.drain_all()

    assert observed_recovery == [True]


@pytest.mark.asyncio
async def test_partial_ready_failure_dispatches_ready_events_and_clears_claim() -> None:
    """One failing member of a same-window burst is skipped while survivors dispatch."""
    batches: list[PreparedTurn] = []
    release_none = asyncio.Event()

    async def dispatch_batch(batch: PreparedTurn) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_turn=dispatch_batch,
        debounce_seconds=lambda: 0.02,
        is_shutting_down=lambda: False,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", RequesterCoalescingOwner("@user:localhost"))
    ready_slot = gate.enter_lane(ReceiptLaneKey(room_id=key.room_id, sender_id=key.owner.requester_user_id))
    gate.submit_lane_slot(
        ready_slot,
        key=key,
        source_event_id="$ready:localhost",
        source_kind=MESSAGE_SOURCE_KIND,
        ready_result=ReadyPendingEvent(pending_event=_pending(_text_event("$ready:localhost", "ready", 1_000_000))),
    )
    none_slot = gate.enter_lane(ReceiptLaneKey(room_id=key.room_id, sender_id=key.owner.requester_user_id))
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

    assert [list(batch.handled_turn.source_event_ids) for batch in batches] == [["$ready:localhost"]]
    assert _coalescing_gate_is_idle(gate)


@pytest.mark.asyncio
async def test_same_window_lane_slot_resolving_to_different_thread_waits_then_splits() -> None:
    """An in-window unready same-sender slot holds debounce, then dispatches under its resolved key."""
    batches: list[tuple[CoalescingKey, list[str]]] = []

    async def dispatch_batch(batch: PreparedTurn) -> None:
        batches.append((batch.ingress.coalescing_key, list(batch.handled_turn.source_event_ids)))

    gate = CoalescingGate(
        dispatch_turn=dispatch_batch,
        debounce_seconds=lambda: 0.03,
        is_shutting_down=lambda: False,
    )
    first_key = CoalescingKey("!room:localhost", "$thread-a:localhost", RequesterCoalescingOwner("@user:localhost"))
    second_key = CoalescingKey("!room:localhost", "$thread-b:localhost", RequesterCoalescingOwner("@user:localhost"))

    await _admit_ready(gate, first_key, _image_pending("$first:localhost", 1_000_000))
    await asyncio.sleep(0.005)
    slot = gate.enter_lane(
        ReceiptLaneKey(room_id=first_key.room_id, sender_id=first_key.owner.requester_user_id),
    )
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
    batches: list[PreparedTurn] = []
    release_first = asyncio.Event()

    async def dispatch_batch(batch: PreparedTurn) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_turn=dispatch_batch,
        debounce_seconds=lambda: 0.5,
        is_shutting_down=lambda: False,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", RequesterCoalescingOwner("@user:localhost"))
    first_pending = _pending(_text_event("$first:localhost", "first", 1_000_000))
    first_slot = gate.enter_lane(
        ReceiptLaneKey(room_id=key.room_id, sender_id=key.owner.requester_user_id),
        receipt_time=1.0,
    )
    second_slot = gate.enter_lane(
        ReceiptLaneKey(room_id=key.room_id, sender_id=key.owner.requester_user_id),
        receipt_time=1.2,
    )

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

    assert [list(batch.handled_turn.source_event_ids) for batch in batches] == [
        ["$first:localhost", "$second:localhost"],
    ]


@pytest.mark.asyncio
async def test_messages_in_different_rooms_do_not_coalesce() -> None:
    """Same-user messages in different rooms stay independent batches."""
    batches: list[PreparedTurn] = []

    async def dispatch_batch(batch: PreparedTurn) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_turn=dispatch_batch,
        debounce_seconds=lambda: 1.0,
        is_shutting_down=lambda: False,
    )
    first_key = CoalescingKey("!room-a:localhost", "$thread:localhost", RequesterCoalescingOwner("@user:localhost"))
    second_key = CoalescingKey("!room-b:localhost", "$thread:localhost", RequesterCoalescingOwner("@user:localhost"))

    await _admit_ready(gate, first_key, _pending(_text_event("$a:localhost", "room a", 1_000_000)))
    await _admit_ready(gate, second_key, _pending(_text_event("$b:localhost", "room b", 1_000_100)))

    await gate.drain_all()

    assert sorted(list(batch.handled_turn.source_event_ids) for batch in batches) == [
        ["$a:localhost"],
        ["$b:localhost"],
    ]


@pytest.mark.asyncio
async def test_messages_in_different_threads_do_not_coalesce() -> None:
    """Same-room messages in different threads stay independent batches."""
    batches: list[PreparedTurn] = []

    async def dispatch_batch(batch: PreparedTurn) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_turn=dispatch_batch,
        debounce_seconds=lambda: 1.0,
        is_shutting_down=lambda: False,
    )
    first_key = CoalescingKey("!room:localhost", "$thread-a:localhost", RequesterCoalescingOwner("@user:localhost"))
    second_key = CoalescingKey("!room:localhost", "$thread-b:localhost", RequesterCoalescingOwner("@user:localhost"))

    await _admit_ready(gate, first_key, _pending(_text_event("$a:localhost", "thread a", 1_000_000)))
    await _admit_ready(gate, second_key, _pending(_text_event("$b:localhost", "thread b", 1_000_100)))

    await gate.drain_all()

    assert sorted(list(batch.handled_turn.source_event_ids) for batch in batches) == [
        ["$a:localhost"],
        ["$b:localhost"],
    ]


@pytest.mark.asyncio
async def test_drain_all_flushes_pending_debounced_work_and_idles_gate() -> None:
    """Shutdown drain dispatches queued work without waiting out the debounce window."""
    batches: list[PreparedTurn] = []

    async def dispatch_batch(batch: PreparedTurn) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_turn=dispatch_batch,
        debounce_seconds=lambda: 60.0,
        is_shutting_down=lambda: False,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", RequesterCoalescingOwner("@user:localhost"))
    await _admit_ready(gate, key, _pending(_text_event("$pending:localhost", "pending", 1_000_000)))
    assert batches == []

    result = await gate.drain_all()

    assert result.completed is True
    assert [list(batch.handled_turn.source_event_ids) for batch in batches] == [["$pending:localhost"]]
    assert _coalescing_gate_is_idle(gate)
