"""Generated idle completion wakes checked against durable reply, Stop, and restart invariants.

A real response runner owns each wake: its admission, lifecycle lock, turn record, journal settlement, and Stop.
Only the reply itself is scripted: it may show a placeholder, consume the outcome it was woken for, start more
work, or keep running until released, and the event loop may be torn down between any two awaits.
"""

from __future__ import annotations

import asyncio
import re
from collections import Counter
from dataclasses import dataclass, field, replace
from functools import partial
from typing import TYPE_CHECKING, Literal
from unittest.mock import AsyncMock

from mindroom.event_journal import DeliveryStage, EventKind
from mindroom.event_journal.offloading import ThreadOffload
from mindroom.handled_turns import _reset_handled_turn_ledger_runtime
from mindroom.message_target import MessageTarget
from mindroom.response_sources import ResponseAttempt, ResponseSources
from mindroom.tool_jobs.completion import completion_event
from mindroom.tool_jobs.instances import pin_background_tool_jobs
from mindroom.tool_jobs.runtime import TERMINAL_STATUSES, BackgroundOutcome, register_background_runtime
from mindroom.tool_jobs.user_stop import restore_user_stops
from mindroom.user_stop_reconciliation import UserStopReconciler, UserStopReconcilerDeps
from tests.conftest import unwrap_extracted_collaborator
from tests.response_runner_helpers import _bot
from tests.test_event_journal_store import ROOM, admit
from tests.test_tool_job_stop_completion import _acknowledge_initial
from tests.test_user_stop_convergence import _CountingGateway
from tests.tool_job_helpers import job_owner, start_job, tool_job_runtime

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import pytest

    from mindroom.event_journal import PrincipalStore
    from mindroom.response_runner import ResponseRequest, _EarlyPlaceholderState

_JOB_ID = re.compile(r'job_id="([^"]+)"')
# Loop iterations one step may take before it counts as livelocked.
_IDLE_ROUNDS = 20_000


@dataclass(frozen=True)
class ReplyScript:
    """What one woken reply does once the runner admits it."""

    # Show a placeholder and finish with a visible reply; otherwise the reply decides to say nothing.
    visible: bool = True
    # Retrieve the outcome it was woken for, as the completion prompt asks.
    consume: bool = True
    # Start another job of its own.
    spawn: bool = False
    # Keep running until an explicit release.
    hold: bool = False


@dataclass(frozen=True)
class Step:
    """A printable, shrinkable step; it names jobs and replies by index, never by identity."""

    kind: Literal["job", "settle_human", "release", "deliver", "retry", "stop", "crash"]
    index: int = 0
    # A job's execution waits for a release.
    hold: bool = False
    # The human turn that started a job is still being answered.
    pending_human: bool = False
    # How the replies a delivery pass wakes behave.
    reply: ReplyScript = ReplyScript()


@dataclass
class _Stop:
    task: asyncio.Task[bool]
    reply: str
    order: int
    # Jobs the reply started whose work or unread outcome was still outstanding when Stop arrived.
    outstanding: tuple[str, ...]


@dataclass
class _Model:
    # Human sources, and whether each one's turn finished.
    humans: dict[str, bool] = field(default_factory=dict)
    # Each completion source's reply script, chosen when a delivery pass first offers it.
    scripts: dict[str, ReplyScript] = field(default_factory=dict)
    runs: Counter[str] = field(default_factory=Counter)
    # Process losses while a source's reply was running; each may cause one more run.
    interrupted: Counter[str] = field(default_factory=Counter)
    replies: dict[str, set[str]] = field(default_factory=dict)
    # Jobs each completion's reply started.
    children: dict[str, list[str]] = field(default_factory=dict)
    # The first source whose reply consumed each job.
    consumers: dict[str, str] = field(default_factory=dict)
    stops: dict[str, int] = field(default_factory=dict)


class CompletionFuzzRunner:
    """Drive one bot's completion wakes through generated steps and process losses over durable storage."""

    def __init__(self, root: Path, patch: pytest.MonkeyPatch) -> None:
        # Blocking work runs on the event loop, so an idle loop marks the end of each step and a teardown lands
        # between two awaits.
        patch.setattr(asyncio, "to_thread", _inline)
        patch.setattr(ThreadOffload, "submit", _inline_submit)
        self._patch = patch
        self._root = root
        self.target = MessageTarget.resolve(ROOM, "$thread", "$thread")
        self.owner = replace(
            job_owner(),
            agent_name="general",
            room_id=ROOM,
            thread_id="$thread",
            resolved_thread_id="$thread",
            session_id=self.target.session_id,
        )
        self.model = _Model()
        self.gate = asyncio.Event()
        self.stops: list[_Stop] = []
        self._live: dict[str, asyncio.Task[object]] = {}
        self._admitted: set[tuple[str, int]] = set()
        self._jobs = 0
        self._order = 0
        self._replies = 0
        self._baseline = asyncio.all_tasks()

    async def open(self) -> None:
        """Start one process over the shared storage: bot, job runtime, saved Stops, and pending wakes."""
        _reset_handled_turn_ledger_runtime()
        self.bot = _bot(self._root)
        self.runner = unwrap_extracted_collaborator(self.bot._response_runner)
        self.runner.deps.runtime.config.background_tool_jobs.enabled = True
        await self.bot._turn_store.warm()
        self.store = self.runner.deps.approval_store
        paths = self.runner.deps.runtime_paths
        self.runtime = tool_job_runtime(paths.storage_root)
        pin_background_tool_jobs(self.runner.deps.runtime.config, paths)
        register_background_runtime(paths, self.runtime)
        await self.runtime.recover()
        self._patch.setattr(self.runner, "generate_response", self._generate)
        self.reconciler = UserStopReconciler(
            UserStopReconcilerDeps(self.bot._turn_store, self.runner, _CountingGateway()),  # type: ignore[arg-type]
        )
        self._admitted.clear()
        # As the delivery coordinator does at startup, apply Stops saved while no runtime could receive them.
        await restore_user_stops(
            self.runtime,
            self.store,
            self.bot._journal_store.turn_records("general"),
            await self.runtime.stoppable_jobs(),
        )
        await self._retry()

    async def run(self, steps: list[Step]) -> None:
        """Apply each step, then finish every turn and check the settled state."""
        for step in steps:
            await self.step(step)
        await self.finish()

    async def step(self, step: Step) -> None:
        """Apply one step, run every task it woke to a standstill, then check every invariant."""
        await getattr(self, f"_{step.kind}")(step)
        await self._idle()
        await self.check()

    async def finish(self) -> None:
        """Answer every human turn, release all work, and wake until nothing is left to deliver."""
        self.gate.set()
        for source, settled in self.model.humans.items():
            if not settled:
                await self.store.settle(source)
                self.model.humans[source] = True
        for _ in range(10):
            await self._idle()
            await self._deliver(Step("deliver"))
            await self._retry()
            await self._idle()
            if not await self.store.pending_of_kind(EventKind.TOOL_JOB_COMPLETION):
                break
        await self.check()
        pending = await self.store.pending_of_kind(EventKind.TOOL_JOB_COMPLETION)
        # Every wake settles once the turns before it finished.
        assert not pending, [event.event_id for event in pending]
        for source in self.model.scripts:
            record = self.bot._turn_store.get_turn_record(source)
            assert record is None or record.completed, source
        assert not self._live_replies(), self._live_replies()

    async def close(self) -> None:
        """End every task this example started and release storage, even after a failed check."""
        self.gate.set()
        await self._end_tasks()
        self.runtime._lease.close()
        await self.bot._journal_store.close()

    def _live_replies(self) -> list[str]:
        return [source for source, task in self._live.items() if not task.done()]

    async def _generate(self, request: ResponseRequest) -> str | None:
        """Run a woken reply in the runner's real response lifecycle, with only its generation scripted."""
        source = request.response_envelope.source_event_id
        self._live[source] = asyncio.current_task()  # type: ignore[assignment]
        return await self.runner._run_locked_response_lifecycle(
            request,
            response_kind="ai",
            locked_operation=partial(self._reply, request),
            signal_queued_message=False,
        )

    async def _reply(
        self,
        request: ResponseRequest,
        target: MessageTarget,
        _early: _EarlyPlaceholderState,
    ) -> str | None:
        source = request.response_envelope.source_event_id
        script = self.model.scripts[source]
        self.model.runs[source] += 1
        reply = request.existing_event_id
        if reply is None and script.visible:
            self._replies += 1
            reply = f"$reply{self._replies}"
            # The placeholder is durable before the reply binds to it, as a delivered first attempt leaves it.
            await _acknowledge_initial(self.store, source, reply)
            assert request.on_visible_response is not None
            await request.on_visible_response(reply)
        if reply is not None:
            self.model.replies.setdefault(source, set()).add(reply)
            self.runner.deps.stop_manager.set_current(reply, target, asyncio.current_task())  # type: ignore[arg-type]
        if script.consume:
            for job_id in _JOB_ID.findall(request.prompt):
                waited = await self.runtime.wait(job_id, owner=self.owner, depth=0, timeout=0)
                if waited.claim is not None:
                    await self.runtime.acknowledge_wait(job_id, waited.claim, source_event_id=source)
                    self.model.consumers.setdefault(job_id, source)
        if script.spawn and source not in self.model.children:
            self.model.children[source] = [await self._start_job(source, hold=True)]
        if script.hold:
            await self.gate.wait()
        if reply is None:
            assert request.on_no_response_handled is not None
            await request.on_no_response_handled()
            return None
        # The answer's FINAL delivery takes the source over from the journal in one commit, as Matrix delivery does.
        await _deliver_final(self.store, source, reply)
        return reply

    async def _start_job(self, source: str, *, hold: bool) -> str:
        job_id = f"job{self._jobs}"
        self._jobs += 1

        async def operation() -> BackgroundOutcome:
            if hold:
                await self.gate.wait()
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
        return job_id

    async def _job(self, step: Step) -> None:
        human = f"$human{len(self.model.humans)}"
        await admit(self.store, human, thread_id="$thread")
        if not step.pending_human:
            await self.store.settle(human)
        self.model.humans[human] = not step.pending_human
        await self._start_job(human, hold=step.hold)

    async def _settle_human(self, step: Step) -> None:
        pending = [source for source, settled in self.model.humans.items() if not settled]
        if pending:
            source = pending[step.index % len(pending)]
            await self.store.settle(source)
            self.model.humans[source] = True

    async def _release(self, _step: Step) -> None:
        self.gate.set()
        await self._idle()
        self.gate = asyncio.Event()

    async def _deliver(self, step: Step) -> None:
        """Wake each ready outcome once per process, as the delivery coordinator does."""
        for job in await self.runtime.pending_outcomes():
            if (job.job_id, job.generation) in self._admitted:
                continue
            self._admitted.add((job.job_id, job.generation))
            event = completion_event(job, sender_id=self.bot.matrix_id.full_id)
            self.model.scripts.setdefault(event.event_id, step.reply)
            await self.store.admit(event)
            await self._handoff(event.event_id)

    async def _retry(self, _step: Step | None = None) -> None:
        """Hand every pending wake to the runner again, as the journal worker does with unsettled work."""
        for event in await self.store.pending_of_kind(EventKind.TOOL_JOB_COMPLETION):
            await self._handoff(event.event_id)

    async def _handoff(self, event_id: str) -> None:
        event = await self.store.load_event(event_id)
        assert event is not None
        await self.runner.handoff_tool_job_completion(event)

    async def _stop(self, step: Step) -> None:
        replies = sorted(reply for replies in self.model.replies.values() for reply in replies)
        if not replies:
            return
        reply = replies[step.index % len(replies)]
        # Stop is ordered by the receipt of its reaction, after everything already admitted.
        self._order += 1
        reaction = f"$stop{self._order}"
        await admit(self.store, reaction, thread_id="$thread")
        await self.store.settle(reaction)
        loaded = await self.store.load_event(reaction)
        assert loaded is not None
        sources = [source for source, replies in self.model.replies.items() if reply in replies]
        outstanding = []
        for child in (child for source in sources for child in self.model.children.get(source, ())):
            job = await self.runtime.lookup(child, owner=self.owner, depth=0)
            if job.status not in TERMINAL_STATUSES or not job.consumed:
                outstanding.append(child)
        task = asyncio.create_task(self.reconciler.finalize(reply, loaded.receipt_order, AsyncMock()))
        self.stops.append(_Stop(task, reply, loaded.receipt_order, tuple(outstanding)))

    async def _crash(self, _step: Step) -> None:
        """Tear the event loop down between two awaits, then start again over what was saved."""
        for source in self._live_replies():
            self.model.interrupted[source] += 1
        await self._end_tasks()
        self.stops.clear()
        self._live.clear()
        self.runtime._lease.close()
        await self.bot._journal_store.close()
        self.gate = asyncio.Event()
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
        """Yield until no task is runnable; with blocking work inline, every waiter then awaits a test gate."""
        loop = asyncio.get_running_loop()
        for _ in range(_IDLE_ROUNDS):
            await asyncio.sleep(0)
            if not loop._ready:  # type: ignore[attr-defined]
                return
        msg = "Completion fuzz step never became idle"
        raise AssertionError(msg)

    async def check(self) -> None:
        """Compare replies, consumption, and Stops with what the runner and runtime saved."""
        model = self.model
        for source, replies in model.replies.items():
            # A re-run reuses the placeholder an interrupted attempt showed.
            assert len(replies) <= 1, (source, replies)
        for source, runs in model.runs.items():
            # Only a process loss during a reply may run it again.
            assert runs <= 1 + model.interrupted[source], (source, runs, model.interrupted[source])
        for job_id, source in model.consumers.items():
            job = await self.runtime.lookup(job_id, owner=self.owner, depth=0)
            assert job.consumed_by_source == source, (job_id, job.consumed_by_source, source)
        for stop in [stop for stop in self.stops if stop.task.done()]:
            self.stops.remove(stop)
            # Stop always finds the reply's durable owner, whether it is still running or already finished.
            assert stop.task.result(), stop.reply
            record = self.bot._turn_store.turn_record_for_response_event_id(stop.reply)
            assert record is not None, stop.reply
            assert record.user_stop_receipt_order is not None, stop.reply
            assert record.user_stop_receipt_order >= stop.order, stop.reply
            model.stops[stop.reply] = stop.order
            for child in stop.outstanding:
                # Stop reaches outstanding work the reply started, which then never wakes the conversation.
                job = await self.runtime.lookup(child, owner=self.owner, depth=0)
                assert job.user_stop_receipt_order is not None, (stop.reply, child)


async def _deliver_final(store: PrincipalStore, source: str, reply: str) -> None:
    await store.enqueue_matrix_delivery(
        delivery_id=source,
        stage=DeliveryStage.FINAL,
        room_id=ROOM,
        thread_id="$thread",
        payload={"body": "Answer"},
        response_attempt=ResponseAttempt("general", ResponseSources((source,), (source,))),
        edits_event_id=reply,
        settle_source_event_ids=(source,),
    )
    await store.claim_matrix_delivery(delivery_id=source, stage=DeliveryStage.FINAL)
    await store.acknowledge_matrix_delivery(
        delivery_id=source,
        stage=DeliveryStage.FINAL,
        event_id=f"{reply}-final",
        delivered_projections=(),
    )


async def _inline[Result](function: Callable[..., Result], /, *args: object, **kwargs: object) -> Result:
    return function(*args, **kwargs)


def _inline_submit[Result](_offload: ThreadOffload, call: Callable[[], Result]) -> asyncio.Future[Result]:
    work = asyncio.get_running_loop().create_future()
    try:
        work.set_result(call())
    except Exception as error:
        work.set_exception(error)
    return work
