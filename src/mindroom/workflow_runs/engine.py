"""The workflow engine: a run's attempts, its tool nodes and their dispatch handles, and the run's state."""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import time
from collections import Counter
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import NAMESPACE_OID, uuid5
from weakref import WeakValueDictionary

from mindroom.background_tasks import run_coroutine_until_complete, wait_for_future_until_complete
from mindroom.delegation.personas import persona_allows
from mindroom.logging_config import get_logger
from mindroom.script_runs.broker import ScriptRuntimeUnavailableError
from mindroom.script_runs.manager import ScriptRunLimits
from mindroom.script_runs.models import (
    TERMINAL_SCRIPT_RUN_STATES,
    ScriptCallRecord,
    ScriptCallState,
    ScriptRunState,
    ScriptToolGrant,
    utc_timestamp,
)
from mindroom.script_runs.reasons import MAX_RUNTIME_EXCEEDED
from mindroom.script_runs.store import ScriptCallNotFoundError, ScriptRunNotFoundError, ScriptRunStoreError
from mindroom.workflow_runs.faults import hold
from mindroom.workflow_runs.models import (
    TERMINAL_RUN_STATES,
    DispatchHandle,
    NodeReceipt,
    WorkflowNode,
    WorkflowNodeState,
    WorkflowRun,
    WorkflowRunState,
)
from mindroom.workflow_runs.recovery import AUTO_RESUME_REASONS
from mindroom.workflow_runs.replay import DirtyLanes, decide, node_digest, result_digest, split_key, stale_keys
from mindroom.workflow_runs.store import WorkflowRunStoreError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine, Sequence

    from mindroom.script_runs.broker import ScriptToolBroker
    from mindroom.script_runs.manager import ScriptResourceProfileName, ScriptRunManager
    from mindroom.script_runs.models import ScriptRunRecord
    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from mindroom.workflow_runs.models import NodeRequest
    from mindroom.workflow_runs.store import WorkflowRunStore

logger = get_logger(__name__)

_UNSETTLED_NODE_STATES = frozenset({WorkflowNodeState.QUEUED, WorkflowNodeState.RUNNING})
_LIVE_RUN_STATES = frozenset(WorkflowRunState) - TERMINAL_RUN_STATES
# The states in which a run's attempt may submit calls: a run that is starting names no attempt that may.
_SERVED_RUN_STATES = frozenset({WorkflowRunState.RUNNING, WorkflowRunState.WAITING})
_SHELL_COMMAND = ScriptToolGrant("shell", "run_shell_command")
_MAX_LABEL_CHARS = 80
# The owner of the tasks that admit calls: they outlive their requests, and a run's end waits for them, not stops them.
_ADMISSIONS = "admissions"
_ERROR_TAIL_BYTES = 4096
_NO_RETURN = "The script ended without returning from main(args)."
_ABANDONED = "The call that waited was cancelled."
_ATTEMPT_GONE = "The record of the run's attempt is gone."
_CHANGED_WITHOUT_EDIT = (
    "Call n{seq} changed between attempts without a script edit. It probably contains the time or a random value; "
    "use now() or random() from mindroom.workflow."
)
_NO_ACCESS = "The requester no longer has access."
_OWNER_NOT_READY = "The run's agent is not ready in the run's room yet. Try again in a moment."
_IDLE = "Stopped resuming after {attempts} attempts that finished nothing. Fix the script and call resume_workflow."
# How many attempts in a row an interrupted run is given on its own while none of them finishes a call.
_MAX_IDLE_RESUMES = 5
_OUT_OF_TIME = "The run used its {hours:g} hours of working time. resume_workflow continues it."
# How often a running run's working time is saved, so a primary that dies loses at most this much of the count.
_CHARGE_SECONDS = 60.0
_INTERRUPTED: dict[str, object] = {"kind": "interrupted", "message": "The call was interrupted before its tool ran."}
# How long a waiter sleeps before it reads the store again, with or without a wake.
_RECHECK_SECONDS = 1.0
# How long after its report a script's exit is looked for, before the script runtime's own pass finds it.
_EXIT_POLL_SECONDS = (0.05, 0.2, 0.5, 1.0, 2.0, 5.0)

type _Outcome = tuple[WorkflowNodeState, object, dict[str, object] | None]


@dataclass(frozen=True, slots=True)
class WorkflowLimits:
    """The limits one `workflow` toolkit sets for the runs it starts."""

    max_concurrent_runs: int = 8
    max_runtime_hours: float = 72
    max_tool_calls_per_minute: int = 600
    max_concurrent_agents: int | None = None


# What a run an interruption stopped goes on with: its owner's live context and limits once the owner's access is
# confirmed; "pending" while that cannot be checked yet, "revoked" when it is gone, None when nobody waits for the run.
type ResumeAccess = tuple[ToolRuntimeContext, WorkflowLimits] | Literal["pending", "revoked"] | None


class SupersededAttemptError(RuntimeError):
    """Raised for a request of an attempt its run no longer names, so a leftover process cannot act."""


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
        self._clock = clock
        self.started_at = utc_timestamp()
        # What the saved owner of a run may start an attempt with now; set by the runtime that knows the live bots.
        self.owner_access: Callable[[WorkflowRun], ResumeAccess] = lambda _run: "pending"
        # The attempts this engine started. No other is served: one an earlier primary left is a leftover process.
        self._launched: set[str] = set()
        # The lanes in which a call ran in each run's current attempt.
        self._dirty: dict[str, DirtyLanes] = {}
        # When each running run's working time was last charged, by the engine's clock.
        self._charged: dict[str, float] = {}
        # One per run with a waiter or a launch in progress: polls and `wait` sleep on it, a launch holds its lock.
        self._changed: WeakValueDictionary[str, asyncio.Condition] = WeakValueDictionary()
        self._tasks: dict[str, set[asyncio.Task[Any]]] = {}
        self._attending: Counter[object] = Counter()
        self._inline_runs: dict[object, str] = {}
        self._closed = False
        self._launching: dict[asyncio.Task[WorkflowRun], str] = {}

    async def launch(
        self,
        run_id: str,
        context: ToolRuntimeContext,
        limits: WorkflowLimits | None = None,
        *,
        automatic: bool = False,
    ) -> WorkflowRun:
        """Start the next attempt of a reserved run; a script that cannot start ends the run failed with the reason.

        The engine owns a launch once it began: a caller that goes away leaves no process its run does not name, and
        only the engine's stop ends a launch, which leaves the run starting for the next primary. An *automatic*
        launch that is refused leaves the run as it is, for the next pass to try again.
        """
        if self._closed:
            return self.store.get_run(run_id)
        launch = self._spawn(_ADMISSIONS, self._launch(run_id, context, limits or WorkflowLimits(), automatic))
        self._launching[launch] = run_id
        launch.add_done_callback(self._launching.pop)
        try:
            return await asyncio.shield(launch)
        except asyncio.CancelledError:
            caller = asyncio.current_task()
            if not launch.cancelled() or (caller is not None and caller.cancelling()):
                raise
            # The stopping engine ended the launch; the run stays as it is, for the next primary to start.
            return self.store.get_run(run_id)

    async def _launch(
        self,
        run_id: str,
        context: ToolRuntimeContext,
        limits: WorkflowLimits,
        automatic: bool,
    ) -> WorkflowRun:
        # Held until the run names the attempt, so the new process's first request waits instead of being refused.
        async with self._condition(run_id):
            run = self.store.get_run(run_id)
            if run.state is not WorkflowRunState.STARTING or self._closed:
                # A reserved run gets one attempt from one launch; a run that was launched or has ended gets none,
                # and a stopping engine starts none.
                return run
            # An attempt after the first that the engine starts on its own counts toward the idle limit, once the run
            # names it.
            automatic = automatic and run.attempt > 0
            idle = automatic and run.idle_resumes >= _MAX_IDLE_RESUMES
            attempt, refusal = None, _IDLE.format(attempts=_MAX_IDLE_RESUMES)
            if not idle:
                try:
                    attempt = await self.scripts.run(
                        self._owner_context(run, context),
                        source=run.source,
                        workflow_run_id=run_id,
                        resource_profile=cast("ScriptResourceProfileName | None", run.resource_profile),
                        limits=ScriptRunLimits(max_tool_calls_per_minute=limits.max_tool_calls_per_minute),
                    )
                except Exception as exc:
                    logger.warning("workflow_launch_refused", run_id=run_id, error=str(exc))
                    refusal = str(exc)
                if attempt is None and automatic:
                    return run
            if attempt is not None:
                self._launched.add(attempt.run_id)
                # Before the run reads `running`, so whoever sees that state sees its time counted from here.
                self._charged[run_id] = self._clock()
                launched = await asyncio.to_thread(
                    self.store.update_run,
                    run_id,
                    when={WorkflowRunState.STARTING},
                    state=WorkflowRunState.RUNNING,
                    attempt=run.attempt + 1,
                    script_run_id=attempt.run_id,
                    idle_resumes=run.idle_resumes + automatic,
                )
                if launched.script_run_id != attempt.run_id:
                    # The run ended while its process started, so nothing names this attempt: stop it.
                    await self.scripts.revoke(attempt.run_id, reason=launched.error or launched.state.value)
                    await self.scripts.reconcile_durable(run_id=attempt.run_id)
                    self._charged.pop(run_id, None)
                else:
                    self._spawn(run_id, self._watch_time(run_id, limits.max_runtime_hours))
                return launched
        # The gate is free again, which ending a run takes.
        return await self._end_run(run, WorkflowRunState.INTERRUPTED if idle else WorkflowRunState.FAILED, refusal)

    def _owner_context(self, run: WorkflowRun, caller: ToolRuntimeContext) -> ToolRuntimeContext:
        """Return the context every attempt of *run* executes in: that of the conversation the run was started in.

        A caller in that conversation is that context. One that resumes the run from elsewhere only receives the
        report: the run's own conversation is rebuilt from its saved owner, whose access there is checked again.
        """
        if (run.room_id, run.thread_root_event_id) == (caller.room_id, caller.resolved_thread_id):
            return caller
        access = self.owner_access(run)
        if not isinstance(access, tuple):
            raise WorkflowRunStoreError(_NO_ACCESS if access == "revoked" else _OWNER_NOT_READY)
        return access[0]

    async def cancel(self, run_id: str, *, reason: str, job_id: str | None = None) -> WorkflowRun:
        """Revoke the run's attempt, end its process and end the run cancelled; completed nodes stay saved.

        With *job_id*, only a run that is still that job's is cancelled: one that a resume gave to another job is left.
        """
        return await self._stop(run_id, WorkflowRunState.CANCELLED, reason, job_id)

    async def _stop(self, run_id: str, state: WorkflowRunState, reason: str, job_id: str | None = None) -> WorkflowRun:
        """Revoke the run's attempt, end its process and end the run in *state*, unless the attempt ended before."""
        async with self._condition(run_id):
            run = self.store.get_run(run_id)
        if run.state in TERMINAL_RUN_STATES or job_id not in (None, run.job_id):
            return run
        outcome: tuple[WorkflowRunState, str | None] = (state, reason)
        # A run that waits for its next attempt has no process: how its last one ended says nothing about this stop.
        if run.script_run_id is not None and run.state is not WorkflowRunState.STARTING:
            await self.scripts.revoke(run.script_run_id, reason=reason)
            attempt = await self.scripts.reconcile_durable(run_id=run.script_run_id)
            if attempt.state in TERMINAL_SCRIPT_RUN_STATES:
                # The same rule as the maintenance pass: an attempt that ended before the stop ends as it did.
                outcome = _run_outcome(self.store.get_run(run_id), attempt, stopped=state)
        return await self._end_run(run, *outcome)

    def resumable(self, run_id: str, rerun: Sequence[str] = (), *, claimed: bool = False) -> frozenset[str]:
        """Return the keys of the calls *rerun* names, for a run that has ended; a live run or an unknown ID is refused.

        A resume that is *claimed* compares the run's end when it takes the run, so only the calls are checked here.
        """
        if not claimed and self.store.get_run(run_id).state not in TERMINAL_RUN_STATES:
            msg = f"Run {run_id} is still running. Cancel it first."
            raise WorkflowRunStoreError(msg)
        try:
            return frozenset(self._node(run_id, str(node_id)).key for node_id in rerun)
        except LookupError as exc:
            raise WorkflowRunStoreError(str(exc)) from exc

    async def reopen(
        self,
        run_id: str,
        context: ToolRuntimeContext,
        *,
        source: str | None = None,
        rerun: Sequence[str] = (),
        limits: WorkflowLimits | None = None,
        job_id: str | None = None,
        claim: object = None,
    ) -> tuple[WorkflowRun, bool]:
        """Make an ended run ready for its next attempt, with the full working time; `launch` then starts it.

        The saved calls stay: the next attempt replays the script, with *source* when given, and reuses them, except
        the calls *rerun* names. The answer has the form of a reservation that found its run.

        Nothing changes before the resume takes the run, and it takes it in one write: the run becomes *job_id*'s, or
        no job's, names no attempt, and the calls of *rerun* are marked. *context* is refused first when the run's
        own conversation is closed to it. *claim* is the job and the end the run had when the resume was accepted:
        a run that another resume took since, also one whose attempt never started, is refused.
        """
        hours = (limits or WorkflowLimits()).max_runtime_hours
        run = self.store.get_run(run_id)
        keys = self.resumable(run_id, rerun, claimed=claim is not None)
        self._owner_context(run, context)
        text = run.source if source is None else source
        take = partial(
            self.store.claim_run,
            run_id,
            claim,
            keys,
            state=WorkflowRunState.STARTING,
            source=text,
            source_digest=hashlib.sha256(text.encode()).hexdigest(),
            previous_source_digest=run.source_digest,
            runtime_seconds_left=hours * 3600 if hours else None,
            idle_resumes=0,
            job_id=job_id,
            script_run_id=None,
            result=None,
            error=None,
            finished_at=None,
        )
        async with self._condition(run_id):
            # Waited for also by a caller that is cancelled here, so the run it finds says whether it took it.
            reopened = await wait_for_future_until_complete(asyncio.ensure_future(asyncio.to_thread(take)))
        if reopened is None:
            msg = f"Run {run_id} was resumed by another call."
            raise WorkflowRunStoreError(msg)
        return reopened, False

    async def resume(
        self,
        context: ToolRuntimeContext,
        run_id: str,
        *,
        limits: WorkflowLimits | None = None,
        **request: Any,  # noqa: ANN401
    ) -> WorkflowRun:
        """Start the next attempt of an ended run for a person; see `reopen` for the *request*."""
        await self.reopen(run_id, context, limits=limits, **request)
        return await self.launch(run_id, context, limits)

    async def wait(self, run_id: str) -> WorkflowRun:
        """Return the run once it has ended."""
        condition = self._condition(run_id)
        async with condition:
            while (run := self.store.get_run(run_id)).state not in TERMINAL_RUN_STATES:
                # The saved run decides: a wake only shortens the wait, since a writer cancelled at its commit sends none.
                with suppress(TimeoutError):
                    await asyncio.wait_for(condition.wait(), _RECHECK_SECONDS)
        return run

    async def attend(
        self,
        origin: str | None,
        reserve: Callable[[], Awaitable[tuple[WorkflowRun, bool]]],
        context: ToolRuntimeContext,
        limits: WorkflowLimits,
    ) -> WorkflowRun:
        """Reserve, launch and wait for a run that is no job, as one of the calls of *origin* that wait for it.

        A cancelled call only stops waiting, and its reservation still ends. Whenever a call leaves, however it ends,
        and no other call of *origin* is left while MindRoom is not stopping, nobody can receive the report, so the
        engine cancels the run. A call whose reservation is refused holds the run only until that refusal.
        """
        key: object = origin or object()
        self._attending[key] += 1
        reserving = asyncio.ensure_future(reserve())
        try:
            run, _ = await wait_for_future_until_complete(reserving)
            await self.launch(run.run_id, context, limits)
            return await self.wait(run.run_id)
        finally:
            if not reserving.cancelled() and not reserving.exception():
                self._inline_runs[key] = reserving.result()[0].run_id
            self._attending -= Counter({key: 1})
            stopping = self._closed or context.orchestrator is None or not context.orchestrator.running
            if not self._attending[key] and (run_id := self._inline_runs.pop(key, None)) and not stopping:
                # A run that ended is left as it ended.
                await run_coroutine_until_complete(self.cancel(run_id, reason=_ABANDONED))

    def status(self, run_id: str) -> tuple[WorkflowRun, list[WorkflowNode]]:
        """Return the run and its current nodes."""
        return self.store.get_run(run_id), self.store.nodes(run_id)

    async def reconcile(self, access: Callable[[WorkflowRun], ResumeAccess] | None = None) -> list[str]:
        """End each run whose attempt has ended, as that attempt and the script's report say.

        With *access*, a run that an infrastructure interruption stopped gets its next attempt instead, with what
        *access* answers for it; the runs that got one are returned. One run's error stops no other.
        """
        resumed = []
        for run in await asyncio.to_thread(self.store.list_runs, include_finished=False):
            try:
                if await self._reconcile_run(run, access(run) if access is not None else None):
                    resumed.append(run.run_id)
            except Exception:
                logger.exception("workflow_run_reconcile_failed", run_id=run.run_id)
        return resumed

    async def go_on(self, run_id: str, context: ToolRuntimeContext, limits: WorkflowLimits) -> None:
        """Start the attempt *run_id* waits for, for the job that was just continued and can cancel the run now."""
        await self._reconcile_run(self.store.get_run(run_id), (context, limits))

    async def _reconcile_run(self, run: WorkflowRun, access: ResumeAccess) -> bool:
        """End *run* if its attempt has ended, or start its next attempt; return whether one started."""
        if access == "pending":
            # Nothing is decided before the owner's access is: the run stays as the interruption left it.
            return False
        if run.state is WorkflowRunState.STARTING:
            if access is None or run.run_id in self._launching.values():
                # Being launched, or its own call or job's to launch.
                return False
        else:
            try:
                attempt = await asyncio.to_thread(self.scripts.store.get_run, run.script_run_id or "")
            except ScriptRunNotFoundError:
                attempt = None
            if attempt is not None and attempt.state not in TERMINAL_SCRIPT_RUN_STATES:
                return False
            state, error = _run_outcome(self.store.get_run(run.run_id), attempt)
            interrupted = state is WorkflowRunState.INTERRUPTED and error in AUTO_RESUME_REASONS
            # The attempt's own process limit says nothing about a run that has working time left, or no limit.
            spent = error == MAX_RUNTIME_EXCEEDED and run.runtime_seconds_left == 0
            if access is None or not interrupted or spent:
                await self._end_run(run, state, error)
                return False
            # The attempt is over and the run is not: its nodes are settled, and it waits for its next attempt.
            waiting = await self._end_run(run, WorkflowRunState.STARTING, None, finished_at=None)
            if waiting.script_run_id != run.script_run_id:
                # A resume took the run since this look began: the attempt it judged says nothing about that one.
                return False
            run = waiting
        return await self._relaunch(run, access)

    async def _relaunch(self, run: WorkflowRun, access: ResumeAccess) -> bool:
        """Start the next attempt of an interrupted run that waits for one, as *access* allows; return whether it started."""
        if access is None or run.state is not WorkflowRunState.STARTING:
            return False
        if isinstance(access, str):
            await self._end_run(run, WorkflowRunState.INTERRUPTED, _NO_ACCESS)
            return False
        context, limits = access
        return (await self.launch(run.run_id, context, limits, automatic=True)).attempt > run.attempt

    async def close(self) -> None:
        """Stop the engine's own tasks and start no more; what was starting or unsettled stays for the next start."""
        self._closed = True
        tasks = [task for run_tasks in self._tasks.values() for task in run_tasks]
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def run_info(self, attempt: ScriptRunRecord) -> dict[str, object]:
        """Return what the attempt's script starts from."""
        run = await self._current_run(attempt)
        return {"run_id": run.run_id, "attempt": run.attempt, "args": run.args}

    async def submit_node(self, attempt: ScriptRunRecord, request: NodeRequest) -> NodeReceipt:
        """Save and dispatch one call of the attempt's script, or return the receipt of the node its key has."""
        run = await self._current_run(attempt)
        # The engine owns a call from its insert to its dispatch, so a caller that goes away strands no running node.
        return await asyncio.shield(self._spawn(_ADMISSIONS, self._admit(run.run_id, attempt, request)))

    async def _admit(self, run_id: str, attempt: ScriptRunRecord, request: NodeRequest) -> NodeReceipt:
        # Held from the lookup to the dispatch: two requests with one key cannot both find it free, and the run
        # cannot end in between.
        async with self._condition(run_id):
            return await self._admit_gated(self.store.get_run(run_id), attempt, request)

    async def _admit_gated(self, run: WorkflowRun, attempt: ScriptRunRecord, request: NodeRequest) -> NodeReceipt:  # noqa: C901
        key, kind, meta = request.key, request.kind, request.meta
        lanes = self._dirty.setdefault(run.run_id, DirtyLanes())
        digest = node_digest(kind, request.args)
        members = _members(meta) if kind == "group" else None
        saved = self.store.get_node(run.run_id, key)
        # A saved call with no digest was named by a resume, or follows a call that ran in an attempt that died.
        cached, dirty, rerun = (
            meta.get("cached") is not False,
            lanes.is_dirty(key),
            saved is not None and not saved.digest,
        )
        decision = partial(decide, digest=digest, attempt=run.attempt, dirty=dirty, rerun=rerun, cached=cached)
        action = decision(saved).action
        if saved is not None and action == "return":
            # A request the script sent twice never repeats work, not even failed work.
            return _receipt(saved)
        if run.script_run_id != attempt.run_id or run.state not in _SERVED_RUN_STATES:
            msg = f"Run {run.run_id} takes no more calls from attempt {attempt.run_id}."
            raise SupersededAttemptError(msg)
        if saved is not None and action == "reconcile":
            # Left unsettled by an attempt that ended: its handle says how it ended, before any rule may run it again.
            await asyncio.to_thread(self.settle_from_handle, saved)
            saved = self.store.get_node(run.run_id, key)
            action = decision(saved).action
        if saved is not None and action != "run":
            # Reused: the node is this attempt's now, and a fan-out whose items changed dirties what follows it.
            stamp: dict[str, object] = {"attempt": run.attempt, "stale": False}
            if members != saved.members:
                stamp["members"] = members
                # What follows the group is dirty and no member lane is, which is what marking the group does.
                await self._mark_dirty(lanes, run.run_id, run.attempt, key)
            await asyncio.to_thread(partial(self.store.update_node, run.run_id, key, saved.generation, **stamp))
            if action == "raise_indeterminate":
                # It may have run, so nothing is dispatched until a resume names it.
                return replace(_receipt(saved), state=WorkflowNodeState.FAILED)
            return replace(_receipt(saved), cached=action == "hit")
        # The completed node this call replaces although no rule before it said so: the call itself changed, or it
        # runs on every resume.
        completed = saved is not None and saved.state is WorkflowNodeState.COMPLETED and not (dirty or rerun)
        previous = saved if completed else None
        changed = previous is not None and previous.digest != digest
        if previous is not None and changed and cached and run.source_digest == run.previous_source_digest:
            notice = _CHANGED_WITHOUT_EDIT.format(seq=previous.seq)
            await asyncio.to_thread(self.store.append_log, run.run_id, notice)
        lane, index = split_key(key)
        generation = 1 if saved is None else saved.generation + 1
        node = WorkflowNode(
            run_id=run.run_id,
            key=key,
            kind=kind,
            lane=lane,
            idx=index,
            digest=digest,
            request=request.args,
            generation=generation,
            state=WorkflowNodeState.COMPLETED,
            attempt=run.attempt,
            label=str(meta.get("label") or _default_label(kind, request.args))[:_MAX_LABEL_CHARS],
            name=cast("str | None", meta.get("name")),
            phase=cast("str | None", meta.get("phase")),
            members=members,
            cached=cached,
            started_at=utc_timestamp(),
        )
        if kind == "tool":
            same_as = None
            if previous is not None and not changed:
                same_as = result_digest(await asyncio.to_thread(self.store.whole_result, previous))
            return await self._dispatch_tool(run, attempt, node, lanes, same_as)
        if kind == "value":
            node = replace(node, result=_value(request.args))
        elif kind != "group":
            msg = f"This MindRoom cannot run a '{kind}' call in a workflow yet."
            raise WorkflowRunStoreError(msg)
        # A value and a group are complete when they are submitted, so no restart finds one half done.
        await self._mark_dirty(lanes, run.run_id, run.attempt, key)
        node = await asyncio.to_thread(self.store.put_node, replace(node, ended_at=node.started_at))
        return _receipt(node)

    async def _mark_dirty(self, lanes: DirtyLanes, run_id: str, attempt: int, key: str) -> None:
        """Record that the call at *key* runs in *attempt*: what follows it in its lanes runs again, in any attempt."""
        lanes.mark(key)
        await asyncio.to_thread(self._forget_after, run_id, attempt, key)

    def _forget_after(self, run_id: str, attempt: int, key: str) -> None:
        """Make each completed call an earlier attempt left after *key*, in its lanes, run again when it is reached.

        A later attempt remembers nothing of this one, so those calls lose their digests before the call that dirtied
        them changes. A call that may have run keeps its digest, and its own rule. A first attempt reuses nothing.
        """
        if attempt > 1:
            lanes = DirtyLanes()
            lanes.mark(key)
            saved = self.store.reusable_keys(run_id, attempt)
            self.store.forget_digests(run_id, [key for key in saved if lanes.is_dirty(key)])

    def _invalidate(self, node: WorkflowNode, outcome: _Outcome) -> bool:
        """Return whether a call that runs on every resume answered something else than before, and if so mark what follows it.

        Its handle holds the digest of the earlier result, compared whole and as JSON. The live call and the look at
        the receipt of a call whose attempt died both come here, before the node shows its end.
        """
        same_as = node.handle.same_as if node.handle is not None else None
        state, result, _ = outcome
        if same_as is None or (state is WorkflowNodeState.COMPLETED and result_digest(result) == same_as):
            return False
        self._forget_after(node.run_id, node.attempt, node.key)
        return True

    async def _dispatch_tool(
        self,
        run: WorkflowRun,
        attempt: ScriptRunRecord,
        node: WorkflowNode,
        lanes: DirtyLanes,
        same_as: str | None,
    ) -> NodeReceipt:
        """Save a tool node with the handle of its call, then run the call as the engine's own task.

        With *same_as*, the digest of its earlier result, the call's lane is dirty only if it answers something else;
        without, it ran because a rule said so, and its lane is dirty whatever it answers.
        """
        request = node.request
        arguments = request.get("arguments")
        if not isinstance(arguments, dict) or not all(
            isinstance(request.get(name), str) for name in ("toolkit", "function")
        ):
            msg = "A tool call needs a toolkit, a function and its arguments."
            raise WorkflowRunStoreError(msg)
        grant = ScriptToolGrant(str(request["toolkit"]), str(request["function"]))
        call_id = uuid5(NAMESPACE_OID, f"{run.run_id}:{node.key}:{node.generation}").hex
        if same_as is None:
            await self._mark_dirty(lanes, run.run_id, run.attempt, node.key)
        node = await asyncio.to_thread(
            self.store.put_node,
            replace(
                node,
                state=WorkflowNodeState.RUNNING,
                # Saved before the dispatch, so a crash after the tool ran reads the broker's receipt for this call.
                handle=DispatchHandle("broker", f"{attempt.run_id}:{call_id}", dispatched=False, same_as=same_as),
            ),
        )
        kept = cast("list[str] | None", (run.persona or {}).get("tools"))
        call = partial(self.broker.execute_for_workflow, attempt, call_id=call_id, grant=grant, arguments=arguments)
        self._spawn(run.run_id, self._run_tool_node(node, grant, call, kept, lanes))
        return _receipt(node)

    async def node_receipt(self, attempt: ScriptRunRecord, node_id: str, *, wait: float) -> NodeReceipt:
        """Return a node's receipt, held up to *wait* seconds until the node has ended."""
        run = await self._current_run(attempt)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait
        condition = self._condition(run.run_id)
        async with condition:
            while True:
                node = self._node(run.run_id, node_id)
                remaining = deadline - loop.time()
                if node.state not in _UNSETTLED_NODE_STATES or remaining <= 0:
                    return _receipt(node)
                with suppress(TimeoutError):
                    await asyncio.wait_for(condition.wait(), min(remaining, _RECHECK_SECONDS))

    async def node(self, attempt: ScriptRunRecord, node_id: str) -> WorkflowNode:
        """Return one node of the attempt's run, for its whole result."""
        return self._node((await self._current_run(attempt)).run_id, node_id)

    async def append_log(self, attempt: ScriptRunRecord, text: str) -> None:
        """Add one progress line to the attempt's run."""
        await asyncio.to_thread(self.store.append_log, (await self._current_run(attempt)).run_id, text)

    async def finish(self, attempt: ScriptRunRecord, *, result: object, error: str | None) -> None:
        """Store what the script's `main` returned or raised; the run ends when the attempt's process has."""
        run = await self._current_run(attempt)
        await asyncio.to_thread(
            self.store.update_run,
            run.run_id,
            when={WorkflowRunState.RUNNING},
            result=result,
            error=error,
            finished_at=utc_timestamp(),
        )
        self._spawn(run.run_id, self._observe_exit(attempt.run_id))

    def settle_from_handle(self, node: WorkflowNode) -> WorkflowNodeState:
        """Settle a node an ended attempt left unsettled from the broker receipt its handle names; return its state.

        The work is never done again here: a finished receipt is copied, and a call that never reached its tool fails
        as safe to run again.
        """
        assert node.handle is not None
        script_run_id, _, call_id = node.handle.ref.partition(":")
        try:
            outcome = _call_outcome(node, self.scripts.store.get_call(script_run_id, call_id))
        except ScriptCallNotFoundError:
            # No claim proves the call never started only while the attempt's records are still there to hold one.
            try:
                self.scripts.store.get_run(script_run_id)
                outcome = (WorkflowNodeState.FAILED, None, _INTERRUPTED)
            except ScriptRunNotFoundError:
                outcome = _indeterminate(node)
        self._invalidate(node, outcome)
        state, result, error = outcome
        self.store.settle_node(node.run_id, node.key, node.generation, state=state, result=result, error=error)
        return state

    def _condition(self, run_id: str) -> asyncio.Condition:
        condition = self._changed.get(run_id)
        if condition is None:
            condition = self._changed[run_id] = asyncio.Condition()
        return condition

    async def _notify(self, run_id: str) -> None:
        condition = self._changed.get(run_id)
        if condition is not None:
            async with condition:
                condition.notify_all()

    def _spawn[T](self, owner: str, work: Coroutine[None, None, T]) -> asyncio.Task[T]:
        """Start *work* as the engine's own task, stopped when the run *owner* ends or the engine closes."""
        tasks = self._tasks.setdefault(owner, set())
        task = asyncio.create_task(work, name=f"workflow:{owner}")
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return task

    async def _current_run(self, attempt: ScriptRunRecord) -> WorkflowRun:
        """Return the run *attempt* is the current attempt of."""
        if attempt.workflow_run_id is None:
            msg = f"{attempt.run_id} is not a workflow attempt."
            raise LookupError(msg)
        async with self._condition(attempt.workflow_run_id):
            run = self.store.get_run(attempt.workflow_run_id)
        if run.script_run_id != attempt.run_id or attempt.run_id not in self._launched:
            msg = f"Attempt {attempt.run_id} is no longer the attempt of {run.run_id}."
            raise SupersededAttemptError(msg)
        return run

    def _node(self, run_id: str, node_id: str) -> WorkflowNode:
        node = self.store.node_by_seq(run_id, int(node_id[1:])) if node_id[1:].isdecimal() else None
        if node is None:
            msg = f"Run {run_id} has no call {node_id}."
            raise LookupError(msg)
        return node

    async def _run_tool_node(
        self,
        node: WorkflowNode,
        grant: ScriptToolGrant,
        call: Callable[[], Awaitable[ScriptCallRecord]],
        kept: list[str] | None,
        lanes: DirtyLanes,
    ) -> None:
        """Run one tool call and settle its node; *kept* is the tool list of the narrowed agent that started the run."""
        outcome: _Outcome
        try:
            _require_kept(kept, grant)
            outcome = _call_outcome(node, await call())
        except (ScriptRunStoreError, ScriptRuntimeUnavailableError) as exc:
            # Refused before any claim: the rate limit, a revoked attempt, a broker that takes no calls now.
            outcome = WorkflowNodeState.FAILED, None, {"kind": "failed", "message": str(exc)}
        if await asyncio.to_thread(self._invalidate, node, outcome):
            # Before the node shows its end, so the script's next call already finds the lane dirty.
            lanes.mark(node.key)
        await hold("before_settle")
        state, result, error = outcome
        settle = partial(self.store.settle_node, node.run_id, node.key, node.generation)
        await asyncio.to_thread(settle, state=state, result=result, error=error)
        await self._notify(node.run_id)

    async def _watch_time(self, run_id: str, hours: float) -> None:
        """Save a running run's working time as it is used, and stop the run when none is left."""
        left = self.store.get_run(run_id).runtime_seconds_left
        while left is not None and left > 0:
            await asyncio.sleep(min(_CHARGE_SECONDS, left))
            run = self.store.get_run(run_id)
            if charge := self._time_left(run):
                run = await asyncio.to_thread(partial(self.store.update_run, run_id, when=_SERVED_RUN_STATES, **charge))
            left = run.runtime_seconds_left
        if left is not None:
            await self._stop(run_id, WorkflowRunState.INTERRUPTED, _OUT_OF_TIME.format(hours=hours))

    def _time_left(self, run: WorkflowRun) -> dict[str, object]:
        """Return, as a change to save, the working time *run* has left after what it used since the last charge.

        Only a running run uses any: time spent waiting for a person, starting or ended is not charged.
        """
        now = self._clock()
        since, self._charged[run.run_id] = self._charged.get(run.run_id, now), now
        if run.state is not WorkflowRunState.RUNNING or run.runtime_seconds_left is None:
            return {}
        return {"runtime_seconds_left": max(0.0, run.runtime_seconds_left - (now - since))}

    async def _observe_exit(self, script_run_id: str) -> None:
        """End the run soon after its script reported: look for the exit instead of waiting for the next pass."""
        for delay in _EXIT_POLL_SECONDS:
            await asyncio.sleep(delay)
            if (await self.scripts.reconcile_durable(run_id=script_run_id)).state in TERMINAL_SCRIPT_RUN_STATES:
                # Only the run that reported: another run's ended attempt is the next pass's to judge.
                run = await asyncio.to_thread(self.store.run_for_script, script_run_id)
                if run is not None:
                    await self._reconcile_run(run, None)
                break

    async def _end_run(
        self,
        run: WorkflowRun,
        state: WorkflowRunState,
        error: str | None,
        **changes: object,
    ) -> WorkflowRun:
        """End *run*: stop its node tasks, read each unsettled node from its handle, store the state, wake waiters.

        With the state `starting` only the run's attempt is over. A run that has ended, or that names another attempt
        than *run* does, is left as it is.
        """
        condition = self._condition(run.run_id)
        # Held to the run's last write, so no call is admitted between the look at its nodes and its end.
        async with condition:
            if (moved_on := self.store.get_run(run.run_id)).script_run_id != run.script_run_id:
                return moved_on
            tasks = self._tasks.pop(run.run_id, set()) - {asyncio.current_task()}
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            current = self.store.get_run(run.run_id)
            if current.state in TERMINAL_RUN_STATES:
                return current
            nodes = self.store.nodes(run.run_id, include_stale=True)
            for node in nodes:
                if node.state in _UNSETTLED_NODE_STATES and node.handle is not None:
                    await asyncio.to_thread(self.settle_from_handle, node)
            if state is WorkflowRunState.COMPLETED:
                # What the completed script no longer reaches leaves the run's current view.
                await asyncio.to_thread(self.store.mark_stale, run.run_id, stale_keys(nodes, current.attempt))
            ended_at = current.finished_at or utc_timestamp()
            changes = {"state": state, "error": error, "finished_at": ended_at, **self._time_left(current), **changes}
            ended = await asyncio.to_thread(
                partial(self.store.update_run, run.run_id, when=_LIVE_RUN_STATES, **changes),
            )
            self._dirty.pop(run.run_id, None)
            self._charged.pop(run.run_id, None)
            condition.notify_all()
        return ended


def _require_kept(kept: list[str] | None, grant: ScriptToolGrant) -> None:
    """Refuse a tool outside *kept*; checked at every call beside the owner's live tools, so a run never has more."""
    if kept is not None and not persona_allows(tuple(kept), grant.toolkit_name, grant.function_name):
        msg = f"{grant.toolkit_name}.{grant.function_name} is not a tool of the agent that started this run."
        raise ScriptRunStoreError(msg)


def _default_label(kind: str, args: dict[str, object]) -> object:
    """Return what a node is called when its script gave no label: the tool's function, or the operation."""
    return f"{args.get('toolkit')}.{args.get('function')}" if kind == "tool" else args.get("op") or kind


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


def _receipt(node: WorkflowNode) -> NodeReceipt:
    """Return what the script is told about *node*; a result too large for the row is named, not inlined."""
    node_id = f"n{node.seq}"
    spilled = node.result_file is not None
    return NodeReceipt(
        id=node_id,
        state=node.state,
        cached=False,
        result=None if spilled else node.result,
        error=node.error,
        result_ref=f"results/{node_id}" if spilled else None,
    )


def _indeterminate(node: WorkflowNode) -> _Outcome:
    message = (
        f"This call may already have run (attempt {node.attempt}). Check its effect; "
        f'to run it again call resume_workflow(run_id, rerun=["n{node.seq}"]).'
    )
    return WorkflowNodeState.INDETERMINATE, None, {"kind": "indeterminate", "message": message}


def _completed_outcome(node: WorkflowNode, call: ScriptCallRecord) -> _Outcome:
    """Return how the node of a call that answered ends; a shell command ended only if its exit status says so."""
    facts = call.metadata or {}
    if call.grant != _SHELL_COMMAND:
        return WorkflowNodeState.COMPLETED, call.result, None
    if facts.get("not_started"):
        # The shell tool refused before any command ran, and its text says why.
        return WorkflowNodeState.FAILED, None, {"kind": "failed", "message": str(call.result)}
    if facts.get("exit_code") is None:
        # No status says the command ended: its answer was lost on the way, so it may have run.
        return _indeterminate(node)
    return WorkflowNodeState.COMPLETED, {"output": call.result, "exit_code": facts["exit_code"]}, None


def _call_outcome(node: WorkflowNode, call: ScriptCallRecord) -> _Outcome:
    """Map a broker receipt to how its node ends; the effect stamp says what is safe to run again."""
    if call.state is ScriptCallState.COMPLETED:
        return _completed_outcome(node, call)
    details = cast("dict[str, object]", call.error if isinstance(call.error, dict) else {})
    kind = details.get("kind")
    if call.effect_started_at is None and call.state is not ScriptCallState.FAILED:
        # Claimed, and still waiting for hooks or approval when its attempt ended: the tool was never invoked.
        return WorkflowNodeState.FAILED, None, _INTERRUPTED
    if call.state is ScriptCallState.FAILED and (call.effect_started_at is None or kind == "tool_failure"):
        refusal = "denied" if kind == "approval_denied" else "failed"
        return WorkflowNodeState.FAILED, None, {"kind": refusal, "message": details.get("message")}
    # The tool was invoked and the receipt holds no end it can state: orphaned, or a result it could not keep.
    return _indeterminate(node)


def _run_outcome(
    run: WorkflowRun,
    attempt: ScriptRunRecord | None,
    stopped: WorkflowRunState = WorkflowRunState.CANCELLED,
) -> tuple[WorkflowRunState, str | None]:
    """Return how a run whose attempt ended ends: an accepted stop first, then a failed exit, then the report.

    A stop is a cancel unless the engine stopped the run as *stopped*. An attempt whose record is gone ended with the
    primary, for all that is known.
    """
    state = ScriptRunState.INTERRUPTED if attempt is None else attempt.state
    reason = _ATTEMPT_GONE if attempt is None else attempt.cancellation_reason
    if state is ScriptRunState.CANCELLED:
        return stopped, reason
    if attempt is not None and state is ScriptRunState.FAILED:
        tail = (attempt.error or attempt.output).encode()[-_ERROR_TAIL_BYTES:]
        return WorkflowRunState.FAILED, run.error or tail.decode(errors="ignore")
    if run.finished_at is not None:
        # The script reported and its process then ended cleanly or with the primary, so the report decides.
        return (WorkflowRunState.COMPLETED if run.error is None else WorkflowRunState.FAILED), run.error
    if state is ScriptRunState.EXITED:
        return WorkflowRunState.FAILED, _NO_RETURN
    return WorkflowRunState.INTERRUPTED, reason
