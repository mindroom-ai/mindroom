"""A reply that only waits on background work lets its conversation's other turns run, then takes the lock back."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from mindroom.dispatch_source import MESSAGE_SOURCE_KIND, SCHEDULED_SOURCE_KIND, SILENT_SCHEDULE_SOURCE_KIND
from mindroom.hooks import MessageEnvelope
from mindroom.message_target import MessageTarget
from mindroom.response_lifecycle import ResponseLifecycleCoordinator
from mindroom.tool_jobs.control import current_human_message_signal, released_while_waiting
from tests.conftest import message_origin

_TARGET = MessageTarget.resolve("!room:localhost", "$root", "$first")
_ROOM_ID, _THREAD_ID = _TARGET.lifecycle_key.room_id, _TARGET.lifecycle_key.thread_id


def _envelope(event_id: str, *, source_kind: str = MESSAGE_SOURCE_KIND) -> MessageEnvelope:
    return MessageEnvelope(
        source_event_id=event_id,
        target=_TARGET,
        body="hello",
        attachment_ids=(),
        mentioned_agents=(),
        agent_name="general",
        origin=message_origin(source_kind=source_kind),
    )


@dataclass
class _Reply:
    """A reply that may wait inside its model run, then waits on background work at its response boundary."""

    lifecycle: ResponseLifecycleCoordinator
    source: str = "$first"
    in_model_wait: bool = False
    started: asyncio.Event = field(default_factory=asyncio.Event)
    model_wait_released: asyncio.Event = field(default_factory=asyncio.Event)
    waiting: asyncio.Event = field(default_factory=asyncio.Event)
    work_done: asyncio.Event = field(default_factory=asyncio.Event)
    finished: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[str] | None = None

    async def _operation(self, _target: MessageTarget) -> str:
        self.started.set()
        if self.in_model_wait:
            signal = current_human_message_signal()
            assert signal is not None
            signal.subscribe(self.model_wait_released.set)
            try:
                await self.model_wait_released.wait()
            finally:
                signal.unsubscribe(self.model_wait_released.set)
        async with released_while_waiting():
            self.waiting.set()
            await self.work_done.wait()
        self.finished.set()
        return self.source

    def start(self) -> None:
        self.task = asyncio.create_task(
            self.lifecycle.run_locked_response(
                target=_TARGET,
                response_envelope=_envelope(self.source),
                pipeline_timing=None,
                locked_operation=self._operation,
            ),
        )


async def _run_turn(
    lifecycle: ResponseLifecycleCoordinator,
    source: str,
    *,
    source_kind: str = MESSAGE_SOURCE_KIND,
    signal_queued_message: bool = True,
) -> str:
    async def operation(_target: MessageTarget) -> str:
        return source

    return await lifecycle.run_locked_response(
        target=_TARGET,
        response_envelope=_envelope(source, source_kind=source_kind),
        pipeline_timing=None,
        locked_operation=operation,
        signal_queued_message=signal_queued_message,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source_kind", "signal_queued_message"),
    [(MESSAGE_SOURCE_KIND, True), (SCHEDULED_SOURCE_KIND, True), (SILENT_SCHEDULE_SOURCE_KIND, False)],
)
async def test_waiting_reply_lets_every_other_turn_run(source_kind: str, *, signal_queued_message: bool) -> None:
    """While a reply only waits on background work, it is not active and any turn of the conversation runs at once."""
    lifecycle = ResponseLifecycleCoordinator()
    reply = _Reply(lifecycle)
    reply.start()
    await asyncio.wait_for(reply.waiting.wait(), 5)
    assert not lifecycle.has_active_response_for_target(_TARGET)
    assert lifecycle.active_thread_ids_for_room(_ROOM_ID) == frozenset()
    await asyncio.wait_for(lifecycle.wait_for_thread_idle(_ROOM_ID, _THREAD_ID), 5)
    turn = _run_turn(lifecycle, "$other", source_kind=source_kind, signal_queued_message=signal_queued_message)
    assert await asyncio.wait_for(turn, 5) == "$other"
    assert not reply.finished.is_set()
    reply.work_done.set()
    assert reply.task is not None
    assert await reply.task == "$first"
    assert lifecycle.active_thread_ids_for_room(_ROOM_ID) == frozenset()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source_kind", "signal_queued_message"),
    [(MESSAGE_SOURCE_KIND, True), (SCHEDULED_SOURCE_KIND, True), (SILENT_SCHEDULE_SOURCE_KIND, False)],
)
async def test_queued_turn_ends_the_waits_inside_the_model_run(
    source_kind: str,
    *,
    signal_queued_message: bool,
) -> None:
    """Any turn queued behind a reply waiting inside its model run lets that reply reach its boundary and run.

    The turn that started consumed its notice, so the waits of its own model run stay attached.
    """
    lifecycle = ResponseLifecycleCoordinator()
    reply = _Reply(lifecycle, in_model_wait=True)
    reply.start()
    await asyncio.wait_for(reply.started.wait(), 5)
    assert lifecycle.has_active_response_for_target(_TARGET)
    own_waits_released = asyncio.Event()

    async def queued(_target: MessageTarget) -> str:
        signal = current_human_message_signal()
        assert signal is not None
        signal.subscribe(own_waits_released.set)
        signal.unsubscribe(own_waits_released.set)
        return "$queued"

    turn = asyncio.create_task(
        lifecycle.run_locked_response(
            target=_TARGET,
            response_envelope=_envelope("$queued", source_kind=source_kind),
            pipeline_timing=None,
            locked_operation=queued,
            signal_queued_message=signal_queued_message,
        ),
    )
    await asyncio.wait_for(reply.model_wait_released.wait(), 5)
    assert await asyncio.wait_for(turn, 5) == "$queued"
    assert not own_waits_released.is_set()
    reply.work_done.set()
    assert reply.task is not None
    assert await reply.task == "$first"
    assert not lifecycle._thread_queued_signals[_TARGET.lifecycle_key].has_pending_human_messages()


@pytest.mark.asyncio
async def test_human_message_ends_the_waits_inside_the_model_run_at_ingress() -> None:
    """A new human message ends model-run waits as it arrives, so the reply stops holding it back in the queue."""
    lifecycle = ResponseLifecycleCoordinator()
    reply = _Reply(lifecycle, in_model_wait=True)
    reply.start()
    await asyncio.wait_for(reply.started.wait(), 5)
    reservation = lifecycle.reserve_waiting_human_message(target=_TARGET, response_envelope=_envelope("$other"))
    assert reservation is not None
    await asyncio.wait_for(reply.model_wait_released.wait(), 5)
    await asyncio.wait_for(reply.waiting.wait(), 5)
    # Releasing the model-run wait never ends the reply: it keeps waiting at its boundary.
    assert not reply.finished.is_set()
    reservation.cancel()
    reply.work_done.set()
    assert reply.task is not None
    assert await reply.task == "$first"


@pytest.mark.asyncio
async def test_waiting_reply_takes_the_lock_back_behind_the_turn_that_holds_it() -> None:
    """A reply whose work became ready continues only once the turn running meanwhile lets the lock go."""
    lifecycle = ResponseLifecycleCoordinator()
    reply = _Reply(lifecycle)
    reply.start()
    await asyncio.wait_for(reply.waiting.wait(), 5)
    running, release = asyncio.Event(), asyncio.Event()

    async def other(_target: MessageTarget) -> str:
        running.set()
        await release.wait()
        return "$other"

    turn = asyncio.create_task(
        lifecycle.run_locked_response(
            target=_TARGET,
            response_envelope=_envelope("$other"),
            pipeline_timing=None,
            locked_operation=other,
        ),
    )
    await asyncio.wait_for(running.wait(), 5)
    reply.work_done.set()
    await asyncio.sleep(0.05)
    assert not reply.finished.is_set()
    # Taking the lock back makes the reply active again, so new messages queue behind it as usual.
    assert lifecycle.has_active_response_for_target(_TARGET)
    release.set()
    assert await turn == "$other"
    assert reply.task is not None
    assert await reply.task == "$first"


@pytest.mark.asyncio
async def test_cancelled_waiting_reply_settles_under_the_lock() -> None:
    """Stopping a waiting reply takes the lock back before the reply settles, so it never races the running turn."""
    lifecycle = ResponseLifecycleCoordinator()
    settled_while_other_ran: list[bool] = []
    running, release = asyncio.Event(), asyncio.Event()

    async def waiting_reply(_target: MessageTarget) -> str:
        try:
            async with released_while_waiting():
                await asyncio.Event().wait()
        finally:
            settled_while_other_ran.append(running.is_set() and not release.is_set())
        raise AssertionError

    async def other(_target: MessageTarget) -> str:
        running.set()
        await release.wait()
        return "$other"

    reply = asyncio.create_task(
        lifecycle.run_locked_response(
            target=_TARGET,
            response_envelope=_envelope("$first"),
            pipeline_timing=None,
            locked_operation=waiting_reply,
        ),
    )
    await asyncio.sleep(0)
    turn = asyncio.create_task(
        lifecycle.run_locked_response(
            target=_TARGET,
            response_envelope=_envelope("$other"),
            pipeline_timing=None,
            locked_operation=other,
        ),
    )
    await asyncio.wait_for(running.wait(), 5)
    reply.cancel()
    await asyncio.sleep(0.05)
    assert not reply.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await reply
    assert await turn == "$other"
    assert settled_while_other_ran == [False]
    assert not lifecycle.has_active_response_for_target(_TARGET)


@pytest.mark.asyncio
async def test_waiting_reply_keeps_its_conversation_lock_from_eviction() -> None:
    """A full lock table never drops the lock a waiting reply will take back."""
    lifecycle = ResponseLifecycleCoordinator()
    reply = _Reply(lifecycle)
    reply.start()
    await asyncio.wait_for(reply.waiting.wait(), 5)
    held = lifecycle._response_lifecycle_locks[_TARGET.lifecycle_key]
    for index in range(150):
        lifecycle._response_lifecycle_lock(MessageTarget.resolve("!other:localhost", f"$root{index}", f"$e{index}"))
    assert lifecycle._response_lifecycle_locks[_TARGET.lifecycle_key] is held
    reply.work_done.set()
    assert reply.task is not None
    assert await reply.task == "$first"
