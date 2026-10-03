"""Generated conversations checked against the rule that the latest reply holds outstanding background work.

The response lifecycle, the reply join, and the job runtime are real: they serialize replies, hand work over to a
newer reply, and decide what each join waits for. Only a reply's model is scripted: it may start jobs, then retrieves
each ready outcome its join offers, as the model does with the job tool. The event loop may be torn down between any
two awaits.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Literal

from mindroom.dispatch_source import MESSAGE_SOURCE_KIND
from mindroom.hooks import MessageEnvelope
from mindroom.message_target import MessageTarget
from mindroom.response_lifecycle import ResponseLifecycleCoordinator
from mindroom.tool_jobs.completion import JOB_JOIN_LIMIT, join_conversation_jobs
from mindroom.tool_jobs.instances import pin_background_tool_jobs
from mindroom.tool_jobs.runtime import TERMINAL_STATUSES, BackgroundOutcome, register_background_runtime
from mindroom.tool_system.events import BackgroundWaitChunk
from mindroom.tool_system.runtime_context import tool_runtime_context
from tests.conftest import message_origin, test_runtime_paths
from tests.delegation_helpers import _delegate_runtime_context
from tests.tool_job_helpers import completed_delegation_job, managed_team_config, start_job, tool_job_runtime

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import pytest

    from mindroom.tool_jobs.runtime import BackgroundJob

_JOB_ID = re.compile(r'job_id="([^"]+)"')
# Loop iterations one step may take before it counts as livelocked.
_IDLE_ROUNDS = 20_000


@dataclass(frozen=True)
class Step:
    """A printable, shrinkable step; it names jobs by index, never by identity."""

    kind: Literal["message", "other", "release", "stop", "crash"]
    # Jobs the reply to a message starts before it joins its conversation's work.
    jobs: int = 0
    # Those jobs wait for a release.
    hold: bool = False
    index: int = 0


@dataclass
class _Reply:
    source: str
    order: int
    task: asyncio.Task[str] | None = None
    joins: int = 0

    @property
    def live(self) -> bool:
        return self.task is not None and not self.task.done()


@dataclass
class _Model:
    # The receipt order of each message the agent answered, by source.
    orders: dict[str, int] = field(default_factory=dict)
    # The message whose reply started each job.
    sources: dict[str, str] = field(default_factory=dict)
    # The first reply that retrieved each job's outcome.
    consumers: dict[str, str] = field(default_factory=dict)
    replies: list[_Reply] = field(default_factory=list)
    # No reply started since the last process loss, so the outcomes it interrupted wait for the next message.
    restarted: bool = False


class ReplyHoldFuzzRunner:
    """Drive one agent's conversation through generated steps and process losses over durable storage."""

    def __init__(self, root: Path, patch: pytest.MonkeyPatch) -> None:
        # Blocking work runs on the event loop, so an idle loop marks the end of each step.
        patch.setattr(asyncio, "to_thread", _inline)
        self._root = root
        self._paths = test_runtime_paths(root)
        self._config = managed_team_config(root)
        self.owner = completed_delegation_job().owner
        self.context = replace(
            _delegate_runtime_context(self._config, self._paths, execution_identity=self.owner),
            agent_name=self.owner.agent_name,
            transport_agent_name=self.owner.transport_agent_name,
        )
        self.model = _Model()
        self.gates: dict[str, asyncio.Event] = {}
        self._order = 0
        self._baseline = asyncio.all_tasks()

    async def open(self) -> None:
        """Start one process over the shared storage."""
        self.runtime = tool_job_runtime(self._root)
        pin_background_tool_jobs(self._config, self._paths)
        register_background_runtime(self._paths, self.runtime)
        await self.runtime.recover()
        self.lifecycle = ResponseLifecycleCoordinator()

    async def run(self, steps: list[Step]) -> None:
        """Apply each step, then finish the conversation and check the settled state."""
        for step in steps:
            await self.step(step)
        await self.finish()

    async def step(self, step: Step) -> None:
        """Apply one step, run every task it woke to a standstill, then check every invariant."""
        await getattr(self, f"_{step.kind}")(step)
        await self._idle()
        await self.check()

    async def finish(self) -> None:
        """Release all work; outcomes no reply holds, as after a restart, wait for the next message's reply."""
        for gate in self.gates.values():
            gate.set()
        await self._idle()
        if self._outstanding() and not self._live():
            await self.step(Step("message"))
        await self.check()
        assert not self._outstanding(), [job.job_id for job in self._outstanding()]
        assert not self._live(), [reply.source for reply in self._live()]

    async def close(self) -> None:
        """End every task this example started and release storage, even after a failed check."""
        for gate in self.gates.values():
            gate.set()
        await self._end_tasks()
        await self.runtime.shutdown()

    def _target(self, source: str) -> MessageTarget:
        assert self.owner.room_id is not None
        return MessageTarget.resolve(self.owner.room_id, self.owner.resolved_thread_id, source)

    def _envelope(self, source: str) -> MessageEnvelope:
        return MessageEnvelope(
            source_event_id=source,
            target=self._target(source),
            body="hello",
            attachment_ids=(),
            mentioned_agents=(),
            agent_name=self.owner.recipient,
            origin=message_origin(
                sender_id=self.owner.requester_id or "",
                requester_id=self.owner.requester_id,
                source_kind=MESSAGE_SOURCE_KIND,
            ),
        )

    def _jobs(self) -> list[BackgroundJob]:
        return [entry.job for entry in self.runtime._entries.values()]

    def _outstanding(self) -> list[BackgroundJob]:
        """Work no Stop ended whose execution is still running or whose outcome no reply retrieved."""
        return [
            job
            for job in self._jobs()
            if job.user_stop_receipt_order is None and (job.status not in TERMINAL_STATUSES or not job.consumed)
        ]

    def _live(self) -> list[_Reply]:
        return [reply for reply in self.model.replies if reply.live]

    async def _message(self, step: Step) -> None:
        """A human message this agent answers; its reply queues behind, and takes over from, the holding reply."""
        self._order += 1
        source = f"$message{self._order}"
        self.model.orders[source] = self._order
        reply = _Reply(source, self._order)
        reply.task = asyncio.create_task(
            self.lifecycle.run_locked_response(
                target=self._target(source),
                response_envelope=self._envelope(source),
                pipeline_timing=None,
                locked_operation=lambda _target: self._reply(reply, step),
            ),
        )
        self.model.replies.append(reply)
        self.model.restarted = False

    async def _reply(self, reply: _Reply, step: Step) -> str:
        """Start the scripted jobs, then join the conversation's work until none is left to hold."""
        with tool_runtime_context(self.context):
            for _ in range(step.jobs):
                await self._start_job(reply.source, hold=step.hold)
            attempted: set[str] = set()
            while reply.joins < JOB_JOIN_LIMIT:
                prompt = None
                async for item in join_conversation_jobs(attempted):
                    if not isinstance(item, BackgroundWaitChunk):
                        prompt = item.prompt
                if prompt is None:
                    break
                reply.joins += 1
                for job_id in _JOB_ID.findall(prompt):
                    waited = await self.runtime.wait(job_id, owner=self.owner, depth=0, timeout=0)
                    if waited.claim is not None:
                        await self.runtime.acknowledge_wait(job_id, waited.claim, source_event_id=reply.source)
                        self.model.consumers.setdefault(job_id, reply.source)
        return reply.source

    async def _start_job(self, source: str, *, hold: bool) -> None:
        job_id = f"job{len(self.model.sources)}"
        self.model.sources[job_id] = source
        gate = self.gates.setdefault(job_id, asyncio.Event())
        if not hold:
            gate.set()

        async def operation() -> BackgroundOutcome:
            await gate.wait()
            return BackgroundOutcome("completed", f"Result of {job_id}")

        await start_job(
            self.runtime,
            job_id,
            tool_name="tool",
            depth=0,
            source_event_id=source,
            adapter={},
            owner=self.owner,
            operation=operation,
        )

    async def _other(self, _step: Step) -> None:
        """A human message in the conversation that the turn policy then gives to another agent."""
        self._order += 1
        source = f"$other{self._order}"
        reservation = self.lifecycle.reserve_waiting_human_message(
            target=self._target(source),
            response_envelope=self._envelope(source),
        )
        await self._idle()
        if reservation is not None:
            reservation.cancel()

    async def _release(self, step: Step) -> None:
        held = sorted(job_id for job_id, gate in self.gates.items() if not gate.is_set())
        if held:
            self.gates[held[step.index % len(held)]].set()

    async def _stop(self, _step: Step) -> None:
        """Stop the latest reply: it ends, and so does the work of its turn and every earlier one."""
        if not self.model.replies:
            return
        stopped = self.model.replies[-1]
        self._order += 1

        async def through_stopped_reply(job: BackgroundJob) -> bool:
            return self.model.orders[self.model.sources[job.job_id]] <= stopped.order

        if stopped.task is not None:
            stopped.task.cancel()
        await self.runtime.stop_jobs(receipt_order=self._order, matches=through_stopped_reply)

    async def _crash(self, _step: Step) -> None:
        """Tear the event loop down between two awaits, then start again over what was saved."""
        await self._end_tasks()
        self.runtime._lease.close()
        for gate in self.gates.values():
            gate.set()
        self.model.restarted = True
        await self.open()

    async def _end_tasks(self) -> None:
        current = asyncio.current_task()
        while pending := [
            task
            for task in asyncio.all_tasks()
            if task not in self._baseline and task is not current and not task.done()
        ]:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    async def _idle(self) -> None:
        """Yield until no task is runnable; every waiter then awaits a gate or the hand-over signal."""
        loop = asyncio.get_running_loop()
        for _ in range(_IDLE_ROUNDS):
            await asyncio.sleep(0)
            if not loop._ready:  # type: ignore[attr-defined]
                return
        msg = "Reply hold fuzz step never became idle"
        raise AssertionError(msg)

    async def check(self) -> None:
        """Compare live replies, outstanding work, and consumption with what the runtime saved."""
        live = self._live()
        # The lifecycle serializes replies, and a newer reply takes over at once.
        assert len(live) <= 1, [reply.source for reply in live]
        outstanding = self._outstanding()
        limited = bool(self.model.replies) and self.model.replies[-1].joins >= JOB_JOIN_LIMIT
        if outstanding and not self.model.restarted and not limited:
            # Outstanding work always has a holding reply: the latest one.
            assert live == self.model.replies[-1:], [job.job_id for job in outstanding]
        if live:
            # A holding reply retrieves every ready outcome as soon as it is ready.
            ready = [job.job_id for job in outstanding if job.status in TERMINAL_STATUSES]
            assert not ready, ready
        for job in self._jobs():
            consumer = self.model.consumers.get(job.job_id)
            # An outcome is retrieved once, by a reply no older than the turn that started the job.
            assert job.consumed_by_source == consumer, (job.job_id, job.consumed_by_source, consumer)
            if consumer is not None:
                assert self.model.orders[consumer] >= self.model.orders[self.model.sources[job.job_id]]
            if job.user_stop_receipt_order is not None and consumer is not None:
                # Stop keeps its work from being continued automatically.
                assert self.model.orders[consumer] < job.user_stop_receipt_order, job.job_id


async def _inline[Result](function: Callable[..., Result], /, *args: object, **kwargs: object) -> Result:
    return function(*args, **kwargs)
