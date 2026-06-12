"""Tests for per-(room, sender) ingress lanes and conversation independence."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import nio
import pytest

from mindroom.coalescing import CoalescingGate, IngressAdmissionClosedError, ReadyPendingEvent
from mindroom.coalescing_batch import CoalescingKey, PendingEvent
from mindroom.dispatch_source import ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND, VOICE_SOURCE_KIND
from mindroom.matrix.thread_membership import ThreadMembershipLookupError
from mindroom.message_target import MessageTarget
from tests.conftest import prepared_dispatch_result, unwrap_extracted_collaborator
from tests.test_live_message_coalescing import (
    _enqueue_for_dispatch,
    _make_bot,
    _make_room,
    _prepared_dispatch,
    _respond_dispatch_plan,
    _text_event,
    _wait_for,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
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
    dispatch_allowed_now: Callable[[CoalescingKey], bool] | bool | None = None,
    wait_until_dispatch_allowed: Callable[[CoalescingKey], Awaitable[None]] | None = None,
) -> tuple[CoalescingGate, list[CoalescedBatch]]:
    batches: list[CoalescedBatch] = []

    async def record(batch: CoalescedBatch) -> None:
        batches.append(batch)

    if isinstance(dispatch_allowed_now, bool):
        allowed = dispatch_allowed_now
        dispatch_allowed_now = lambda _key: allowed  # noqa: E731

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch or record,
        debounce_seconds=lambda: debounce_seconds,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
        wait_until_dispatch_allowed=wait_until_dispatch_allowed,
        room_scope_is_single_conversation=(
            None if room_scope_is_single_conversation is None else lambda _room_id: room_scope_is_single_conversation
        ),
        dispatch_allowed_now=dispatch_allowed_now,
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
async def test_straggler_follow_up_logs_missed_combined_turn() -> None:
    """A late-resolving follow-up that misses its busy window is logged as a missed merge."""
    busy = {"value": True}
    gate, batches = _gate(debounce_seconds=0.0, dispatch_allowed_now=lambda _key: not busy["value"])
    key = CoalescingKey("!room:localhost", "$thread", "@user:localhost")
    release_ready = asyncio.Event()

    async def slow_ready() -> ReadyPendingEvent:
        await release_ready.wait()
        return _ready(_plain_event("$late", "late follow-up", 1_000_000))

    slot = gate.enter_lane(room_id="!room:localhost", sender_id="@user:localhost")
    gate.submit_lane_slot(
        slot,
        key=key,
        source_event_id="$late",
        source_kind="message",
        ready_task=asyncio.create_task(slow_ready()),
    )

    busy["value"] = False
    with patch("mindroom.coalescing.logger") as logger_mock:
        release_ready.set()
        await _wait_for(lambda: slot.settled.is_set())

    logger_mock.info.assert_any_call(
        "follow_up_missed_combined_turn",
        room_id="!room:localhost",
        thread_id="$thread",
        source_event_id="$late",
    )
    await gate.drain_all()
    assert [batch.source_event_ids for batch in batches] == [["$late"]]
    assert batches[0].dispatch_policy_source_kind is None


@pytest.mark.asyncio
async def test_follow_up_delivered_while_still_busy_is_not_logged_as_missed() -> None:
    """A follow-up that lands inside the busy window joins the queue without a missed-turn log."""
    busy = {"value": True}
    idle = asyncio.Event()

    async def wait_until_idle(_key: CoalescingKey) -> None:
        await idle.wait()

    gate, batches = _gate(
        debounce_seconds=0.0,
        dispatch_allowed_now=lambda _key: not busy["value"],
        wait_until_dispatch_allowed=wait_until_idle,
    )
    key = CoalescingKey("!room:localhost", "$thread", "@user:localhost")

    slot = gate.enter_lane(room_id="!room:localhost", sender_id="@user:localhost")
    with patch("mindroom.coalescing.logger") as logger_mock:
        gate.submit_lane_slot(
            slot,
            key=key,
            source_event_id="$queued",
            source_kind="message",
            ready_result=_ready(_plain_event("$queued", "queued follow-up", 1_000_000)),
        )
        await _wait_for(lambda: slot.settled.is_set())

    assert not any(call.args[:1] == ("follow_up_missed_combined_turn",) for call in logger_mock.info.call_args_list)
    assert batches == []

    busy["value"] = False
    idle.set()
    await _wait_for(lambda: [batch.source_event_ids for batch in batches] == [["$queued"]])
    assert batches[0].dispatch_policy_source_kind == ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND
    await gate.drain_all()


@pytest.mark.asyncio
async def test_busy_conversation_queues_any_sender_into_one_combined_follow_up() -> None:
    """Admissions during a running response queue and flush as one combined follow-up."""
    busy = {"value": True}
    idle = asyncio.Event()

    async def wait_until_idle(_key: CoalescingKey) -> None:
        await idle.wait()

    gate, batches = _gate(
        debounce_seconds=0.0,
        dispatch_allowed_now=lambda _key: not busy["value"],
        wait_until_dispatch_allowed=wait_until_idle,
    )

    await gate.admit(
        CoalescingKey("!room:localhost", "$thread", "@alice:localhost"),
        ready_result=_ready(_plain_event("$a", "from alice", 1_000_000)),
        source_event_id="$a",
        source_kind="message",
    )
    await gate.admit(
        CoalescingKey("!room:localhost", "$thread", "@bob:localhost"),
        ready_result=_ready(_plain_event("$b", "from bob", 1_000_100)),
        source_event_id="$b",
        source_kind="message",
    )
    await asyncio.sleep(0.01)
    assert batches == []

    busy["value"] = False
    idle.set()
    await _wait_for(lambda: [batch.source_event_ids for batch in batches] == [["$a", "$b"]])
    assert batches[0].dispatch_policy_source_kind == ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND
    await gate.drain_all()


@pytest.mark.asyncio
async def test_machine_fire_queues_while_conversation_busy() -> None:
    """A scheduled fire arriving mid-response queues and becomes the follow-up turn."""
    busy = {"value": True}
    idle = asyncio.Event()

    async def wait_until_idle(_key: CoalescingKey) -> None:
        await idle.wait()

    gate, batches = _gate(
        debounce_seconds=0.0,
        dispatch_allowed_now=lambda _key: not busy["value"],
        wait_until_dispatch_allowed=wait_until_idle,
    )

    await gate.admit(
        CoalescingKey("!room:localhost", "$thread", "@scheduler:localhost"),
        ready_result=_ready(_plain_event("$fire2", "scheduled check-in", 1_000_000), source_kind="scheduled"),
        source_event_id="$fire2",
        source_kind="scheduled",
    )
    await asyncio.sleep(0.01)
    assert batches == []

    busy["value"] = False
    idle.set()
    await _wait_for(lambda: [batch.source_event_ids for batch in batches] == [["$fire2"]])
    assert batches[0].dispatch_policy_source_kind is None
    await gate.drain_all()


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


@pytest.mark.asyncio
async def test_long_running_turn_never_delays_other_ingress_from_same_sender(tmp_path: Path) -> None:
    """A multi-minute in-flight turn delays neither a new top-level turn nor another thread."""
    bot = _make_bot(tmp_path, debounce_ms=0)
    room = _make_room()
    first = _text_event(event_id="$m1", body="first")
    second = _text_event(event_id="$m2", body="second top-level")
    other_thread_reply = _text_event(event_id="$m3", body="reply elsewhere", thread_id="$other")
    first_locked = asyncio.Event()
    release_first_response = asyncio.Event()
    generated: list[str] = []

    async def fake_generate_response_locked(_self: object, request: object, **_kwargs: object) -> None:
        # The real lifecycle acquired the response lock before invoking this
        # locked operation; only post-lock generation is faked here.
        generated.append(request.response_envelope.source_event_id)
        if request.on_lifecycle_lock_acquired is not None:
            request.on_lifecycle_lock_acquired()
        if len(generated) == 1:
            first_locked.set()
            await release_first_response.wait()

    async def fake_prepare_dispatch(
        _room: object,
        event: object,
        requester_user_id: str,
        **_kwargs: object,
    ) -> object:
        thread_id = "$other" if event.event_id == "$m3" else f"{event.event_id}-thread"
        dispatch = _prepared_dispatch(
            event_id=event.event_id,
            requester_user_id=requester_user_id,
            body=event.body,
            thread_id=thread_id,
        )
        return prepared_dispatch_result(dispatch)

    with (
        patch.object(bot._turn_controller, "_prepare_dispatch", new=fake_prepare_dispatch),
        patch.object(bot._turn_policy, "plan_turn", new=AsyncMock(return_value=_respond_dispatch_plan())),
        patch.object(bot._turn_controller, "_has_newer_unresponded_in_thread", return_value=False),
        patch(
            "mindroom.response_runner.ResponseRunner.generate_response_locked",
            new=fake_generate_response_locked,
        ),
    ):
        await bot._turn_controller.handle_text_event(room, first)
        await asyncio.wait_for(first_locked.wait(), timeout=1.0)

        await bot._turn_controller.handle_text_event(room, second)
        await bot._turn_controller.handle_text_event(room, other_thread_reply)

        await _wait_for(lambda: sorted(generated) == ["$m1", "$m2", "$m3"], deadline_seconds=1.0)
        assert not release_first_response.is_set()

        release_first_response.set()
        await bot._coalescing_gate.drain_all()
        await bot._response_runner.drain_inbox_responses()


@pytest.mark.asyncio
async def test_lane_worker_failure_at_wait_phase_does_not_poison_lane() -> None:
    """A failure while waiting on the head slot settles it and keeps the lane serving."""
    gate, batches = _gate(debounce_seconds=0.0)
    poisoned_slot = gate.enter_lane(room_id="!room:localhost", sender_id="@user:localhost")

    async def failing_wait() -> None:
        msg = "wait phase failed"
        raise RuntimeError(msg)

    with patch.object(poisoned_slot.loaded, "wait", new=failing_wait):
        await asyncio.wait_for(poisoned_slot.settled.wait(), timeout=1.0)

    assert poisoned_slot.released
    with pytest.raises(IngressAdmissionClosedError):
        gate.submit_lane_slot(
            poisoned_slot,
            key=CoalescingKey("!room:localhost", "$thread", "@user:localhost"),
            source_event_id="$poisoned",
            source_kind="message",
            ready_result=_ready(_plain_event("$poisoned", "lost", 1_000_000)),
        )

    next_slot = gate.enter_lane(room_id="!room:localhost", sender_id="@user:localhost")
    gate.submit_lane_slot(
        next_slot,
        key=CoalescingKey("!room:localhost", "$thread", "@user:localhost"),
        source_event_id="$next",
        source_kind="message",
        ready_result=_ready(_plain_event("$next", "still works", 1_000_100)),
    )

    await _wait_for(lambda: [batch.source_event_ids for batch in batches] == [["$next"]])
    result = await gate.drain_all()
    assert result.completed is True


@pytest.mark.asyncio
async def test_response_failure_drains_follow_up_queue(tmp_path: Path) -> None:
    """Follow-ups queued behind a response that fails still dispatch as the follow-up turn."""
    bot = _make_bot(tmp_path, debounce_ms=0)
    room = _make_room()
    target = MessageTarget.resolve(room.room_id, "$thread", "$m0")
    runner = unwrap_extracted_collaborator(bot._response_runner)
    lifecycle = runner._lifecycle_coordinator
    lifecycle_lock = lifecycle._response_lifecycle_lock(target)
    queued_signal = lifecycle._get_or_create_queued_signal(target)
    calls: list[CoalescedBatch] = []

    async def record_dispatch(batch: CoalescedBatch) -> None:
        calls.append(batch)

    await lifecycle_lock.acquire()
    queued_signal.begin_response_turn()
    with patch.object(bot._turn_controller, "handle_coalesced_batch", new=AsyncMock(side_effect=record_dispatch)):
        for event_id, sender in (("$f1", "@alice:localhost"), ("$f2", "@bob:localhost")):
            await _enqueue_for_dispatch(
                bot,
                _text_event(event_id=event_id, body="follow-up", sender=sender, thread_id="$thread"),
                room,
                source_kind="message",
                requester_user_id=sender,
                coalescing_key=CoalescingKey(room.room_id, "$thread", sender),
            )
        await _wait_for(
            lambda: sum(len(entry.queue) for entry in bot._coalescing_gate._gates.values()) == 2,
        )
        assert calls == []

        # A failing response releases its lifecycle exactly like a clean one;
        # the queued follow-ups must survive the failure and flush together.
        queued_signal.finish_response_turn()
        lifecycle_lock.release()

        await _wait_for(lambda: [list(batch.source_event_ids) for batch in calls] == [["$f1", "$f2"]])
    assert calls[0].dispatch_policy_source_kind == ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND


@pytest.mark.asyncio
async def test_response_cancellation_drains_follow_up_queue(tmp_path: Path) -> None:
    """Follow-ups queued behind a cancelled detached response still dispatch together."""
    bot = _make_bot(tmp_path, debounce_ms=0)
    room = _make_room()
    target = MessageTarget.resolve(room.room_id, "$thread", "$m0")
    runner = unwrap_extracted_collaborator(bot._response_runner)
    lifecycle = runner._lifecycle_coordinator
    lifecycle_lock = lifecycle._response_lifecycle_lock(target)
    queued_signal = lifecycle._get_or_create_queued_signal(target)
    response_running = asyncio.Event()
    calls: list[CoalescedBatch] = []

    async def record_dispatch(batch: CoalescedBatch) -> None:
        calls.append(batch)

    async def blocked_response() -> None:
        await lifecycle_lock.acquire()
        queued_signal.begin_response_turn()
        response_running.set()
        try:
            await asyncio.Event().wait()
        finally:
            queued_signal.finish_response_turn()
            lifecycle_lock.release()

    response_task = runner.track_inbox_response(blocked_response(), name="test_blocked_response")
    await asyncio.wait_for(response_running.wait(), timeout=1.0)
    with patch.object(bot._turn_controller, "handle_coalesced_batch", new=AsyncMock(side_effect=record_dispatch)):
        for event_id, sender in (("$f1", "@alice:localhost"), ("$f2", "@bob:localhost")):
            await _enqueue_for_dispatch(
                bot,
                _text_event(event_id=event_id, body="follow-up", sender=sender, thread_id="$thread"),
                room,
                source_kind="message",
                requester_user_id=sender,
                coalescing_key=CoalescingKey(room.room_id, "$thread", sender),
            )
        await _wait_for(
            lambda: sum(len(entry.queue) for entry in bot._coalescing_gate._gates.values()) == 2,
        )
        assert calls == []

        response_task.cancel()
        assert await runner.drain_inbox_responses() is True

        await _wait_for(lambda: [list(batch.source_event_ids) for batch in calls] == [["$f1", "$f2"]])
    assert calls[0].dispatch_policy_source_kind == ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND


@pytest.mark.asyncio
async def test_machine_and_human_follow_ups_split_solo_batch() -> None:
    """A scheduled fire queued alongside a human follow-up dispatches as its own turn."""
    busy = {"value": True}
    idle = asyncio.Event()

    async def wait_until_idle(_key: CoalescingKey) -> None:
        await idle.wait()

    gate, batches = _gate(
        debounce_seconds=0.0,
        dispatch_allowed_now=lambda _key: not busy["value"],
        wait_until_dispatch_allowed=wait_until_idle,
    )

    await gate.admit(
        CoalescingKey("!room:localhost", "$thread", "@user:localhost"),
        ready_result=_ready(_plain_event("$human", "human follow-up", 1_000_000)),
        source_event_id="$human",
        source_kind="message",
    )
    await gate.admit(
        CoalescingKey("!room:localhost", "$thread", "@scheduler:localhost"),
        ready_result=_ready(_plain_event("$fire", "scheduled check-in", 1_000_100), source_kind="scheduled"),
        source_event_id="$fire",
        source_kind="scheduled",
    )
    await asyncio.sleep(0.01)
    assert batches == []

    busy["value"] = False
    idle.set()
    await _wait_for(lambda: [batch.source_event_ids for batch in batches] == [["$human"], ["$fire"]])
    assert batches[0].dispatch_policy_source_kind == ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND
    assert batches[1].dispatch_policy_source_kind is None
    await gate.drain_all()
