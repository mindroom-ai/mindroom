"""The latest reply of an agent holds its conversation's background work until that agent answers a newer message."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from mindroom.dispatch_source import MESSAGE_SOURCE_KIND, SCHEDULED_SOURCE_KIND
from mindroom.hooks import MessageEnvelope
from mindroom.message_target import MessageTarget
from mindroom.response_lifecycle import ResponseLifecycleCoordinator
from mindroom.tool_jobs.control import current_human_message_signal
from tests.conftest import message_origin

_TARGET = MessageTarget.resolve("!room:localhost", "$root", "$first")


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
class _HoldingReply:
    """A reply that waits on background work until its human signal releases it."""

    lifecycle: ResponseLifecycleCoordinator
    started: asyncio.Event = field(default_factory=asyncio.Event)
    released: asyncio.Event = field(default_factory=asyncio.Event)
    finish: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[str] | None = None

    async def _operation(self, _target: MessageTarget) -> str:
        signal = current_human_message_signal()
        assert signal is not None
        signal.subscribe(self.released.set)
        self.started.set()
        try:
            await self.finish.wait()
        finally:
            signal.unsubscribe(self.released.set)
        return "$holding"

    async def start(self) -> None:
        self.task = asyncio.create_task(
            self.lifecycle.run_locked_response(
                target=_TARGET,
                response_envelope=_envelope("$first"),
                pipeline_timing=None,
                locked_operation=self._operation,
            ),
        )
        await asyncio.wait_for(self.started.wait(), 5)

    async def stop(self) -> None:
        self.finish.set()
        assert self.task is not None
        assert await self.task == "$holding"


@pytest.mark.asyncio
async def test_follow_up_ingress_alone_keeps_the_reply_holding() -> None:
    """A human message this agent may not answer leaves the holding reply waiting."""
    lifecycle = ResponseLifecycleCoordinator()
    reply = _HoldingReply(lifecycle)
    await reply.start()
    reservation = lifecycle.reserve_waiting_human_message(target=_TARGET, response_envelope=_envelope("$other"))
    assert reservation is not None
    await asyncio.sleep(0)
    assert not reply.released.is_set()
    # The turn policy decided another agent answers; the reply keeps holding.
    reservation.cancel()
    await asyncio.sleep(0)
    assert not reply.released.is_set()
    await reply.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("other_message_pending", [False, True])
async def test_newer_reply_of_the_same_agent_takes_over(other_message_pending: bool) -> None:
    """Queuing a reply to a newer human message releases the holding reply, and only until that reply starts.

    A message still waiting for the turn policy cannot keep releasing the newer reply's own waits.
    """
    lifecycle = ResponseLifecycleCoordinator()
    reply = _HoldingReply(lifecycle)
    await reply.start()
    other = (
        lifecycle.reserve_waiting_human_message(target=_TARGET, response_envelope=_envelope("$other"))
        if other_message_pending
        else None
    )
    newer_released = asyncio.Event()

    async def newer(_target: MessageTarget) -> str:
        signal = current_human_message_signal()
        assert signal is not None
        signal.subscribe(newer_released.set)
        signal.unsubscribe(newer_released.set)
        return "$newer"

    follow_up = asyncio.create_task(
        lifecycle.run_locked_response(
            target=_TARGET,
            response_envelope=_envelope("$second"),
            pipeline_timing=None,
            locked_operation=newer,
        ),
    )
    await asyncio.wait_for(reply.released.wait(), 5)
    await reply.stop()
    assert await follow_up == "$newer"
    # The newer reply consumed the hand-over, so its own waits stay attached.
    assert not newer_released.is_set()
    if other is not None:
        other.cancel()


@pytest.mark.asyncio
@pytest.mark.parametrize("silent", [False, True])
async def test_scheduled_turn_takes_over_unless_it_is_silent(silent: bool) -> None:
    """A visible scheduled reply takes the work over like any newer reply; a silent one cannot hold it and queues."""
    lifecycle = ResponseLifecycleCoordinator()
    reply = _HoldingReply(lifecycle)
    await reply.start()

    async def scheduled(_target: MessageTarget) -> str:
        return "$scheduled"

    queued = asyncio.create_task(
        lifecycle.run_locked_response(
            target=_TARGET,
            response_envelope=_envelope("$fire", source_kind=SCHEDULED_SOURCE_KIND),
            pipeline_timing=None,
            locked_operation=scheduled,
            # The response runner never lets a silent schedule signal the conversation.
            signal_queued_message=not silent,
        ),
    )
    if silent:
        await asyncio.sleep(0.05)
        assert not reply.released.is_set()
        assert not queued.done()
    else:
        await asyncio.wait_for(reply.released.wait(), 5)
    await reply.stop()
    assert await queued == "$scheduled"
    # The scheduled turn is no human message, so the running reply got no queued notice for it.
    assert not lifecycle._thread_queued_signals[_TARGET.lifecycle_key].has_pending_human_messages()


@pytest.mark.asyncio
async def test_new_messages_reach_the_turn_policy_while_the_reply_only_waits() -> None:
    """New messages wait behind a generating reply, but not behind one that only waits on background work.

    Otherwise a follow-up this agent would answer could never start the reply that takes the work over.
    """
    lifecycle = ResponseLifecycleCoordinator()
    room_id, thread_id = _TARGET.lifecycle_key.room_id, _TARGET.lifecycle_key.thread_id
    generating, waits = asyncio.Event(), asyncio.Event()
    reply = _HoldingReply(lifecycle)

    async def generate_then_wait(target: MessageTarget) -> str:
        generating.set()
        await waits.wait()
        return await reply._operation(target)

    reply.task = asyncio.create_task(
        lifecycle.run_locked_response(
            target=_TARGET,
            response_envelope=_envelope("$first"),
            pipeline_timing=None,
            locked_operation=generate_then_wait,
        ),
    )
    await asyncio.wait_for(generating.wait(), 5)
    assert lifecycle.thread_ids_holding_follow_ups(room_id) == frozenset({thread_id})
    dispatch = asyncio.create_task(lifecycle.wait_until_follow_ups_may_dispatch(room_id, thread_id))
    await asyncio.sleep(0)
    assert not dispatch.done()
    waits.set()
    await asyncio.wait_for(dispatch, 5)
    assert lifecycle.thread_ids_holding_follow_ups(room_id) == frozenset()
    await reply.stop()
    assert lifecycle.thread_ids_holding_follow_ups(room_id) == frozenset()
