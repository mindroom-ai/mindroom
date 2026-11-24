"""The workflow engine: a run's attempts, its tool nodes and their dispatch handles, and the run's state."""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections import Counter
from contextlib import suppress
from functools import partial
from typing import TYPE_CHECKING, Any, Literal, cast

from mindroom.background_tasks import (
    run_blocking_until_complete,
    run_coroutine_until_complete,
    wait_for_future_until_complete,
)
from mindroom.delegation.personas import persona_allows
from mindroom.logging_config import get_logger
from mindroom.script_runs.manager import ScriptRunLimits, ScriptRuntimeNotReadyError
from mindroom.script_runs.models import (
    TERMINAL_SCRIPT_RUN_STATES,
    utc_timestamp,
)
from mindroom.script_runs.store import ScriptRunNotFoundError
from mindroom.workflow_runs.agent_dispatch import (
    OWNER_NOT_READY,
    AgentDispatcher,
)
from mindroom.workflow_runs.coordination import ADMISSIONS, RunCoordinator, wait_for_change
from mindroom.workflow_runs.dispatch import NodeDispatcher
from mindroom.workflow_runs.endings import RunEndings, run_outcome
from mindroom.workflow_runs.interactions import RunInteractions
from mindroom.workflow_runs.limits import (
    WorkflowLimits,
    ceiling,
    limit_reached,
    run_usage,
    usd,
)
from mindroom.workflow_runs.models import (
    RECHECK_SECONDS,
    TERMINAL_RUN_STATES,
    DispatchHandle,
    WorkflowNode,
    WorkflowNodeState,
    WorkflowRun,
    WorkflowRunState,
)
from mindroom.workflow_runs.recovery import AUTO_RESUME_REASONS, NO_ACCESS
from mindroom.workflow_runs.script_calls import ScriptCalls
from mindroom.workflow_runs.store import SupersededAttemptError, WorkflowRunNotFoundError, WorkflowRunStoreError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence

    from mindroom.script_runs.broker import ScriptToolBroker
    from mindroom.script_runs.manager import ScriptResourceProfileName, ScriptRunManager
    from mindroom.script_runs.models import ScriptRunRecord, ScriptToolGrant
    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from mindroom.workflow_runs.agent_nodes import AgentRuntime
    from mindroom.workflow_runs.store import WorkflowRunStore

logger = get_logger(__name__)

# How many failed calls the first line of a report names; `get_workflow` lists them all.
_MAX_FAILED_NAMED = 10
_ABANDONED = "The call that waited was cancelled."
_TAKEN = "Run {} was resumed by another call."
_IDLE = "Stopped resuming after {attempts} attempts that finished nothing. Fix the script and call resume_workflow."
# How many attempts in a row an interrupted run is given on its own while none of them finishes a call.
_MAX_IDLE_RESUMES = 5
_OUT_OF_TIME = "The run used its {hours:g} hours of working time. resume_workflow continues it."
# How often a running run's working time is saved, so a primary that dies loses at most this much of the count.
_CHARGE_SECONDS = 60.0
# How long after its report a script's exit is looked for, before the script runtime's own pass finds it.
_EXIT_POLL_SECONDS = (0.05, 0.2, 0.5, 1.0, 2.0, 5.0)


# What the saved owner of a run has now in the run's conversation: its live context once access is confirmed,
# "pending" while it cannot be checked yet, "revoked" when it is gone.
type ResumeAccess = ToolRuntimeContext | Literal["pending", "revoked"]


class WorkflowEngine:
    """Run workflow attempts on the script runtime and their nodes through its broker."""

    def __init__(
        self,
        store: WorkflowRunStore,
        scripts: ScriptRunManager,
        broker: ScriptToolBroker,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.store = store
        self.scripts = scripts
        self.broker = broker
        self.live = RunCoordinator(store, scripts, clock, self.cancel_as_task)
        self.ask = RunInteractions(store, self.live, self.authorize, lambda: self.agents())
        self.agent_dispatch = AgentDispatcher(store, self.live, self.authorize, lambda: self.agents())
        self.dispatch = NodeDispatcher(
            store,
            scripts,
            broker,
            self.live,
            self.agent_dispatch,
            agents=lambda: self.agents(),
            close_card=self.ask.close_card,
            cancel=lambda *a, **kw: self.cancel(*a, **kw),
        )
        # What the saved owner of a run may start an attempt with now; set by the runtime that knows the live bots.
        self.owner_access: Callable[[WorkflowRun], ResumeAccess] = lambda _run: "pending"
        # Live workflow limits for an authorized owner context; read only where limits are used.
        self.owner_limits: Callable[[ToolRuntimeContext, str], WorkflowLimits] = lambda _context, _agent: (
            WorkflowLimits()
        )
        # What agent turns run on, None while there is no job runtime; set by the runtime that knows it.
        self.agents: Callable[[], AgentRuntime | None] = lambda: None
        # The bot that serves a run, which posts its cards and acts on the events people send to them: its agent's,
        # or the bot its agent answers through. None while that bot is not running; set by the same runtime.
        self.served_by: Callable[[WorkflowRun], str | None] = lambda run: run.agent_name
        # The attempts this engine started, each with the epoch its run got when it began: all it may ever write
        # with. No other attempt is served: one an earlier primary left is a leftover process.
        self._launched: dict[str, int] = {}
        # The runs whose wait for an attempt was logged as refused, so the tries that follow say nothing.
        self._refused: set[str] = set()
        self._attending: Counter[str | None] = Counter()
        self._inline_runs: dict[str, str] = {}
        self.endings = RunEndings(
            store,
            self.live,
            self.dispatch,
            launched=self._launched,
            refused=self._refused,
            agents=lambda: self.agents(),
            cancel=lambda *a, **kw: self.cancel(*a, **kw),
        )
        self.calls = ScriptCalls(
            store,
            scripts,
            self.live,
            self.ask,
            self.agent_dispatch,
            self.dispatch,
            launched=self._launched,
            authorize=self.authorize,
            agents=lambda: self.agents(),
            observe_exit=self._observe_exit,
        )

    def authorize(self, run: WorkflowRun, grant: ScriptToolGrant | None = None) -> ToolRuntimeContext | str | None:
        """Say whether *run* may act now: its owner's live context, why not, or None while that cannot be said yet.

        The one check before an attempt starts and before any call of the run is made, a tool's, an agent's or
        one that starts or resumes a run inside. The run's saved ceiling must keep the tool *grant* names, so a run
        never has more than the turn that started or resumed it. Its owner must have its access as the live runtime
        answers it now (`owner_access`): the agent the run is named for still has the toolkit and may still answer
        the requester, and the bot that serves the run is still in the run's room.
        """
        kept = cast("list[str] | None", (run.persona or {}).get("tools"))
        tool = (grant.toolkit_name, grant.function_name) if grant is not None and kept is not None else None
        if tool is not None and not persona_allows(tuple(kept or ()), *tool):
            return f"{tool[0]}.{tool[1]} is not a tool of the agent that started this run."
        access = self.owner_access(run)
        if access is None or access == "pending":
            return None
        return NO_ACCESS if access == "revoked" else access

    def admit(
        self,
        run: WorkflowRun,
        caller: ToolRuntimeContext | None = None,
        limits: WorkflowLimits | None = None,
    ) -> tuple[ToolRuntimeContext, WorkflowLimits] | str | None:
        """Say what the next attempt of *run* starts with now, why it gets none, or None while that cannot be said yet.

        The one check of a start, a person's resume and a replay after an interruption: asked before a resume
        changes anything, and again under the run's lock right before any attempt starts. Every attempt runs in the
        conversation the run was started in, and needs the saved owner's access there as the live runtime answers it
        now: the serving bot synced and in the room, the requester allowed, the agent with its `workflow` toolkit. A
        first attempt and a resume that *caller* makes from there then start with the caller's own context and
        *limits*. A run that waits has working time left, and has not replayed too often without finishing a call.
        """
        replay = run.state is WorkflowRunState.STARTING and run.script_run_id is not None
        place = (run.room_id, run.thread_root_event_id)
        context = self.authorize(run)
        if context is None or isinstance(context, str):
            return context
        if caller is not None and not replay and place == (caller.room_id, caller.resolved_thread_id):
            context = caller
            resolved_limits = limits or self.owner_limits(context, run.agent_name)
        else:
            resolved_limits = self.owner_limits(context, run.agent_name)
        if run.state is WorkflowRunState.STARTING and run.runtime_seconds_left == 0:
            return _OUT_OF_TIME.format(hours=resolved_limits.max_runtime_hours)
        if replay and run.idle_resumes >= _MAX_IDLE_RESUMES:
            return _IDLE.format(attempts=_MAX_IDLE_RESUMES)
        return context, resolved_limits

    async def launch(
        self,
        run_id: str,
        caller: ToolRuntimeContext | None = None,
        limits: WorkflowLimits | None = None,
        *,
        owner: str | None = None,
        epoch: int | None = None,
    ) -> WorkflowRun:
        """Start the attempt a run waits for, if `admit` allows one now; a refusal ends the run with the reason.

        Whoever launches names the *owner* it launches for, and the pass the *epoch* it judged the run at: a run
        that another invocation took since is left to that one.

        The engine owns a launch once it began: a caller that goes away leaves no process its run does not name, and
        only the engine's stop ends a launch, which leaves the run starting for the next primary. The run also stays
        as it is, for whoever launches it to try again, while `admit` cannot answer, when the script runtime refuses
        a replay, and when a script runtime that is still starting refuses an attempt of a job's run.
        """
        if self.live.closed:
            return self.store.get_run(run_id)
        launch = self.live.spawn(ADMISSIONS, self._launch(run_id, caller, limits, owner, epoch))
        try:
            return await asyncio.shield(launch)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if not launch.cancelled() or (current is not None and current.cancelling()):
                raise
            # The stopping engine ended the launch; the run stays as it is, for the next primary to start.
            return self.store.get_run(run_id)

    async def _launch(
        self,
        run_id: str,
        caller: ToolRuntimeContext | None,
        limits: WorkflowLimits | None,
        owner: str | None,
        epoch: int | None,
    ) -> WorkflowRun:
        # Held until the run names the attempt: the new process's first request waits instead of being refused, and
        # a stop finds either no attempt or the one it has to end.
        async with self.live.condition(run_id):
            run = self.store.get_run(run_id)
            # A reserved run gets one attempt from one launch; a run that was launched or has ended gets none, a
            # stopping engine starts none, and neither does a launcher whose run moved on to another owner.
            moved = owner not in (None, run.owner) or epoch not in (None, run.epoch)
            waits = run.state is WorkflowRunState.STARTING and not self.live.closed and not moved
            admitted = self.admit(run, caller, limits) if waits else None
            if admitted is None:
                return run
            # An attempt that follows one an interruption ended counts toward the idle limit; a first attempt, or
            # one a person's resume asked for, follows none.
            state, refusal, replay = WorkflowRunState.INTERRUPTED, admitted, run.script_run_id is not None
            if isinstance(admitted, tuple):
                context, limits = admitted
                try:
                    attempt = await self.scripts.run(
                        context,
                        source=run.source,
                        workflow_run_id=run_id,
                        resource_profile=cast("ScriptResourceProfileName | None", run.resource_profile),
                        limits=ScriptRunLimits(max_tool_calls_per_minute=limits.max_tool_calls_per_minute),
                    )
                except Exception as exc:
                    if run_id not in self._refused:
                        logger.warning("workflow_launch_refused", run_id=run_id, error=str(exc))
                    self._refused.add(run_id)
                    if replay or (run.job_id is not None and isinstance(exc, ScriptRuntimeNotReadyError)):
                        return run
                    state, refusal = WorkflowRunState.FAILED, str(exc)
                else:
                    return await self._begin(run, attempt, limits.max_runtime_hours)
        # The gate is free again, which ending a run takes.
        return await self.endings.end_run(run, state, cast("str", refusal))

    async def _begin(self, run: WorkflowRun, attempt: ScriptRunRecord, hours: float) -> WorkflowRun:
        """Name the started *attempt* as the next one of *run*; the caller holds the run's lock."""
        # Before the run reads `running`, so whoever sees that state sees its time counted from here.
        self.live.charged[run.run_id] = self.live.clock()
        begin = partial(
            self.store.update_run,
            run.run_id,
            run.epoch,
            when={WorkflowRunState.STARTING},
            state=WorkflowRunState.RUNNING,
            attempt=run.attempt + 1,
            script_run_id=attempt.run_id,
            idle_resumes=run.idle_resumes + (run.script_run_id is not None),
        )
        try:
            launched = await asyncio.to_thread(begin)
        except SupersededAttemptError:
            # The run moved on while its process started, so nothing names this attempt: stop it.
            launched = self.store.get_run(run.run_id)
            await self.scripts.revoke(attempt.run_id, reason=launched.error or launched.state.value)
            await self.scripts.reconcile_durable(run_id=attempt.run_id)
            self.live.charged.pop(run.run_id, None)
        else:
            self._launched[attempt.run_id] = launched.epoch
            self.live.spawn(run.run_id, self._watch_time(run.run_id, launched.epoch, hours))
        return launched

    async def cancel(
        self,
        run_id: str,
        *,
        reason: str,
        state: WorkflowRunState = WorkflowRunState.CANCELLED,
        owner: str | None = None,
        epoch: int | None = None,
    ) -> WorkflowRun:
        """Revoke the run's attempt, end its process and end the run in *state*; completed nodes stay saved.

        An attempt that ended before the stop ends the run as it did. With *owner*, only a run that is still that
        invocation's is stopped, and with *epoch* only that attempt: a run that a resume took since is left. A stop
        wins against a launch: when the run got an attempt before the stop's write, that attempt is stopped too.
        """
        async with self.live.condition(run_id):
            run = self.store.get_run(run_id)
        while run.state not in TERMINAL_RUN_STATES and owner in (None, run.owner) and epoch in (None, run.epoch):
            outcome: tuple[WorkflowRunState, str | None] = (state, reason)
            # Before anything of the run is stopped: the place a stopped call gives up starts no queued one.
            self.live.places.pop(run_id, None)
            # A run that waits for its next attempt has no process: how its last one ended says nothing about this stop.
            if run.script_run_id is not None and run.state is not WorkflowRunState.STARTING:
                await self.scripts.revoke(run.script_run_id, reason=reason)
                attempt = await self.scripts.reconcile_durable(run_id=run.script_run_id)
                if attempt.state in TERMINAL_SCRIPT_RUN_STATES:
                    # The same rule as the maintenance pass: an attempt that ended before the stop ends as it did.
                    outcome = run_outcome(self.store.get_run(run_id), attempt, stopped=state)
            run = await self.endings.end_run(run, *outcome)
        return run

    def cancel_as_task(self, run_id: str, **how: Any) -> asyncio.Task[WorkflowRun]:  # noqa: ANN401 - as `cancel`
        """Stop a run as the engine's own task, for a caller with less time than the end of a run may take."""

        def failed(ended: asyncio.Task[WorkflowRun]) -> None:
            error = None if ended.cancelled() else ended.exception()
            if error is not None and not isinstance(error, WorkflowRunNotFoundError):
                logger.error("workflow_run_stop_failed", run_id=run_id, exc_info=error)

        task = self.live.spawn(run_id, self.cancel(run_id, **how))
        task.add_done_callback(failed)
        return task

    def job_stop(self, run: WorkflowRun, reason: str) -> dict[str, Any]:
        """Return how the run of a stopped job ends: with *reason*, or interrupted for the access its owner lost."""
        if self.owner_access(run) == "revoked":
            return {"state": WorkflowRunState.INTERRUPTED, "reason": NO_ACCESS}
        return {"reason": reason}

    def resumable(
        self,
        run_id: str,
        caller: ToolRuntimeContext,
        rerun: Sequence[str] = (),
        limits: WorkflowLimits | None = None,
        *,
        max_cost_usd: float | None = None,
    ) -> frozenset[str]:
        """Check a resume before anything changes, and return the keys of the calls *rerun* names.

        A live run is refused, an ID that names no call of the run is refused by name, a run that already reached its
        cost limit requires a higher `max_cost_usd`, and a resume that `admit` gives no attempt now is refused.
        """
        run = self.store.get_run(run_id)
        if run.state not in TERMINAL_RUN_STATES:
            msg = f"Run {run_id} is still running. Cancel it first."
            raise WorkflowRunStoreError(msg)
        effective_limit = run.max_cost_usd if max_cost_usd is None else max_cost_usd
        if effective_limit is not None:
            spent = run_usage(self.store, run_id)["cost_usd"]
            if limit_reached(spent, effective_limit):
                msg = (
                    f"Run {run_id} has already spent ${usd(spent)} (limit ${usd(effective_limit)}). "
                    f"Pass max_cost_usd above ${usd(spent)} to resume it."
                )
                raise WorkflowRunStoreError(msg)
        try:
            keys = frozenset(self.calls.node_by_id(run_id, str(node_id)).key for node_id in rerun)
        except LookupError as exc:
            raise WorkflowRunStoreError(str(exc)) from exc
        if not isinstance(admitted := self.admit(run, caller, limits), tuple):
            raise WorkflowRunStoreError(admitted or OWNER_NOT_READY)
        return keys

    async def reopen(
        self,
        run_id: str,
        context: ToolRuntimeContext,
        *,
        source: str | None = None,
        rerun: Sequence[str] = (),
        max_cost_usd: float | None = None,
        limits: WorkflowLimits | None = None,
        owner: str | None = None,
        job_id: str | None = None,
        epoch: int | None = None,
        persona: Mapping[str, object] | None = None,
    ) -> WorkflowRun:
        """Make an ended run ready for its next attempt, with the full working time; `launch` then starts it.

        The saved calls stay: the next attempt replays the script, with *source* when given, and reuses them, except
        the calls *rerun* names.

        Nothing changes before the resume takes the run (`resumable`), and it takes it in one write: the run becomes
        *owner*'s, the invocation that resumes it, with *job_id* when that is a job, names no attempt, and the calls
        of *rerun* are marked. A run that is *owner*'s already was taken by this same invocation, delivered again,
        and is returned as it is. *epoch* is the one the run had when the resume was accepted: a run that another
        resume took since, also one whose attempt never started, is refused, as is one over its owner's limit of
        live runs. A job was accepted with its request, so it waits here while `admit` cannot answer yet. *persona*
        is that of the turn the resume was accepted from: the run keeps what it, *context* and the run all keep.
        """
        run = self.store.get_run(run_id)
        if owner is not None and run.owner == owner:
            return run
        limits = limits or self.owner_limits(context, run.agent_name)
        while job_id is not None and self.admit(run, context, limits) is None:  # noqa: ASYNC110 - access has no event
            await asyncio.sleep(RECHECK_SECONDS)
        text = run.source if source is None else source
        take = partial(
            self.store.update_run,
            run_id,
            run.epoch if epoch is None else epoch,
            when=TERMINAL_RUN_STATES,
            rerun=self.resumable(run_id, context, rerun, limits, max_cost_usd=max_cost_usd),
            max_active=limits.max_concurrent_runs,
            state=WorkflowRunState.STARTING,
            source=text,
            source_digest=hashlib.sha256(text.encode()).hexdigest(),
            persona=ceiling(context, persona, run.persona),
            previous_source_digest=run.source_digest,
            max_cost_usd=run.max_cost_usd if max_cost_usd is None else max_cost_usd,
            runtime_seconds_left=limits.max_runtime_hours * 3600 if limits.max_runtime_hours else None,
            idle_resumes=0,
            owner=owner,
            job_id=job_id,
            script_run_id=None,
            result=None,
            error=None,
            finished_at=None,
        )
        try:
            async with self.live.condition(run_id):
                # Waited for also by a caller that is cancelled here, so the run it finds says whether it took it.
                reopened = await run_blocking_until_complete(take)
        except SupersededAttemptError:
            raise WorkflowRunStoreError(_TAKEN.format(run_id)) from None
        return reopened

    async def wait(
        self,
        run_id: str,
        launch: Callable[[], Awaitable[WorkflowRun]] | None = None,
        owner: str | None = None,
    ) -> WorkflowRun:
        """Return the run once it has ended.

        With *launch*, the waiter is the invocation that owns the run, *owner*: a job's operation, or a call that
        waits itself. It alone starts the run's attempts while it waits: whenever the run waits for one it calls
        *launch*, again after a moment when that started none. A run that another invocation took is not waited
        for: its report is that one's.
        """
        condition = self.live.condition(run_id)
        while True:
            async with condition:
                while True:
                    run = self.store.get_run(run_id)
                    if owner not in (None, run.owner):
                        raise WorkflowRunStoreError(_TAKEN.format(run_id))
                    if run.state in TERMINAL_RUN_STATES or (launch and run.state is WorkflowRunState.STARTING):
                        break
                    # The saved run decides: a wake only shortens the wait, since a writer cancelled at its commit
                    # sends none.
                    await wait_for_change(condition)
            if launch is None or run.state in TERMINAL_RUN_STATES:
                return run
            if (await launch()).state is WorkflowRunState.STARTING:
                await asyncio.sleep(RECHECK_SECONDS)

    async def attend(
        self,
        origin: str,
        reserve: Callable[[], Awaitable[WorkflowRun]],
        context: ToolRuntimeContext,
        limits: WorkflowLimits | None = None,
    ) -> WorkflowRun:
        """Reserve, launch and wait for a run that is no job, as one of the calls of *origin* that wait for it.

        A cancelled call only stops waiting, and its reservation still ends. Whenever a call leaves, however it ends,
        and no other call of *origin* is left while MindRoom is not stopping, nobody can receive the report, so the
        engine cancels the run. A call whose reservation is refused holds the run only until that refusal. *origin*
        is the key of the call, the owner its reservation gave the run: a run that is another invocation's by now is
        neither waited for nor cancelled.
        """
        self._attending[origin] += 1
        reserving = asyncio.ensure_future(reserve())
        try:
            run = await wait_for_future_until_complete(reserving)
            # The call starts the run's attempt as a job's operation does, again while `admit` cannot answer yet.
            return await self.wait(run.run_id, partial(self.launch, run.run_id, context, limits, owner=origin), origin)
        finally:
            if not reserving.cancelled() and not reserving.exception():
                self._inline_runs[origin] = reserving.result().run_id
            self._attending -= Counter({origin: 1})
            stopping = self.live.closed or context.orchestrator is None or not context.orchestrator.running
            if not self._attending[origin] and (run_id := self._inline_runs.pop(origin, None)) and not stopping:
                # A run that ended is left as it ended.
                await run_coroutine_until_complete(self.cancel(run_id, reason=_ABANDONED, owner=origin))

    def attended(self, owner: str | None) -> bool:
        """Return whether a call of this primary waits for the run it owns as *owner*, and so starts its attempts."""
        return self._attending[owner] > 0

    async def settle(self, run: WorkflowRun, *, replay: bool = False) -> WorkflowRun:
        """End the attempt of *run* if its process has ended, and return the run as that leaves it.

        The run ends as the attempt and the script's report say. With *replay*, a run that an infrastructure
        interruption stopped waits for its next attempt instead, its working time charged up to here and no longer.
        """
        if run.state is WorkflowRunState.STARTING:
            return run
        try:
            attempt = await asyncio.to_thread(self.scripts.store.get_run, run.script_run_id or "")
        except ScriptRunNotFoundError:
            attempt = None
        if attempt is not None and attempt.state not in TERMINAL_SCRIPT_RUN_STATES:
            return run
        state, error = run_outcome(self.store.get_run(run.run_id), attempt)
        if replay and state is WorkflowRunState.INTERRUPTED and error in AUTO_RESUME_REASONS:
            # The attempt is over and the run is not: its nodes are settled, and the next attempt runs the same script.
            same_script = {"previous_source_digest": run.source_digest}
            return await self.endings.end_run(run, WorkflowRunState.STARTING, None, finished_at=None, **same_script)
        return await self.endings.end_run(run, state, error)

    async def close(self) -> None:
        """Stop the engine's own tasks and start no more; what was starting or unsettled stays for the next start."""
        self.live.closed = True
        tasks = [task for run_tasks in self.live.tasks.values() for task in run_tasks]
        self.live.tasks.clear()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def nest(self, child: WorkflowRun) -> None:
        """Show the nested run *child* in the run it is in, as a group under the call that started it.

        The group names the run, so the end of the outer run ends it, and it is part of the attempt it began in.
        """
        outer = self.store.get_run(str(child.parent_run_id))
        key = f"{child.parent_node_key}/{child.run_id}"
        existing = self.store.get_node(outer.run_id, key)
        group = WorkflowNode(
            run_id=outer.run_id,
            key=key,
            kind="group",
            lane=str(child.parent_node_key),
            idx=0,
            digest=child.source_digest,
            request={"op": "workflow"},
            generation=1 if existing is None else existing.generation + 1,
            state=WorkflowNodeState.RUNNING,
            handle=DispatchHandle("run", child.run_id, dispatched=True),
            attempt=outer.attempt,
            label=child.title or "workflow",
            started_at=utc_timestamp(),
            links={"child_run_id": child.run_id},
        )
        with suppress(SupersededAttemptError):
            await asyncio.to_thread(self.store.put_node, group, outer.epoch)

    async def delivery(self, run: WorkflowRun) -> str:
        """Return what an ended run delivers to whoever started it: its report, or why it did not finish.

        A run that completed although calls of it failed, which its script caught, says so in a first line.
        """
        text = run.outcome_text()
        if run.state is not WorkflowRunState.COMPLETED:
            return text
        nodes = await asyncio.to_thread(self.store.nodes, run.run_id, light=True)
        failed = [
            f"n{node.seq}"
            for node in nodes
            if node.state in (WorkflowNodeState.FAILED, WorkflowNodeState.INDETERMINATE)
        ]
        if not failed:
            return text
        named = ", ".join(failed[:_MAX_FAILED_NAMED]) + (", ..." if len(failed) > _MAX_FAILED_NAMED else "")
        calls = "call" if len(failed) == 1 else "calls"
        return f'{len(failed)} {calls} failed ({named}); get_workflow(run_id, nodes="failed") shows them.\n\n{text}'

    async def _watch_time(self, run_id: str, epoch: int, hours: float) -> None:
        """Save the working time the attempt that began with *epoch* uses, and stop it when none is left."""
        left = self.store.get_run(run_id).runtime_seconds_left
        with suppress(SupersededAttemptError):
            while left is not None and left > 0:
                await asyncio.sleep(min(_CHARGE_SECONDS, left))
                # Under the run's lock, as a change of the run's state charges it: the time left is read and written
                # by one of them at a time, so neither takes the same time off twice or writes back an older time.
                async with self.live.condition(run_id):
                    run = self.store.get_run(run_id)
                    if charge := self.live.time_left(run):
                        run = await asyncio.to_thread(self.store.update_run, run_id, epoch, **charge)
                left = run.runtime_seconds_left
            if left is not None:
                # An attempt that had ended already is judged as the pass judges it, before the run is stopped.
                await self.settle(self.store.get_run(run_id), replay=True)
                out_of_time = _OUT_OF_TIME.format(hours=hours)
                await self.cancel(run_id, reason=out_of_time, state=WorkflowRunState.INTERRUPTED, epoch=epoch)

    async def _observe_exit(self, run_id: str, script_run_id: str) -> None:
        """End the run soon after its script reported: look for the exit instead of waiting for the next pass."""
        for delay in _EXIT_POLL_SECONDS:
            await asyncio.sleep(delay)
            if (await self.scripts.reconcile_durable(run_id=script_run_id)).state in TERMINAL_SCRIPT_RUN_STATES:
                # Only the run that reported: another run's ended attempt is the next pass's to judge.
                with suppress(WorkflowRunNotFoundError):
                    run = await asyncio.to_thread(self.store.get_run, run_id)
                    if run.script_run_id == script_run_id:
                        await self.settle(run)
                break
