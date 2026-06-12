"""Tests for per-(room, sender) ingress lanes and conversation independence."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import nio
import pytest

from mindroom.coalescing import CoalescingGate, IngressAdmissionClosedError, ReadyPendingEvent
from mindroom.coalescing_batch import CoalescingKey, PendingEvent, active_follow_up_coalescing_key
from mindroom.dispatch_source import VOICE_SOURCE_KIND
from mindroom.matrix.thread_membership import ThreadMembershipLookupError
from tests.test_live_message_coalescing import _make_bot, _make_room, _text_event, _wait_for

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.coalescing_batch import CoalescedBatch


def _room(room_id: str = "!room:localhost") -> nio.MatrixRoom:
    return nio.MatrixRoom(room_id, "@mindroom:localhost")


def _plain_event(
    event_id: str,
    body: str,
    origin_server_ts: int,
    *,
    room_id: str = "!room:localhost",
) -> nio.RoomMessageText:
    return nio.RoomMessageText.from_dict(
        {
            "content": {"body": body, "msgtype": "m.text"},
            "event_id": event_id,
            "sender": "@user:localhost",
            "origin_server_ts": origin_server_ts,
            "room_id": room_id,
            "type": "m.room.message",
        },
    )


def _ready(
    event: nio.RoomMessageText,
    *,
    source_kind: str = "message",
    room_id: str = "!room:localhost",
) -> ReadyPendingEvent:
    return ReadyPendingEvent(
        pending_event=PendingEvent(event=event, room=_room(room_id), source_kind=source_kind),
    )


def _gate(
    dispatch_batch: AsyncMock | None = None,
    *,
    debounce_seconds: float = 0.02,
    room_scope_is_single_conversation: bool | None = None,
    dispatch_allowed_now: bool | None = None,
) -> tuple[CoalescingGate, list[CoalescedBatch]]:
    batches: list[CoalescedBatch] = []

    async def record(batch: CoalescedBatch) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch or record,
        debounce_seconds=lambda: debounce_seconds,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
        room_scope_is_single_conversation=(
            None if room_scope_is_single_conversation is None else lambda _room_id: room_scope_is_single_conversation
        ),
        dispatch_allowed_now=(None if dispatch_allowed_now is None else lambda _key: dispatch_allowed_now),
    )
    return gate, batches


@pytest.mark.asyncio
async def test_unready_lane_slot_does_not_delay_other_senders_or_rooms() -> None:
    """One sender's unresolved ingress must never hold another sender or room."""
    gate, batches = _gate()
    blocked_voice = asyncio.Event()

    async def never_ready() -> ReadyPendingEvent:
        await blocked_voice.wait()
        return _ready(_plain_event("$voice", "voice", 1_000_000), source_kind=VOICE_SOURCE_KIND)

    voice_slot = gate.enter_lane(room_id="!room:localhost", sender_id="@alice:localhost")
    gate.submit_lane_slot(
        voice_slot,
        key=CoalescingKey("!room:localhost", "$thread", "@alice:localhost"),
        source_event_id="$voice",
        source_kind=VOICE_SOURCE_KIND,
        ready_task=asyncio.create_task(never_ready()),
    )

    bob_slot = gate.enter_lane(room_id="!room:localhost", sender_id="@bob:localhost")
    gate.submit_lane_slot(
        bob_slot,
        key=CoalescingKey("!room:localhost", "$thread", "@bob:localhost"),
        source_event_id="$bob",
        source_kind="message",
        ready_result=_ready(_plain_event("$bob", "from bob", 1_000_100)),
    )
    other_room_slot = gate.enter_lane(room_id="!other:localhost", sender_id="@alice:localhost")
    gate.submit_lane_slot(
        other_room_slot,
        key=CoalescingKey("!other:localhost", "$elsewhere", "@alice:localhost"),
        source_event_id="$elsewhere",
        source_kind="message",
        ready_result=_ready(
            _plain_event("$elsewhere", "other room", 1_000_200, room_id="!other:localhost"),
            room_id="!other:localhost",
        ),
    )

    await _wait_for(
        lambda: sorted(batch.source_event_ids[0] for batch in batches) == ["$bob", "$elsewhere"],
    )

    blocked_voice.set()
    await gate.drain_all()
    assert ["$voice"] in [batch.source_event_ids for batch in batches]


@pytest.mark.asyncio
async def test_thread_batch_dispatches_while_root_dispatch_is_in_flight() -> None:
    """A thread conversation must not wait for an in-flight room-root dispatch."""
    entered_root = asyncio.Event()
    release_root = asyncio.Event()
    batches: list[CoalescedBatch] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)
        if batch.source_event_ids == ["$root"]:
            entered_root.set()
            await release_root.wait()

    gate, _ = _gate(AsyncMock(side_effect=dispatch_batch), debounce_seconds=0.0)
    root_key = CoalescingKey("!room:localhost", None, "@user:localhost")
    thread_key = CoalescingKey("!room:localhost", "$root", "@user:localhost")

    await gate.admit(
        root_key,
        ready_result=_ready(_plain_event("$root", "root", 1_000_000)),
        source_event_id="$root",
        source_kind="message",
    )
    await entered_root.wait()
    await gate.admit(
        thread_key,
        ready_result=_ready(_plain_event("$reply", "reply", 1_000_100)),
        source_event_id="$reply",
        source_kind="message",
    )

    await _wait_for(lambda: ["$reply"] in [batch.source_event_ids for batch in batches])

    release_root.set()
    await gate.drain_all()


@pytest.mark.asyncio
async def test_room_mode_text_burst_coalesces_into_one_turn() -> None:
    """A room-mode agent treats rapid room-level texts as one conversation burst."""
    gate, batches = _gate(debounce_seconds=0.05, room_scope_is_single_conversation=True)
    key = CoalescingKey("!room:localhost", None, "@user:localhost")

    await gate.admit(
        key,
        ready_result=_ready(_plain_event("$m1", "first", 1_000_000)),
        source_event_id="$m1",
        source_kind="message",
    )
    await gate.admit(
        key,
        ready_result=_ready(_plain_event("$m2", "second", 1_000_100)),
        source_event_id="$m2",
        source_kind="message",
    )

    await _wait_for(lambda: [batch.source_event_ids for batch in batches] == [["$m1", "$m2"]])
    assert "quick succession" in batches[0].prompt


@pytest.mark.asyncio
async def test_straggler_active_follow_up_logs_missed_combined_turn() -> None:
    """A follow-up arriving after its response went idle is logged as a missed merge."""
    gate, batches = _gate(debounce_seconds=0.0, dispatch_allowed_now=True)
    key = active_follow_up_coalescing_key("!room:localhost", "$thread")

    with patch("mindroom.coalescing.logger") as logger_mock:
        await gate.admit(
            key,
            ready_result=_ready(_plain_event("$late", "late follow-up", 1_000_000)),
            source_event_id="$late",
            source_kind="message",
        )

    logger_mock.info.assert_any_call(
        "follow_up_missed_combined_turn",
        room_id="!room:localhost",
        thread_id="$thread",
        source_event_id="$late",
    )
    await gate.drain_all()
    assert [batch.source_event_ids for batch in batches] == [["$late"]]


@pytest.mark.asyncio
async def test_active_follow_up_with_running_response_does_not_log_missed_turn() -> None:
    """Follow-ups queued behind a still-active response are not logged as missed."""
    gate, batches = _gate(debounce_seconds=0.0, dispatch_allowed_now=False)
    key = active_follow_up_coalescing_key("!room:localhost", "$thread")

    with patch("mindroom.coalescing.logger") as logger_mock:
        await gate.admit(
            key,
            ready_result=_ready(_plain_event("$queued", "queued follow-up", 1_000_000)),
            source_event_id="$queued",
            source_kind="message",
        )

    assert not any(call.args[:1] == ("follow_up_missed_combined_turn",) for call in logger_mock.info.call_args_list)
    await gate.drain_all()
    assert [batch.source_event_ids for batch in batches] == [["$queued"]]


@pytest.mark.asyncio
async def test_enter_lane_during_bounded_drain_returns_closed_slot() -> None:
    """Ingress arriving during a bounded drain is refused without recreating work."""
    entered_dispatch = asyncio.Event()
    release_dispatch = asyncio.Event()

    async def blocking_dispatch(_batch: CoalescedBatch) -> None:
        entered_dispatch.set()
        await release_dispatch.wait()

    gate, _ = _gate(AsyncMock(side_effect=blocking_dispatch), debounce_seconds=0.0)
    key = CoalescingKey("!room:localhost", "$thread", "@user:localhost")
    await gate.admit(
        key,
        ready_result=_ready(_plain_event("$m1", "first", 1_000_000)),
        source_event_id="$m1",
        source_kind="message",
    )
    await entered_dispatch.wait()

    drain_task = asyncio.create_task(gate.drain_all(ready_timeout_seconds=0.05))
    await asyncio.sleep(0)
    slot = gate.enter_lane(room_id="!room:localhost", sender_id="@user:localhost")
    assert slot.closed
    with pytest.raises(IngressAdmissionClosedError):
        gate.submit_lane_slot(
            slot,
            key=key,
            source_event_id="$m2",
            source_kind="message",
            ready_result=_ready(_plain_event("$m2", "second", 1_000_100)),
        )

    release_dispatch.set()
    result = await drain_task
    assert result.completed is False
    assert result.released_reservation_count >= 1


@pytest.mark.asyncio
async def test_router_command_targeting_unresolved_conversation_fails_visibly(tmp_path: Path) -> None:
    """A command whose conversation cannot resolve yet gets a loud visible no-op."""
    bot = _make_bot(tmp_path, agent_name="router")
    room = _make_room()
    command_event = _text_event(event_id="$cmd", body="!help", server_timestamp=1000, thread_id="$pending_root")
    send_text_mock = AsyncMock(return_value="$notice")

    with (
        patch.object(
            bot._conversation_resolver,
            "coalescing_thread_id",
            new=AsyncMock(side_effect=ThreadMembershipLookupError("unproven root")),
        ),
        patch.object(bot._delivery_gateway, "send_text", new=send_text_mock),
        patch.object(bot._turn_controller, "_dispatch_text_message", new=AsyncMock()) as dispatch_mock,
    ):
        await bot._turn_controller.handle_text_event(room, command_event)

    send_text_mock.assert_awaited_once()
    request = send_text_mock.await_args.args[0]
    assert request.target.room_id == room.room_id
    assert "command" in request.response_text.lower()
    dispatch_mock.assert_not_awaited()
    assert bot._turn_store.is_handled("$cmd")


@pytest.mark.asyncio
async def test_non_router_agent_marks_unresolvable_command_handled_without_notice(tmp_path: Path) -> None:
    """Non-router agents drop unresolvable commands quietly but never guess a target."""
    bot = _make_bot(tmp_path)
    room = _make_room()
    command_event = _text_event(event_id="$cmd", body="!help", server_timestamp=1000, thread_id="$pending_root")
    send_text_mock = AsyncMock(return_value="$notice")

    with (
        patch.object(
            bot._conversation_resolver,
            "coalescing_thread_id",
            new=AsyncMock(side_effect=ThreadMembershipLookupError("unproven root")),
        ),
        patch.object(bot._delivery_gateway, "send_text", new=send_text_mock),
        patch.object(bot._turn_controller, "_dispatch_text_message", new=AsyncMock()) as dispatch_mock,
    ):
        await bot._turn_controller.handle_text_event(room, command_event)

    send_text_mock.assert_not_awaited()
    dispatch_mock.assert_not_awaited()
    assert bot._turn_store.is_handled("$cmd")


@pytest.mark.asyncio
async def test_unresolvable_non_command_text_still_rejects_ingress(tmp_path: Path) -> None:
    """Conversation-resolution failures for normal text keep rejecting ingress loudly."""
    bot = _make_bot(tmp_path)
    room = _make_room()
    event = _text_event(event_id="$m1", body="hello there", server_timestamp=1000, thread_id="$pending_root")

    with (
        patch.object(
            bot._conversation_resolver,
            "coalescing_thread_id",
            new=AsyncMock(side_effect=ThreadMembershipLookupError("unproven root")),
        ),
        patch.object(bot._turn_controller, "_dispatch_text_message", new=AsyncMock()) as dispatch_mock,
        pytest.raises(ThreadMembershipLookupError),
    ):
        await bot._turn_controller.handle_text_event(room, event)

    dispatch_mock.assert_not_awaited()
    assert not bot._turn_store.is_handled("$m1")


@pytest.mark.asyncio
async def test_failed_lane_readiness_does_not_block_later_same_sender_work() -> None:
    """A failed readiness task settles its slot so later same-lane work delivers."""
    gate, batches = _gate(debounce_seconds=0.0)
    key = CoalescingKey("!room:localhost", "$thread", "@user:localhost")

    async def failing_ready() -> ReadyPendingEvent:
        msg = "stt failed"
        raise RuntimeError(msg)

    failed_slot = gate.enter_lane(room_id="!room:localhost", sender_id="@user:localhost")
    gate.submit_lane_slot(
        failed_slot,
        key=key,
        source_event_id="$voice",
        source_kind=VOICE_SOURCE_KIND,
        ready_task=asyncio.create_task(failing_ready()),
    )
    text_slot = gate.enter_lane(room_id="!room:localhost", sender_id="@user:localhost")
    gate.submit_lane_slot(
        text_slot,
        key=key,
        source_event_id="$text",
        source_kind="message",
        ready_result=_ready(_plain_event("$text", "typed", 1_000_100)),
    )

    await _wait_for(lambda: [batch.source_event_ids for batch in batches] == [["$text"]])
    await gate.drain_all()
