"""The workflow engine: a run's attempts, its tool nodes and their dispatch handles, and the run's state."""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import time
from collections import Counter
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Any, Literal, cast

from mindroom.background_tasks import (
    run_blocking_until_complete,
    run_coroutine_until_complete,
    wait_for_future_until_complete,
)
from mindroom.delegation.personas import persona_allows
from mindroom.delegation.state import RUN_SUBAGENT, SubagentPersona
from mindroom.logging_config import get_logger
from mindroom.script_runs.broker import ScriptRuntimeUnavailableError
from mindroom.script_runs.manager import ScriptRunLimits, ScriptRuntimeNotReadyError
from mindroom.script_runs.models import (
    TERMINAL_SCRIPT_RUN_STATES,
    ScriptToolGrant,
    utc_timestamp,
)
from mindroom.script_runs.store import ScriptRunNotFoundError
from mindroom.workflow_runs.agent_dispatch import (
    OWNER_NOT_READY,
    AgentDispatcher,
)
from mindroom.workflow_runs.agent_nodes import (
    delegated_request,
    load_follow_up_child,
    schema_failure,
)
from mindroom.workflow_runs.coordination import RunCoordinator, receipt, wait_for_change
from mindroom.workflow_runs.dispatch import NodeDispatcher
from mindroom.workflow_runs.endings import RunEndings, run_outcome
from mindroom.workflow_runs.graph import node_label
from mindroom.workflow_runs.interactions import RunInteractions
from mindroom.workflow_runs.limits import (
    WorkflowLimits,
    ceiling,
    fits_ceiling,
    limit_reached,
    run_usage,
    usd,
)
from mindroom.workflow_runs.models import (
    LIVE_NODE_STATES,
    RECHECK_SECONDS,
    SERVED_RUN_STATES,
    TERMINAL_RUN_STATES,
    DispatchHandle,
    NodeReceipt,
    WorkflowNode,
    WorkflowNodeState,
    WorkflowRun,
    WorkflowRunState,
)
from mindroom.workflow_runs.peers import (
    script_op,
    session_turn_message,
    withdraw_unread,
)
from mindroom.workflow_runs.recovery import AUTO_RESUME_REASONS, NO_ACCESS
from mindroom.workflow_runs.replay import (
    DIRTY_DIGEST,
    decide,
    node_digest,
    result_digest,
    split_key,
)
from mindroom.workflow_runs.saved import WORKFLOWS_DIR, validate_workflow_name
from mindroom.workflow_runs.store import SupersededAttemptError, WorkflowRunNotFoundError, WorkflowRunStoreError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence

    from mindroom.delegation.state import DelegationChild
    from mindroom.script_runs.broker import ScriptToolBroker
    from mindroom.script_runs.manager import ScriptResourceProfileName, ScriptRunManager
    from mindroom.script_runs.models import ScriptRunRecord
    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from mindroom.workflow_runs.agent_nodes import AgentRuntime
    from mindroom.workflow_runs.models import NodeRequest
    from mindroom.workflow_runs.store import WorkflowRunStore

logger = get_logger(__name__)

# How many failed calls the first line of a report names; `get_workflow` lists them all.
_MAX_FAILED_NAMED = 10
# The owner of the tasks that admit calls: they outlive their requests, and a run's end waits for them, not stops them.
_ADMISSIONS = "admissions"
_ABANDONED = "The call that waited was cancelled."
_CHANGED_WITHOUT_EDIT = (
    "Call n{seq} changed between attempts without a script edit. It probably contains the time or a random value; "
    "use now() or random() from mindroom.workflow."
)
_TAKEN = "Run {} was resumed by another call."
_IDLE = "Stopped resuming after {attempts} attempts that finished nothing. Fix the script and call resume_workflow."
# How many attempts in a row an interrupted run is given on its own while none of them finishes a call.
_MAX_IDLE_RESUMES = 5
_OUT_OF_TIME = "The run used its {hours:g} hours of working time. resume_workflow continues it."
# How often a running run's working time is saved, so a primary that dies loses at most this much of the count.
_CHARGE_SECONDS = 60.0
_NO_SESSIONS = 'A workflow script cannot call delegate.{function} yet. Start a fresh agent with agent() or tool("delegate", "run_subagent", ...).'
_SESSION_BUSY = "Session '{name}' is already in a turn. Wait for it or spawn another agent."
_STOPPING = "The agent this call replaces is stopping. Try again in a moment."
# How long after its report a script's exit is looked for, before the script runtime's own pass finds it.
_EXIT_POLL_SECONDS = (0.05, 0.2, 0.5, 1.0, 2.0, 5.0)


@dataclass(frozen=True, slots=True)
class _TurnContext:
    """Resolved session, live sibling turns, earlier turns, last completed turn, and digest for one `turn` call."""

    session: WorkflowNode
    in_attempt: list[WorkflowNode]
    earlier: list[WorkflowNode]
    prev: WorkflowNode | None
    digest: str


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
        launch = self.live.spawn(_ADMISSIONS, self._launch(run_id, caller, limits, owner, epoch))
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
            keys = frozenset(self._node(run_id, str(node_id)).key for node_id in rerun)
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

    async def run_info(self, attempt: ScriptRunRecord) -> dict[str, object]:
        """Return what the attempt's script starts from."""
        run = await self._current_run(attempt)
        return {"run_id": run.run_id, "attempt": run.attempt, "args": run.args}

    async def resolve_source(self, context: ToolRuntimeContext, arguments: Mapping[str, Any]) -> bytes:
        """Return the script a start names: inline source, workspace file, or saved workflow."""
        source, path, name = arguments.get("source"), arguments.get("path"), arguments.get("name")
        if sum(value is not None for value in (source, path, name)) != 1:
            msg = "Give exactly one of source, path, or name."
            raise WorkflowRunStoreError(msg)
        if name is not None:
            try:
                validate_workflow_name(str(name))
            except ValueError as exc:
                raise WorkflowRunStoreError(str(exc)) from exc
            path = f"{WORKFLOWS_DIR}/{name}.py"
        return await self.scripts.resolve_source(
            context,
            source=source,
            path=path,
            create_workspace=name is None,
        )

    async def saved_source(self, attempt: ScriptRunRecord, name: str) -> tuple[bytes, str]:
        """Return the saved workflow bytes and SHA-256 for *name*, snapshotted once per attempt."""
        run = await self._current_run(attempt)
        try:
            validate_workflow_name(name)
        except ValueError as exc:
            raise WorkflowRunStoreError(str(exc)) from exc
        if (existing := await asyncio.to_thread(self.store.get_source, run.run_id, run.attempt, name)) is not None:
            return existing
        context = self.authorize(run)
        if context is None:
            raise ScriptRuntimeUnavailableError(OWNER_NOT_READY)
        if isinstance(context, str):
            raise WorkflowRunStoreError(context)
        try:
            raw = await self.resolve_source(context, {"name": name})
        except ValueError as exc:
            missing = isinstance(exc.__cause__, FileNotFoundError)
            if missing:
                msg = f"Saved workflow {name!r} not found."
                raise LookupError(msg) from exc
            raise WorkflowRunStoreError(str(exc)) from exc
        sha256 = hashlib.sha256(raw).hexdigest()
        return await asyncio.to_thread(self.store.put_source, run.run_id, run.attempt, run.epoch, name, raw, sha256)

    async def submit_node(self, attempt: ScriptRunRecord, request: NodeRequest) -> NodeReceipt:
        """Save and dispatch one call of the attempt's script, or return the receipt of the node its key has."""
        run = await self._current_run(attempt)
        # The engine owns a call from its insert to its dispatch, so a caller that goes away strands no running node.
        return await asyncio.shield(self.live.spawn(_ADMISSIONS, self._admit(run, attempt, request)))

    async def _admit(self, asked: WorkflowRun, attempt: ScriptRunRecord, request: NodeRequest) -> NodeReceipt:
        # Held from the lookup to the dispatch: two requests with one key cannot both find it free, and the run
        # cannot end in between.
        async with (condition := self.live.condition(asked.run_id)):
            receipt = await self._admit_gated(self.store.get_run(asked.run_id), asked.epoch, attempt, request)
            condition.notify_all()
            return receipt

    async def _admit_gated(
        self,
        run: WorkflowRun,
        epoch: int,
        attempt: ScriptRunRecord,
        request: NodeRequest,
    ) -> NodeReceipt:
        """Decide and save one call for the attempt that began with *epoch*, which every write here names."""
        if run.epoch != epoch or run.state not in SERVED_RUN_STATES or run.run_id in self.live.stopping:
            # Before anything is kept for the attempt: one that the run left behind leaves nothing behind here.
            msg = f"Run {run.run_id} takes no more calls from attempt {attempt.run_id}."
            raise SupersededAttemptError(msg)
        turn_ctx = self._turn_context(run, request.key, request.args) if request.kind == "turn" else None
        digest = node_digest(request.kind, request.args) if turn_ctx is None else turn_ctx.digest
        members = _members(request.meta) if request.kind == "group" else None
        saved, previous, reused = await self._resolve_saved(run, epoch, request, turn_ctx, digest, members)
        if reused is not None:
            return reused
        return await self._dispatch_new(run, epoch, attempt, request, turn_ctx, digest, members, saved, previous)

    async def _resolve_saved(
        self,
        run: WorkflowRun,
        epoch: int,
        request: NodeRequest,
        turn_ctx: _TurnContext | None,
        digest: str,
        members: list[str] | None,
    ) -> tuple[WorkflowNode | None, WorkflowNode | None, NodeReceipt | None]:
        """Apply the replay rules to any saved row at `request.key`; return `(saved, previous, reused_receipt)`."""
        saved = self.store.get_node(run.run_id, request.key)
        if saved is None:
            return None, None, None
        if saved.attempt == run.attempt and not saved.stale:
            return saved, None, receipt(await self.ask.asked(run, saved, epoch))
        live, handle = False, None
        if saved.state in LIVE_NODE_STATES:
            live, handle = await self.dispatch.reconcile(run, saved, epoch), saved.handle
            saved = cast("WorkflowNode", self.store.get_node(run.run_id, request.key))
        cached = request.meta.get("cached") is not False
        action = decide(saved, digest, cached=cached)
        if action != "run":
            return saved, None, await self._reuse_saved(run, epoch, saved, request.meta, members, action)
        if live and saved.kind == "ask":
            await self.ask.close_card(run, saved, "cancelled")
        if live and handle is not None:
            await cast("AgentRuntime", self.agents()).jobs.request_cancel(handle.ref)
            raise ScriptRuntimeUnavailableError(_STOPPING)
        marked = saved.digest in ("", DIRTY_DIGEST)
        previous = saved if saved.state is WorkflowNodeState.COMPLETED and not marked else None
        lineage_reran = turn_ctx is not None and any(not turn.cached for turn in turn_ctx.earlier)
        if (
            previous is not None
            and previous.digest != digest
            and cached
            and not lineage_reran
            and run.source_digest == run.previous_source_digest
        ):
            msg = _CHANGED_WITHOUT_EDIT.format(seq=previous.seq)
            await asyncio.to_thread(self.store.append_log, run.run_id, epoch, msg)
        return saved, previous, None

    async def _reuse_saved(
        self,
        run: WorkflowRun,
        epoch: int,
        saved: WorkflowNode,
        meta: dict[str, object],
        members: list[str] | None,
        action: str,
    ) -> NodeReceipt:
        """Adopt a saved node for this attempt (`hit`, `attach`, or `raise_indeterminate`)."""
        stamp: dict[str, object] = {"attempt": run.attempt, "stale": False, "cached": action == "hit"}
        if (phase := cast("str | None", meta.get("phase"))) != saved.phase:
            stamp["phase"] = phase
        if regrouped := members != saved.members:
            stamp["members"] = members
        await self.live.stamp(saved, epoch, dirty_after=regrouped, **stamp)
        if action == "raise_indeterminate":
            return replace(receipt(saved), state=WorkflowNodeState.FAILED)
        if action == "attach":
            if saved.kind == "ask":
                await self.ask.asked(run, saved, epoch)
            else:
                self.agent_dispatch.wait_for_agent(run, saved, epoch)
            await self.live.restate(run.run_id)
        return replace(receipt(saved), cached=action == "hit")

    async def _dispatch_new(
        self,
        run: WorkflowRun,
        epoch: int,
        attempt: ScriptRunRecord,
        request: NodeRequest,
        turn_ctx: _TurnContext | None,
        digest: str,
        members: list[str] | None,
        saved: WorkflowNode | None,
        previous: WorkflowNode | None,
    ) -> NodeReceipt:
        """Create and dispatch (or save as settled/failed) a new generation of `request.key`."""
        key, kind, meta = request.key, request.kind, request.meta
        lane, index = split_key(key)
        generation = 1 if saved is None else saved.generation + 1
        grant, arguments = _tool_grant(request.args) if kind == "tool" else (None, request.args)
        delegated = grant is not None and grant.toolkit_name == "delegate"
        shown, asked = ("agent", delegated_request(arguments)) if delegated else (kind, request.args)
        node_name = cast("str | None", meta.get("name"))
        if turn_ctx is not None:
            node_name = turn_ctx.session.name or node_name
        elif shown == "session":
            node_name = cast("str | None", asked.get("name")) or node_name
        node = WorkflowNode(
            run_id=run.run_id,
            key=key,
            kind=shown,
            lane=lane,
            idx=index,
            digest=digest,
            request=asked,
            generation=generation,
            state=WorkflowNodeState.COMPLETED,
            attempt=run.attempt,
            label=node_label(shown, asked, meta.get("label") or asked.get("name")),
            name=node_name,
            phase=cast("str | None", meta.get("phase")),
            members=members,
            session_key=None if turn_ctx is None else turn_ctx.session.key,
            started_at=utc_timestamp(),
        )
        if (error := self._new_call_error(run, node, grant, delegated, turn_ctx)) is not None:
            failed = replace(node, state=WorkflowNodeState.FAILED, error=error, ended_at=node.started_at)
            return receipt(await asyncio.to_thread(self.store.put_node, failed, epoch, dirty_after=True))
        if saved is not None and shown in ("agent", "turn", "send"):
            await asyncio.to_thread(withdraw_unread, self.store, run.run_id, key, saved.generation)
        if shown in ("agent", "turn"):
            if grant is not None:
                return await self.dispatch.ask_gate(run, attempt, node, (grant, arguments))
            follow_up = None
            if turn_ctx is not None:
                mail = partial(
                    session_turn_message,
                    self.store,
                    run.run_id,
                    turn_ctx.session,
                    key,
                    generation,
                    str(asked["message"]),
                )
                turn_msg = await asyncio.to_thread(mail, prev_turn=turn_ctx.prev)
                follow_up, prompt = await self._prepare_turn(run, turn_ctx, saved, turn_msg)
                node = replace(node, request={**asked, "prompt": prompt, "effective_message": turn_msg})
            dispatch = partial(self.agent_dispatch.dispatch_agent, run, timeout=_timeout(meta), follow_up=follow_up)
            return await self.live.enter(run, node, dispatch)
        if grant is not None:
            cached = meta.get("cached") is not False
            same_as = (
                result_digest(await asyncio.to_thread(self.store.whole_result, previous))
                if previous is not None and not cached
                else None
            )
            start = partial(self.dispatch.dispatch_tool, run, attempt, tool_call=(grant, arguments), same_as=same_as)
            return await self.live.enter(run, node, start, dirty_after=same_as is None)
        return await self._save_immediate(run, epoch, node, request)

    async def _save_immediate(
        self,
        run: WorkflowRun,
        epoch: int,
        node: WorkflowNode,
        request: NodeRequest,
    ) -> NodeReceipt:
        """Save a non-dispatched call (`ask`, `send`, `inbox`, `state`, `value`, `error`, `group`, `session`)."""
        kind = request.kind
        if kind == "ask":
            self.ask.validate_question_options(request.args.get("options"))
            over = await self.live.check_budget(run)
            ask_node = replace(
                node,
                state=WorkflowNodeState.QUEUED if over else WorkflowNodeState.WAITING,
                waiting_on=None if over else "user",
                ended_at=None,
            )
            ask_node = await asyncio.to_thread(self.store.put_node, ask_node, epoch, dirty_after=True)
            if ask_node.state is WorkflowNodeState.WAITING:
                await self.live.restate(run.run_id)
            return receipt(await self.ask.asked(run, ask_node, epoch))
        if kind in ("send", "inbox", "state"):
            try:
                call = partial(script_op, self.store, run.run_id, node.key, node.generation, kind, request.args)
                done, value = await asyncio.to_thread(call)
            except WorkflowRunStoreError as exc:
                node = replace(node, state=WorkflowNodeState.FAILED, error={"kind": "failed", "message": str(exc)})
            else:
                if not done:
                    waiting = replace(node, state=WorkflowNodeState.WAITING, waiting_on="peer", ended_at=None)
                    wait_node = await asyncio.to_thread(self.store.put_node, waiting, epoch, dirty_after=True)
                    self.live.spawn(run.run_id, self.ask.wait_inbox(run, wait_node, epoch, float(value)))
                    return receipt(wait_node)
                node = replace(node, result=value)
        elif kind == "value":
            node = replace(node, result=_value(request.args))
        elif kind == "error":
            message = f"{request.args.get('type')}: {request.args.get('message')}"
            node = replace(node, state=WorkflowNodeState.FAILED, error={"kind": "error", "message": message})
        elif kind not in ("group", "session"):
            msg = f"This MindRoom cannot run a '{kind}' call in a workflow yet."
            raise WorkflowRunStoreError(msg)
        done_node = replace(node, ended_at=node.started_at)
        return receipt(await asyncio.to_thread(self.store.put_node, done_node, epoch, dirty_after=True))

    def _new_call_error(
        self,
        run: WorkflowRun,
        node: WorkflowNode,
        grant: ScriptToolGrant | None,
        delegated: bool,
        turn_ctx: _TurnContext | None,
    ) -> dict[str, object] | None:
        """Return a terminal error dict if a new tool/agent/turn call is refused before dispatch, else None."""
        if grant is not None:
            access = self.authorize(run, grant)
            if access is None:
                raise ScriptRuntimeUnavailableError(OWNER_NOT_READY)
            if isinstance(access, str):
                return {"kind": "failed", "message": access}
        if delegated and grant is not None and grant.function_name != RUN_SUBAGENT:
            return {"kind": "refused", "message": _NO_SESSIONS.format(function=grant.function_name)}
        if (
            node.kind == "turn"
            and turn_ctx is not None
            and any(other.key != node.key and other.state in LIVE_NODE_STATES for other in turn_ctx.in_attempt)
        ):
            return {"kind": "failed", "message": _SESSION_BUSY.format(name=node.name or turn_ctx.session.key)}
        if node.kind in ("agent", "turn"):
            schema_src = {**turn_ctx.session.request, **node.request} if turn_ctx is not None else node.request
            if (bad_schema := schema_failure(schema_src)) is not None:
                return {"kind": "schema", "message": bad_schema}
        return None

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

    def _turn_context(self, run: WorkflowRun, key: str, args: dict[str, object]) -> _TurnContext:
        """Resolve a `turn` call's session, its earlier turns in this attempt, and its lineage-hashed digest."""
        session_key, turn_num = args.get("session"), args.get("turn")
        session = self.store.get_node(run.run_id, session_key) if isinstance(session_key, str) else None
        if session is None or session.kind != "session":
            msg = f"Unknown session '{session_key}'."
            raise WorkflowRunStoreError(msg)
        if type(turn_num) is not int or turn_num < 1:
            msg = "Session turn number must be a whole number of at least 1."
            raise WorkflowRunStoreError(msg)
        in_attempt = [
            turn
            for turn in self.store.session_turns(run.run_id, session.key)
            if turn.attempt == run.attempt and not turn.stale
        ]
        earlier = sorted(
            (turn for turn in in_attempt if turn.key != key and int(turn.request["turn"]) < turn_num),
            key=lambda turn: (int(turn.request["turn"]), turn.seq),
        )
        prev = next((t for t in reversed(earlier) if t.state is WorkflowNodeState.COMPLETED), None)
        lineage = [
            (session.digest, session.generation),
            *((turn.key, turn.digest, turn.generation) for turn in earlier),
        ]
        digest = result_digest({"kind": "turn", "args": args, "lineage": lineage})
        return _TurnContext(session, in_attempt, earlier, prev, digest)

    async def _prepare_turn(
        self,
        run: WorkflowRun,
        turn_ctx: _TurnContext,
        saved: WorkflowNode | None,
        message: str,
    ) -> tuple[DelegationChild | None, str]:
        """Decide whether a session turn continues the saved child or starts a fresh child with a transcript."""
        completed = [turn for turn in turn_ctx.earlier if turn.state is WorkflowNodeState.COMPLETED]
        prev = turn_ctx.prev
        prev_sub = prev.links.get("subagent_id") if prev is not None else None
        if prev is not None and isinstance(prev_sub, str):
            context = self.authorize(run)
            if context is None:
                raise ScriptRuntimeUnavailableError(OWNER_NOT_READY)
            if not isinstance(context, str):
                child = await load_follow_up_child(context, run, prev_sub)
                allowed = {(prev.key, prev.generation)}
                if (
                    saved is not None
                    and saved.state in (WorkflowNodeState.FAILED, WorkflowNodeState.CANCELLED)
                    and not saved.links.get("made")
                ):
                    allowed.add((saved.key, saved.generation))
                fits = not (
                    run.persona
                    and child is not None
                    and child.child_agent_name == run.agent_name
                    and not fits_ceiling(child.persona, SubagentPersona.from_dict(run.persona))
                )
                if child is not None and (child.workflow_node_key, child.workflow_generation) in allowed and fits:
                    return child, message
        if not completed:
            return None, message
        history = [
            (
                str(turn.request.get("effective_message") or turn.request["message"]),
                res
                if isinstance(res := await asyncio.to_thread(self.store.whole_result, turn), str)
                else json.dumps(res),
            )
            for turn in completed
        ]
        return None, _transcript_prompt(history, message)

    async def node_receipt(
        self,
        attempt: ScriptRunRecord,
        node_id: str,
        *,
        wait: float,
        known_state: str | None = None,
    ) -> NodeReceipt:
        """Return a node's receipt, held up to *wait* seconds until it settles or enters/leaves `waiting`."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait
        while True:
            # Asked again after each wait: the end of the attempt ends its calls too, and those are not its to read.
            run = await self._current_run(attempt)
            async with (condition := self.live.condition(run.run_id)):
                node = self._node(run.run_id, node_id)
                remaining = deadline - loop.time()
                waiting_changed = known_state is not None and (
                    (node.state is WorkflowNodeState.WAITING) != (known_state == WorkflowNodeState.WAITING)
                )
                if node.state not in LIVE_NODE_STATES or waiting_changed or remaining <= 0:
                    return receipt(node)
                await wait_for_change(condition, remaining)

    async def node(self, attempt: ScriptRunRecord, node_id: str) -> WorkflowNode:
        """Return one node of the attempt's run, for its whole result."""
        return self._node((await self._current_run(attempt)).run_id, node_id)

    async def append_log(self, attempt: ScriptRunRecord, text: str) -> None:
        """Add one progress line to the attempt's run."""
        run = await self._current_run(attempt)
        await asyncio.to_thread(self.store.append_log, run.run_id, run.epoch, text)

    async def finish(self, attempt: ScriptRunRecord, *, result: object, error: str | None) -> None:
        """Store what the script's `main` returned or raised; the run ends when the attempt's process has.

        Also for a run that shows `waiting`: a call an earlier attempt left with a person, which this attempt's
        script did not reach, takes nothing from the report of the attempt the run serves.
        """
        run = await self._current_run(attempt)
        report = partial(self.store.update_run, run.run_id, run.epoch, when=SERVED_RUN_STATES)
        await asyncio.to_thread(report, result=result, error=error, finished_at=utc_timestamp())
        self.live.spawn(run.run_id, self._observe_exit(run.run_id, attempt.run_id))

    async def _current_run(self, attempt: ScriptRunRecord) -> WorkflowRun:
        """Return the run *attempt* is the current attempt of."""
        if attempt.workflow_run_id is None:
            msg = f"{attempt.run_id} is not a workflow attempt."
            raise LookupError(msg)
        async with self.live.condition(attempt.workflow_run_id):
            run = self.store.get_run(attempt.workflow_run_id)
        if run.epoch != self._launched.get(attempt.run_id):
            msg = f"Attempt {attempt.run_id} is no longer the attempt of {run.run_id}."
            raise SupersededAttemptError(msg)
        return run

    def _node(self, run_id: str, node_id: str) -> WorkflowNode:
        node = self.store.node_by_id(run_id, node_id)
        if node is None:
            msg = f"Run {run_id} has no call {node_id}."
            raise LookupError(msg)
        return node

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


def _transcript_prompt(earlier: list[tuple[str, str]], message: str) -> str:
    """Build the first prompt of a fresh session child from saved earlier exchanges and the new *message*."""
    sections = ["Earlier conversation:", *(f"User: {asked}\nAssistant: {answered}" for asked, answered in earlier)]
    return "\n\n".join(sections) + f"\n\n{message}"


def _tool_grant(request: dict[str, object]) -> tuple[ScriptToolGrant, dict[str, object]]:
    """Return the function a tool call's request names and its arguments; a request that names neither is refused."""
    arguments = request.get("arguments")
    if not isinstance(arguments, dict) or not all(
        isinstance(request.get(name), str) for name in ("toolkit", "function")
    ):
        msg = "A tool call needs a toolkit, a function and its arguments."
        raise WorkflowRunStoreError(msg)
    return ScriptToolGrant(str(request["toolkit"]), str(request["function"])), arguments


def _timeout(meta: dict[str, object]) -> float | None:
    """Return the seconds of work an agent call gives its turn, which is no part of what the call is."""
    seconds = meta.get("timeout")
    return float(seconds) if isinstance(seconds, (int, float)) and not isinstance(seconds, bool) else None


def _members(meta: dict[str, object]) -> tuple[str, ...]:
    """Return the ordered lanes a group's request names."""
    members = meta.get("members")
    if not isinstance(members, list) or not all(isinstance(member, str) for member in members):
        msg = "A group call needs the list of its members."
        raise WorkflowRunStoreError(msg)
    return tuple(str(member) for member in members)


def _value(args: dict[str, object]) -> object:
    """Make the value a `now()` or `random()` call of a script gets, once: a resume returns the saved one."""
    if args.get("op") == "now":
        return datetime.now(UTC).isoformat()
    if args.get("op") == "random":
        return secrets.SystemRandom().random()
    msg = f"A value call is now or random, not {args.get('op')!r}."
    raise WorkflowRunStoreError(msg)
