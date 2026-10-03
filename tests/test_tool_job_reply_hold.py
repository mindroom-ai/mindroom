"""Turns queued behind a running reply end that reply's waits inside its model run, so its message holds the work."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from mindroom.dispatch_source import MESSAGE_SOURCE_KIND, SCHEDULED_SOURCE_KIND, SILENT_SCHEDULE_SOURCE_KIND
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
class _Reply:
    """A reply whose model run waits on background work until something releases that wait."""

    lifecycle: ResponseLifecycleCoordinator
    source: str = "$first"
    started: asyncio.Event = field(default_factory=asyncio.Event)
    model_wait_released: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[str] | None = None

    async def _operation(self, _target: MessageTarget) -> str:
        self.started.set()
        signal = current_human_message_signal()
        assert signal is not None
        signal.subscribe(self.model_wait_released.set)
        try:
            await self.model_wait_released.wait()
        finally:
            signal.unsubscribe(self.model_wait_released.set)
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
    """Any turn queued behind a reply waiting inside its model run lets that reply finish, and then runs.

    The turn that started consumed its notice, so the waits of its own model run stay attached.
    """
    lifecycle = ResponseLifecycleCoordinator()
    reply = _Reply(lifecycle)
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
    assert reply.task is not None
    assert await reply.task == "$first"
    assert await asyncio.wait_for(turn, 5) == "$queued"
    assert not own_waits_released.is_set()
    assert not lifecycle._thread_queued_signals[_TARGET.lifecycle_key].has_pending_human_messages()


@pytest.mark.asyncio
async def test_human_message_ends_the_waits_inside_the_model_run_at_ingress() -> None:
    """A new human message ends model-run waits as it arrives, so the reply stops holding it back in the queue."""
    lifecycle = ResponseLifecycleCoordinator()
    reply = _Reply(lifecycle)
    reply.start()
    await asyncio.wait_for(reply.started.wait(), 5)
    reservation = lifecycle.reserve_waiting_human_message(target=_TARGET, response_envelope=_envelope("$other"))
    assert reservation is not None
    await asyncio.wait_for(reply.model_wait_released.wait(), 5)
    assert reply.task is not None
    assert await reply.task == "$first"
    reservation.cancel()
