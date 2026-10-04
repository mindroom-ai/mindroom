"""Generated conversations checked against the rule that the latest reply's message holds outstanding background work.

The response lifecycle, the response boundary, the job runtime, the journal's held replies, and the runner's hold,
release, wake, continuation, and Stop decisions are real. Only the model and Matrix delivery are scripted: a reply may
start jobs, and every turn retrieves each ready outcome its boundary or wake offers, as the model does with the job
tool. The event loop may be torn down between any two awaits.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal
from unittest.mock import MagicMock, patch

from mindroom.constants import STREAM_STATUS_KEY, STREAM_STATUS_STREAMING
from mindroom.delivery_gateway import EditTextRequest
from mindroom.event_journal import EventKind, JournalEvent
from mindroom.final_delivery import FinalDeliveryOutcome
from mindroom.orchestration.tool_job_runtime import ToolJobRuntimeCoordinator
from mindroom.tool_jobs.completion import (
    _JOB_JOIN_LIMIT,
    ReplyBoundaryReport,
    join_conversation_jobs,
    reply_boundary_report,
)
from mindroom.tool_jobs.held_replies import HeldReply, HoldKey, _wake_event_id, decode_held_reply
from mindroom.tool_jobs.instances import pin_background_tool_jobs
from mindroom.tool_jobs.runtime import TERMINAL_STATUSES, BackgroundOutcome, register_background_runtime
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context, tool_runtime_context
from tests.conftest import unwrap_extracted_collaborator
from tests.response_runner_helpers import _bot, _plain_request, _target
from tests.tool_job_helpers import run_journal_statements_inline, start_job, tool_job_runtime

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import pytest

    from mindroom.message_target import MessageTarget
    from mindroom.response_runner import ResponseRequest, ResponseRunner
    from mindroom.tool_jobs.runtime import BackgroundJob

_JOB_ID = re.compile(r'job_id="([^"]+)"')
_THREAD = "$thread"
# Loop iterations one step may take before it counts as livelocked.
_IDLE_ROUNDS = 20_000


@dataclass(frozen=True)
class Step:
    """A printable, shrinkable step; it names jobs by index, never by identity."""

    kind: Literal[
        "message",
        "silenced",
        "release",
        "wake",
        "race",
        "stop_held",
        "stop_live",
        "fail_next",
        "ignore_next",
        "crash",
    ]
    # Jobs the reply to a message starts before its response boundary.
    jobs: int = 0
    # Those jobs wait for a release.
    hold: bool = False
    index: int = 0


@dataclass
class _Turn:
    """One reply, or one turn continuing a held message."""

    source: str
    order: int
    message: str
    task: asyncio.Task[None] | None = None
    started: bool = False

    @property
    def live(self) -> bool:
        return self.task is not None and not self.task.done()


@dataclass
class _Model:
    # The receipt order of each source a turn ran for: a message or a wake.
    orders: dict[str, int] = field(default_factory=dict)
    # The source whose turn started each job.
    sources: dict[str, str] = field(default_factory=dict)
    # The first turn that retrieved each job's outcome.
    consumers: dict[str, str] = field(default_factory=dict)
    turns: list[_Turn] = field(default_factory=list)
    # The message of the latest turn that reached its response boundary: the one that may hold the work.
    latest_boundary_message: str | None = None
    # Work can be left unheld until the next reply: a continuation failed, or a message used up its joins.
    unheld_allowed: bool = False
    # Outcomes the model left unread; each waits for the conversation's next reply.
    unread: set[str] = field(default_factory=set)
    # A crash cut a turn short somewhere in its settlement, so holds are exact again only after the next boundary.
    uncertain: bool = False
    # The last edit of each message, as a client shows it.
    shown: dict[str, EditTextRequest] = field(default_factory=dict)


class HeldReplyFuzzRunner:
    """Drive one agent's conversation through generated steps and process losses over durable storage."""

    def __init__(self, root: Path, patch_: pytest.MonkeyPatch) -> None:
        # Blocking work runs on the event loop, so an idle loop marks the end of each step.
        patch_.setattr(asyncio, "to_thread", _inline)
        run_journal_statements_inline(patch_)
        self._root = root
        self.bot = _bot(root)
        self.bot.config.background_tool_jobs.enabled = True
        self.runner: ResponseRunner = unwrap_extracted_collaborator(self.bot._response_runner)
        request = _plain_request(_target(thread_id=_THREAD))
        self.envelope = request.response_envelope
        context = self.runner.deps.tool_runtime.build_context(
            self.envelope.target,
            user_id=self.envelope.requester_id,
            source_envelope=self.envelope,
        )
        assert context is not None
        self.context = context
        self.owner = build_execution_identity_from_runtime_context(context)
        self.key = HoldKey(
            recipient=context.recipient,
            room_id=context.room_id,
            thread_id=context.resolved_thread_id,
            requester_id=context.requester_id,
            silent=False,
            participants=(context.agent_name,),
        )
        self.model = _Model()
        self.gates: dict[str, asyncio.Event] = {}
        self._order = 0
        self._crashing = False
        self._failing = False
        self._ignoring = False
        # Wakes the journal still owes a turn: admitted, and not yet run to the end.
        self.pending_wakes: dict[str, JournalEvent] = {}
        self.woken: list[HeldReply] = []
        self.woken_now: list[HeldReply] = []
        self._baseline = asyncio.all_tasks()

        async def edit(_gateway: object, request: EditTextRequest) -> bool:
            self.model.shown[request.event_id] = request
            return True

        self._patches = [
            patch.object(type(self.runner.deps.delivery_gateway), "edit_text", edit),
            patch.object(self.runner, "_generate_held_continuation", self._continuation),
        ]

    async def open(self) -> None:
        """Start one process over the shared storage; the one before it, if any, is gone."""
        for active in self._patches:
            active.start()
        await self._open_runtime()

    async def _open_runtime(self) -> None:
        self.runtime = await tool_job_runtime(self._root)
        pin_background_tool_jobs(self.bot.config, self.bot.runtime_paths)
        register_background_runtime(self.bot.runtime_paths, self.runtime)
        await self.runtime.recover()

        async def wake_held_reply(hold: HeldReply) -> None:
            self.woken.append(hold)

        bot = MagicMock(running=True, wake_held_reply=wake_held_reply)
        bot.client.rooms = {self.key.room_id: object()}
        self.coordinator = ToolJobRuntimeCoordinator(
            runtime_paths=self.bot.runtime_paths,
            config_provider=lambda: self.bot.config,
            bot_provider=lambda _name: bot,
            agent_reply_memberships=MagicMock(),
            journal_provider=lambda: self.bot._journal_store,
        )
        self.coordinator._runtime = self.runtime
        self.coordinator._journal = self.bot._journal_store

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
        """Release all work and wake held messages until none is left; unheld work waits for the next reply."""
        # From here on the model reads everything and no continuation fails.
        self._ignoring = self._failing = False
        for gate in self.gates.values():
            gate.set()
        await self._idle()
        await self._wake_until_quiet()
        if self._outstanding() and (self.model.unheld_allowed or self._unread()):
            # Work a failed continuation left unheld, and outcomes left unread, wait for the next reply.
            await self.step(Step("message"))
            await self._wake_until_quiet()
        await self.check()
        assert not self._outstanding(), [job.job_id for job in self._outstanding()]
        assert await self._hold() is None
        assert not self._held_messages(), self._held_messages()

    async def _wake_until_quiet(self) -> None:
        for _ in range(4 * _JOB_JOIN_LIMIT):
            await self.step(Step("wake"))
            if not self.woken_now and not self.pending_wakes:
                return
        msg = "Held messages kept waking"
        raise AssertionError(msg)

    async def close(self) -> None:
        """End every task this example started and release storage, even after a failed check."""
        for gate in self.gates.values():
            gate.set()
        await self._end_tasks()
        await self.runtime.shutdown()
        for active in reversed(self._patches):
            active.stop()

    def _next_order(self) -> int:
        self._order += 1
        return self._order

    def _jobs(self) -> list[BackgroundJob]:
        return [entry.job for entry in self.runtime._entries.values()]

    def _outstanding(self) -> list[BackgroundJob]:
        """Work no Stop ended whose execution is still running or whose outcome no turn retrieved."""
        return [
            job
            for job in self._jobs()
            if job.user_stop_receipt_order is None and (job.status not in TERMINAL_STATUSES or not job.consumed)
        ]

    def _unread(self) -> list[BackgroundJob]:
        return [job for job in self._outstanding() if job.job_id in self.model.unread]

    def _live(self) -> list[_Turn]:
        return [turn for turn in self.model.turns if turn.live]

    def _held_messages(self) -> list[str]:
        return sorted(
            message
            for message, edit in self.model.shown.items()
            if (edit.extra_content or {}).get(STREAM_STATUS_KEY) == STREAM_STATUS_STREAMING
        )

    async def _hold(self) -> HeldReply | None:
        saved = await self.runner.deps.held_replies.load(self.key.hold_id)
        return decode_held_reply(saved) if saved is not None else None

    def _show_final(self, message: str, outcome: FinalDeliveryOutcome, text: str) -> None:
        """Record the turn's own final delivery, as the client shows it."""
        self.model.shown[message] = EditTextRequest(
            target=_target(thread_id=_THREAD),
            event_id=message,
            new_text=text,
            extra_content=dict(outcome.extra_content or {}),
        )

    async def _message(self, step: Step) -> None:
        """A message this agent answers; its reply queues behind any running turn of the conversation."""
        order = self._next_order()
        source = f"$message{order}"
        self.model.orders[source] = order
        self._reply(source, f"$reply{order}", step)

    def _reply(self, source: str, message: str, step: Step) -> None:
        turn = _Turn(source, self.model.orders[source], message=message)
        request = _plain_request(_target(thread_id=_THREAD), source_event_id=source)
        turn.task = asyncio.create_task(self._run_turn(turn, request, step))
        self.model.turns.append(turn)

    async def _silenced(self, _step: Step) -> None:
        """A reply whose participation check keeps the agent silent: it never reaches its response boundary."""
        order = self._next_order()
        source = f"$silenced{order}"
        self.model.orders[source] = order
        request = _plain_request(_target(thread_id=_THREAD), source_event_id=source)

        async def operation(_target: MessageTarget) -> None:
            await self.runner.held_messages.settle(
                request,
                FinalDeliveryOutcome(terminal_status="completed", event_id=None, suppressed=True),
                None,
                continued=None,
                stop_button_event_id=None,
            )

        turn = _Turn(source, order, message="")
        turn.task = asyncio.create_task(
            self.runner._lifecycle_coordinator.run_locked_response(
                target=request.response_envelope.target,
                response_envelope=request.response_envelope,
                pipeline_timing=None,
                locked_operation=operation,
            ),
        )
        self.model.turns.append(turn)

    async def _continuation(self, request: ResponseRequest) -> None:
        """The runner's turn for a wake: scripted like a reply, on the held message."""
        assert request.held_reply is not None
        assert request.held_reply.message_event_id is not None
        source = request.response_envelope.source_event_id
        order = self._next_order()
        self.model.orders[source] = order
        turn = _Turn(source, order, message=request.held_reply.message_event_id)
        self.model.turns.append(turn)
        turn.task = asyncio.current_task()  # type: ignore[assignment]
        await self._run_turn(turn, request, None, signal_queued_message=False)

    async def _run_turn(
        self,
        turn: _Turn,
        request: ResponseRequest,
        step: Step | None,
        *,
        signal_queued_message: bool = True,
    ) -> None:
        report = ReplyBoundaryReport()

        async def operation(_target: MessageTarget) -> None:
            nonlocal request
            resumed = await self.runner.held_messages.resume(request)
            if resumed is None:
                return
            request = resumed
            if request.on_lifecycle_lock_acquired is not None:
                request.on_lifecycle_lock_acquired()
            turn.started = True
            outcome = await self._attempts(turn, request, step, report)
            if report.boundary is not None:
                self.model.latest_boundary_message = turn.message
                self.model.unheld_allowed = self.model.unheld_allowed and report.boundary.joins >= _JOB_JOIN_LIMIT
                self.model.uncertain = False
            await self.runner.held_messages.settle(
                request,
                outcome,
                report.boundary,
                continued=request.held_reply,
                stop_button_event_id=None,
            )

        await self.runner._lifecycle_coordinator.run_locked_response(
            target=request.response_envelope.target,
            response_envelope=request.response_envelope,
            pipeline_timing=None,
            locked_operation=operation,
            signal_queued_message=signal_queued_message,
        )

    async def _attempts(
        self,
        turn: _Turn,
        request: ResponseRequest,
        step: Step | None,
        report: ReplyBoundaryReport,
    ) -> FinalDeliveryOutcome:
        """Run the scripted model up to the response boundary and deliver the turn's final answer."""
        seed = request.held_continuation
        attempted: set[str] = set(seed.attempted_job_ids) if seed is not None else set()
        joins = seed.joins + 1 if seed is not None else 0
        try:
            with tool_runtime_context(self.context), reply_boundary_report(report):
                if seed is not None:
                    await self._retrieve(request.prompt, turn)
                    if self._failing:
                        self._failing = False
                        self.model.unheld_allowed = True
                        return self._delivered(turn, "error", "A continuation failed.")
                for _ in range(step.jobs if step is not None else 0):
                    await self._start_job(turn.source, hold=step is not None and step.hold)
                while True:
                    join = await join_conversation_jobs(attempted, joins=joins)
                    if join.prompt is None:
                        break
                    joins += 1
                    await self._retrieve(join.prompt, turn)
                if joins >= _JOB_JOIN_LIMIT:
                    self.model.unheld_allowed = True
        except asyncio.CancelledError:
            if self._crashing:
                raise
            # A Stop: the turn settles its message as stopped before the cancellation ends it.
            outcome = self._delivered(turn, "cancelled", "Stopped.")
            await self.runner.held_messages.settle(
                request,
                outcome,
                report.boundary,
                continued=request.held_reply,
                stop_button_event_id=None,
            )
            raise
        return self._delivered(turn, "completed", f"Answer of {turn.source}.")

    def _delivered(
        self,
        turn: _Turn,
        status: Literal["completed", "cancelled", "error"],
        text: str,
    ) -> FinalDeliveryOutcome:
        outcome = FinalDeliveryOutcome(
            terminal_status=status,
            event_id=turn.message,
            is_visible_response=True,
            final_visible_body=text,
            cancel_source="user_stop" if status == "cancelled" else None,
            extra_content={STREAM_STATUS_KEY: status},
        )
        self._show_final(turn.message, outcome, text)
        return outcome

    async def _retrieve(self, prompt: str, turn: _Turn) -> None:
        """Retrieve every outcome a prompt names, as the model does with the job tool, unless told to ignore one."""
        if self._ignoring:
            # An outcome the model leaves unread waits for the conversation's next reply.
            self._ignoring = False
            self.model.unread.update(_JOB_ID.findall(prompt))
            return
        for job_id in _JOB_ID.findall(prompt):
            waited = await self.runtime.wait(job_id, owner=self.owner, depth=0, timeout=0)
            if waited.claim is not None:
                await self.runtime.acknowledge_wait(job_id, waited.claim, source_event_id=turn.source)
                self.model.consumers.setdefault(job_id, turn.source)

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

    async def _release(self, step: Step) -> None:
        held = sorted(job_id for job_id, gate in self.gates.items() if not gate.is_set())
        if held:
            self.gates[held[step.index % len(held)]].set()

    async def _wake(self, _step: Step) -> None:
        """One coordinator pass; each wake it admits runs the runner's own handling of that wake."""
        self.woken.clear()
        await self.coordinator._wake_held_replies()
        self.woken_now = list(self.woken)
        for hold in self.woken_now:
            event = JournalEvent(
                event_id=_wake_event_id(hold),
                room_id=hold.key.room_id,
                thread_id=hold.key.thread_id,
                kind=EventKind.HELD_REPLY_WAKE,
                sender=self.bot.matrix_id.full_id,
                origin_server_ts=0,
                source={},
                receipt_order=self._order,
            )
            self.pending_wakes[event.event_id] = event
            self._dispatch_wake(event)

    async def _race(self, step: Step) -> None:
        """Wake held messages and answer a message at once, so their turns queue for the conversation together."""
        await self._wake(step)
        await self._message(step)

    def _dispatch_wake(self, event: JournalEvent) -> None:
        """Run a wake as the journal does; one whose turn did not run to the end stays pending."""

        def settled(task: asyncio.Task[None]) -> None:
            if not task.cancelled() and task.exception() is None:
                self.pending_wakes.pop(event.event_id, None)

        asyncio.create_task(self.runner._continue_held_reply(event)).add_done_callback(settled)

    async def _stop_held(self, _step: Step) -> None:
        """Stop on the held message while no turn runs on it."""
        hold = await self._hold()
        if hold is None or hold.message_event_id is None:
            return
        if any(turn.message == hold.message_event_id for turn in self._live()):
            return
        assert await self.runner.held_messages.stop(hold.message_event_id, self._next_order())

    async def _stop_live(self, _step: Step) -> None:
        """Stop the running turn: it ends, and so does the work of it and every earlier turn."""
        live = [turn for turn in self._live() if turn.started]
        if not live:
            return
        stopped = live[-1]
        order = self._next_order()
        assert stopped.task is not None
        stopped.task.cancel()

        async def through_stopped_turn(job: BackgroundJob) -> bool:
            return self.model.orders[self.model.sources[job.job_id]] <= stopped.order

        await self.runtime.stop_jobs(receipt_order=order, matches=through_stopped_turn)

    async def _fail_next(self, _step: Step) -> None:
        self._failing = True

    async def _ignore_next(self, _step: Step) -> None:
        self._ignoring = True

    async def _crash(self, _step: Step) -> None:
        """Tear the event loop down between two awaits, then start again over what was saved.

        The journal re-runs what it still owes: each interrupted reply, which retrieves the work it started instead
        of starting it again, and each wake whose turn did not finish.
        """
        interrupted = [turn for turn in self.model.turns if turn.live and turn.source.startswith("$message")]
        self.model.uncertain = self.model.uncertain or any(turn.live for turn in self.model.turns)
        self._crashing = True
        try:
            await self._end_tasks()
        finally:
            self._crashing = False
        for gate in self.gates.values():
            gate.set()
        await self.runtime.close_journal()
        await self._open_runtime()
        for turn in interrupted:
            self._reply(turn.source, f"{turn.message}-rerun", Step("message"))
        for event in list(self.pending_wakes.values()):
            self._dispatch_wake(event)

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
        """Yield until no task is runnable; every waiter then awaits a gate, a lock, or a wake."""
        loop = asyncio.get_running_loop()
        for _ in range(_IDLE_ROUNDS):
            await asyncio.sleep(0)
            if not loop._ready:  # type: ignore[attr-defined]
                return
        msg = "Held reply fuzz step never became idle"
        raise AssertionError(msg)

    async def check(self) -> None:
        """Compare holds, what each message shows, outstanding work, and consumption with what was saved."""
        hold = await self._hold()
        held_messages = self._held_messages()
        if self.model.uncertain:
            pass
        elif hold is None:
            assert held_messages == [], held_messages
        else:
            # Only the latest reply's message holds the work, and only it shows the waiting notice.
            assert hold.message_event_id == self.model.latest_boundary_message, (
                hold.message_event_id,
                self.model.latest_boundary_message,
            )
            if not any(turn.message == hold.message_event_id for turn in self._live()):
                assert held_messages == [hold.message_event_id], held_messages
        outstanding = [job for job in self._outstanding() if job.job_id not in self.model.unread]
        if outstanding and not self._live() and not self.pending_wakes and not self.model.unheld_allowed:
            # Outstanding work always has a message holding it.
            assert hold is not None, [job.job_id for job in outstanding]
        for job in self._jobs():
            consumer = self.model.consumers.get(job.job_id)
            # An outcome is retrieved once, by a turn no older than the turn that started the job.
            assert job.consumed_by_source == consumer, (job.job_id, job.consumed_by_source, consumer)
            if consumer is not None:
                assert self.model.orders[consumer] >= self.model.orders[self.model.sources[job.job_id]]
            if job.user_stop_receipt_order is not None and consumer is not None:
                # Stop keeps its work from being continued automatically.
                assert self.model.orders[consumer] < job.user_stop_receipt_order, job.job_id


async def _inline[Result](function: Callable[..., Result], /, *args: object, **kwargs: object) -> Result:
    return function(*args, **kwargs)
