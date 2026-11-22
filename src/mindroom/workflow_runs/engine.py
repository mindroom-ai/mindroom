"""The workflow engine: a run's attempts, its tool nodes and their dispatch handles, and the run's state."""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import time
from collections import Counter, deque
from contextlib import suppress
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import NAMESPACE_OID, uuid5
from weakref import WeakValueDictionary

from mindroom.background_tasks import run_coroutine_until_complete, wait_for_future_until_complete
from mindroom.delegation.personas import persona_allows
from mindroom.delegation.state import SubagentPersona
from mindroom.logging_config import get_logger
from mindroom.redaction import redact_sensitive_text
from mindroom.script_runs.broker import ScriptRuntimeUnavailableError
from mindroom.script_runs.manager import ScriptRunLimits, ScriptRuntimeNotReadyError
from mindroom.script_runs.models import (
    TERMINAL_SCRIPT_RUN_STATES,
    ScriptCallRecord,
    ScriptCallState,
    ScriptRunState,
    ScriptToolGrant,
    utc_timestamp,
)
from mindroom.script_runs.store import (
    ScriptCallNotFoundError,
    ScriptCallRateLimitError,
    ScriptRunNotFoundError,
    ScriptRunStoreError,
)
from mindroom.tool_jobs.runtime import TERMINAL_STATUSES
from mindroom.workflow_runs.agent_nodes import (
    FRESH_AGENT_FUNCTION,
    AgentNodeError,
    AgentNotReadyError,
    AgentRuntime,
    await_child,
    delegated_request,
    launch_child,
)
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
from mindroom.workflow_runs.recovery import AUTO_RESUME_REASONS, NO_ACCESS
from mindroom.workflow_runs.replay import DirtyLanes, decide, node_digest, result_digest, split_key, stale_keys
from mindroom.workflow_runs.store import SupersededAttemptError, WorkflowRunStoreError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence

    from mindroom.script_runs.broker import ScriptToolBroker
    from mindroom.script_runs.manager import ScriptResourceProfileName, ScriptRunManager
    from mindroom.script_runs.models import ScriptRunRecord
    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from mindroom.workflow_runs.models import NodeRequest
    from mindroom.workflow_runs.store import WorkflowRunStore

logger = get_logger(__name__)

_UNSETTLED_NODE_STATES = frozenset({WorkflowNodeState.QUEUED, WorkflowNodeState.RUNNING})
_LIVE_NODE_STATES = _UNSETTLED_NODE_STATES | {WorkflowNodeState.WAITING}
# How many failed calls the first line of a report names; `get_workflow` lists them all.
_MAX_FAILED_NAMED = 10
# The states in which a run's attempt may submit calls: a run that is starting names no attempt that may.
_SERVED_RUN_STATES = frozenset({WorkflowRunState.RUNNING, WorkflowRunState.WAITING})
_SHELL_COMMAND = ScriptToolGrant("shell", "run_shell_command")
_MAX_LABEL_CHARS = 80
# The owner of the tasks that admit calls: they outlive their requests, and a run's end waits for them, not stops them.
_ADMISSIONS = "admissions"
# How the engine's wait for an agent with a timeout is named, before its job's ID.
_TIMED = "timed:"
_ERROR_TAIL_BYTES = 4096
_NO_RETURN = "The script ended without returning from main(args)."
_ABANDONED = "The call that waited was cancelled."
_ATTEMPT_GONE = "The record of the run's attempt is gone."
_CHANGED_WITHOUT_EDIT = (
    "Call n{seq} changed between attempts without a script edit. It probably contains the time or a random value; "
    "use now() or random() from mindroom.workflow."
)
_TAKEN = "Run {} was resumed by another call."
_OWNER_NOT_READY = "The run's agent is not ready in the run's room yet. Try again in a moment."
_IDLE = "Stopped resuming after {attempts} attempts that finished nothing. Fix the script and call resume_workflow."
# How many attempts in a row an interrupted run is given on its own while none of them finishes a call.
_MAX_IDLE_RESUMES = 5
_OUT_OF_TIME = "The run used its {hours:g} hours of working time. resume_workflow continues it."
# How often a running run's working time is saved, so a primary that dies loses at most this much of the count.
_CHARGE_SECONDS = 60.0
_INTERRUPTED: dict[str, object] = {"kind": "interrupted", "message": "The call was interrupted before its tool ran."}
_NO_AGENTS = "A workflow runs agents as background tool jobs. Set background_tool_jobs.enabled: true."
_NAME_LABELS = 'name="{name}" only labels the node. Use agent="{name}" to run that agent.'
_STOPPED_JOB_STATUSES = frozenset({"cancel_requested", "cancelled", "denied"})
_NOT_CONTINUED_YET = "The agent of this call has not been continued yet after a restart. Try again in a moment."
_RUN_ENDED = "The run ended before the agent did."
_NO_SESSIONS = 'A workflow script cannot call delegate.{function} yet. Start a fresh agent with agent() or tool("delegate", "run_subagent", ...).'
_FILTERED_TURN = "A turn that is limited to some of its agent's tools cannot start or resume a workflow."
# The node of the script's tool call that the current task runs, for a call of the `workflow` toolkit made in it.
_CALLING_NODE: ContextVar[tuple[str, str, int] | None] = ContextVar("workflow_calling_node", default=None)


def calling_node() -> tuple[str, str, int] | None:
    """Return the run, the key and the generation of the script's tool call this context runs in, if it runs in one."""
    return _CALLING_NODE.get()


def nested_key(outer: tuple[str, str, int], source: bytes, args: object) -> str:
    """Return the key of a run inside a run: the call it is made in, its script and its arguments, and no call ID.

    A call that is made again under another ID, in the same turn or in a continued one, finds the run it started
    by this key: it waits for a run that still runs, and gets the report of one that ended.
    """
    identity = [*outer, hashlib.sha256(source).hexdigest(), args]
    return "nested:" + hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def ceiling(context: ToolRuntimeContext, *saved: Mapping[str, object] | None) -> dict[str, object] | None:
    """Return the persona a run is saved with: what the turn in *context* and every persona of *saved* all keep.

    *saved* is the run's own persona when the turn resumes it, and its outer run's when the turn starts it inside
    that run. A run so never has more than the turn that started it, nor more than a narrower one that resumed it.
    A turn that a filter narrows keeps a list no persona holds, so it is refused.
    """
    if context.tool_function_filter is not None:
        raise WorkflowRunStoreError(_FILTERED_TURN)
    kept = context.persona
    for other in (SubagentPersona.from_dict(persona) for persona in saved if persona is not None):
        kept = (
            other
            if kept is None
            else replace(
                kept,
                tools=_both(kept.tools, other.tools, toolkits=True),
                skills=_both(kept.kept_skills, other.kept_skills),
                knowledge=_both(kept.kept_knowledge, other.kept_knowledge),
            )
        )
    return asdict(kept) if kept is not None else None


def _both(
    one: tuple[str, ...] | None,
    other: tuple[str, ...] | None,
    *,
    toolkits: bool = False,
) -> tuple[str, ...] | None:
    """Return the names both lists hold, where None holds every name and a toolkit holds each of its functions."""
    if one is None or other is None:
        return other if one is None else one
    names = dict.fromkeys((*one, *other))
    return tuple(
        name
        for name in names
        if all(name in side or (toolkits and name.partition(".")[0] in side) for side in (one, other))
    )


# How long a waiter sleeps before it reads the store again, with or without a wake.
_RECHECK_SECONDS = 1.0
# How long a tool call above its attempt's calls per minute waits before it asks for room again.
_RATE_WAIT_SECONDS = 1.0
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


@dataclass(slots=True)
class _Places:
    """The places among one run's working calls: at most `limit` agent and tool calls of the run work at one time.

    A call holds its place from its start to its end, except while it waits for a person. The calls that found no
    place wait in `queued`, saved as such, in the order they came, each with what starts it.
    """

    limit: int
    working: set[str] = field(default_factory=set)
    queued: deque[tuple[str, Callable[[], Awaitable[object]]]] = field(default_factory=deque)


# What an attempt starts with: the live context and limits of its run's owner.
type _Admission = tuple[ToolRuntimeContext, WorkflowLimits]
# What the saved owner of a run has now in the run's conversation: that, once the owner's access is confirmed;
# "pending" while it cannot be checked yet, "revoked" when it is gone.
type ResumeAccess = _Admission | Literal["pending", "revoked"]


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
        # What the saved owner of a run may start an attempt with now; set by the runtime that knows the live bots.
        self.owner_access: Callable[[WorkflowRun], ResumeAccess] = lambda _run: "pending"
        # What agent turns run on, None while there is no job runtime; set by the runtime that knows it.
        self.agents: Callable[[], AgentRuntime | None] = lambda: None
        # The attempts this engine started, each with the epoch its run got when it began: all it may ever write
        # with. No other attempt is served: one an earlier primary left is a leftover process.
        self._launched: dict[str, int] = {}
        # The runs whose wait for an attempt was logged as refused, so the tries that follow say nothing.
        self._refused: set[str] = set()
        # The lanes in which a call ran in each attempt.
        self._dirty: dict[str, DirtyLanes] = {}
        # When each running run's working time was last charged, by the engine's clock.
        self._charged: dict[str, float] = {}
        # One per run with a waiter or a launch in progress: polls and `wait` sleep on it, a launch holds its lock.
        self._changed: WeakValueDictionary[str, asyncio.Condition] = WeakValueDictionary()
        self._tasks: dict[str, set[asyncio.Task[Any]]] = {}
        # Which calls of each run work now and which wait for a place, counted here and not from the saved nodes: a
        # node that an ended attempt left running holds no place until the attempt that reaches it waits for it.
        self._places: dict[str, _Places] = {}
        self._attending: Counter[str | None] = Counter()
        self._inline_runs: dict[str, str] = {}
        self._closed = False

    def admit(
        self,
        run: WorkflowRun,
        caller: ToolRuntimeContext | None = None,
        limits: WorkflowLimits | None = None,
    ) -> _Admission | str | None:
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
        access = self.owner_access(run)
        if not isinstance(access, tuple):
            return NO_ACCESS if access == "revoked" else None
        if caller is not None and not replay and place == (caller.room_id, caller.resolved_thread_id):
            access = (caller, limits or WorkflowLimits())
        if run.state is WorkflowRunState.STARTING and run.runtime_seconds_left == 0:
            return _OUT_OF_TIME.format(hours=access[1].max_runtime_hours)
        return _IDLE.format(attempts=_MAX_IDLE_RESUMES) if replay and run.idle_resumes >= _MAX_IDLE_RESUMES else access

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
        if self._closed:
            return self.store.get_run(run_id)
        launch = self._spawn(_ADMISSIONS, self._launch(run_id, caller, limits, owner, epoch))
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
        async with self._condition(run_id):
            run = self.store.get_run(run_id)
            # A reserved run gets one attempt from one launch; a run that was launched or has ended gets none, a
            # stopping engine starts none, and neither does a launcher whose run moved on to another owner.
            moved = owner not in (None, run.owner) or epoch not in (None, run.epoch)
            waits = run.state is WorkflowRunState.STARTING and not self._closed and not moved
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
        return await self._end_run(run, state, cast("str", refusal))

    async def _begin(self, run: WorkflowRun, attempt: ScriptRunRecord, hours: float) -> WorkflowRun:
        """Name the started *attempt* as the next one of *run*; the caller holds the run's lock."""
        # Before the run reads `running`, so whoever sees that state sees its time counted from here.
        self._charged[run.run_id] = self._clock()
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
            self._charged.pop(run.run_id, None)
        else:
            self._launched[attempt.run_id] = launched.epoch
            self._spawn(run.run_id, self._watch_time(run.run_id, launched.epoch, hours))
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
        async with self._condition(run_id):
            run = self.store.get_run(run_id)
        while run.state not in TERMINAL_RUN_STATES and owner in (None, run.owner) and epoch in (None, run.epoch):
            outcome: tuple[WorkflowRunState, str | None] = (state, reason)
            # Before anything of the run is stopped: the place a stopped call gives up starts no queued one.
            self._places.pop(run_id, None)
            # A run that waits for its next attempt has no process: how its last one ended says nothing about this stop.
            if run.script_run_id is not None and run.state is not WorkflowRunState.STARTING:
                await self.scripts.revoke(run.script_run_id, reason=reason)
                attempt = await self.scripts.reconcile_durable(run_id=run.script_run_id)
                if attempt.state in TERMINAL_SCRIPT_RUN_STATES:
                    # The same rule as the maintenance pass: an attempt that ended before the stop ends as it did.
                    outcome = _run_outcome(self.store.get_run(run_id), attempt, stopped=state)
            run = await self._end_run(run, *outcome)
        return run

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
    ) -> frozenset[str]:
        """Check a resume before anything changes, and return the keys of the calls *rerun* names.

        A live run is refused, an ID that names no call of the run is refused by name, and so is a resume that
        `admit` gives no attempt now.
        """
        run = self.store.get_run(run_id)
        if run.state not in TERMINAL_RUN_STATES:
            msg = f"Run {run_id} is still running. Cancel it first."
            raise WorkflowRunStoreError(msg)
        try:
            keys = frozenset(self._node(run_id, str(node_id)).key for node_id in rerun)
        except LookupError as exc:
            raise WorkflowRunStoreError(str(exc)) from exc
        if not isinstance(admitted := self.admit(run, caller, limits), tuple):
            raise WorkflowRunStoreError(admitted or _OWNER_NOT_READY)
        return keys

    async def reopen(
        self,
        run_id: str,
        context: ToolRuntimeContext,
        *,
        source: str | None = None,
        rerun: Sequence[str] = (),
        limits: WorkflowLimits | None = None,
        owner: str | None = None,
        job_id: str | None = None,
        epoch: int | None = None,
        persona: Mapping[str, object] | None = None,
    ) -> tuple[WorkflowRun, bool]:
        """Make an ended run ready for its next attempt, with the full working time; `launch` then starts it.

        The saved calls stay: the next attempt replays the script, with *source* when given, and reuses them, except
        the calls *rerun* names. The answer has the form of a reservation that found its run.

        Nothing changes before the resume takes the run (`resumable`), and it takes it in one write: the run becomes
        *owner*'s, the invocation that resumes it, with *job_id* when that is a job, names no attempt, and the calls
        of *rerun* are marked. A run that is *owner*'s already was taken by this same invocation, delivered again,
        and is returned as it is. *epoch* is the one the run had when the resume was accepted: a run that another
        resume took since, also one whose attempt never started, is refused, as is one over its owner's limit of
        live runs. A job was accepted with its request, so it waits here while `admit` cannot answer yet. *persona*
        is that of the turn the resume was accepted from: the run keeps what it, *context* and the run all keep.
        """
        limits = limits or WorkflowLimits()
        run = self.store.get_run(run_id)
        if owner is not None and run.owner == owner:
            return run, False
        while job_id is not None and self.admit(run, context, limits) is None:  # noqa: ASYNC110 - access has no event
            await asyncio.sleep(_RECHECK_SECONDS)
        text = run.source if source is None else source
        take = partial(
            self.store.update_run,
            run_id,
            run.epoch if epoch is None else epoch,
            when=TERMINAL_RUN_STATES,
            rerun=self.resumable(run_id, context, rerun, limits),
            max_active=limits.max_concurrent_runs,
            state=WorkflowRunState.STARTING,
            source=text,
            source_digest=hashlib.sha256(text.encode()).hexdigest(),
            persona=ceiling(context, persona, run.persona),
            previous_source_digest=run.source_digest,
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
            async with self._condition(run_id):
                # Waited for also by a caller that is cancelled here, so the run it finds says whether it took it.
                reopened = await wait_for_future_until_complete(asyncio.ensure_future(asyncio.to_thread(take)))
        except SupersededAttemptError:
            raise WorkflowRunStoreError(_TAKEN.format(run_id)) from None
        return reopened, False

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
        condition = self._condition(run_id)
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
                    with suppress(TimeoutError):
                        await asyncio.wait_for(condition.wait(), _RECHECK_SECONDS)
            if launch is None or run.state in TERMINAL_RUN_STATES:
                return run
            if (await launch()).state is WorkflowRunState.STARTING:
                await asyncio.sleep(_RECHECK_SECONDS)

    async def attend(
        self,
        origin: str,
        reserve: Callable[[], Awaitable[tuple[WorkflowRun, bool]]],
        context: ToolRuntimeContext,
        limits: WorkflowLimits,
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
            run, _ = await wait_for_future_until_complete(reserving)
            # The call starts the run's attempt as a job's operation does, again while `admit` cannot answer yet.
            return await self.wait(run.run_id, partial(self.launch, run.run_id, context, limits, owner=origin), origin)
        finally:
            if not reserving.cancelled() and not reserving.exception():
                self._inline_runs[origin] = reserving.result()[0].run_id
            self._attending -= Counter({origin: 1})
            stopping = self._closed or context.orchestrator is None or not context.orchestrator.running
            if not self._attending[origin] and (run_id := self._inline_runs.pop(origin, None)) and not stopping:
                # A run that ended is left as it ended.
                await run_coroutine_until_complete(self.cancel(run_id, reason=_ABANDONED, owner=origin))

    def attended(self, owner: str | None) -> bool:
        """Return whether a call of this primary waits for the run it owns as *owner*, and so starts its attempts."""
        return self._attending[owner] > 0

    def run_usage(self, run_id: str) -> dict[str, Any]:
        """Return what a run used so far and what its current attempt cost, with the runs inside it, each once.

        A nested run is part of the attempt that started it.
        """
        run = self.store.get_run(run_id)
        used: dict[str, Any] = dict(self.store.usage_totals(run_id))
        used["attempt_cost_usd"] = self.store.usage_totals(run_id, attempt=run.attempt)["cost_usd"]
        for child in self.store.child_runs(run_id):
            inner = self.run_usage(child.run_id)
            for name in ("input_tokens", "output_tokens", "tool_calls", "cost_usd"):
                used[name] += inner[name]
            used["cost_unknown"] = used["cost_unknown"] or inner["cost_unknown"]
            group = self.store.get_node(run_id, f"{child.parent_node_key}/{child.run_id}")
            if group is not None and group.attempt == run.attempt:
                used["attempt_cost_usd"] += inner["cost_usd"]
        return used

    def status(self, run_id: str) -> tuple[WorkflowRun, list[WorkflowNode]]:
        """Return the run and its current nodes."""
        return self.store.get_run(run_id), self.store.nodes(run_id)

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
        state, error = _run_outcome(self.store.get_run(run.run_id), attempt)
        if replay and state is WorkflowRunState.INTERRUPTED and error in AUTO_RESUME_REASONS:
            # The attempt is over and the run is not: its nodes are settled, and the next attempt runs the same script.
            same_script = {"previous_source_digest": run.source_digest}
            return await self._end_run(run, WorkflowRunState.STARTING, None, finished_at=None, **same_script)
        return await self._end_run(run, state, error)

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
        return await asyncio.shield(self._spawn(_ADMISSIONS, self._admit(run, attempt, request)))

    async def _admit(self, asked: WorkflowRun, attempt: ScriptRunRecord, request: NodeRequest) -> NodeReceipt:
        # Held from the lookup to the dispatch: two requests with one key cannot both find it free, and the run
        # cannot end in between.
        async with self._condition(asked.run_id):
            return await self._admit_gated(self.store.get_run(asked.run_id), asked.epoch, attempt, request)

    async def _admit_gated(  # noqa: C901, PLR0912, PLR0915
        self,
        run: WorkflowRun,
        epoch: int,
        attempt: ScriptRunRecord,
        request: NodeRequest,
    ) -> NodeReceipt:
        """Decide and save one call for the attempt that began with *epoch*, which every write here names."""
        key, kind, meta = request.key, request.kind, request.meta
        if run.epoch != epoch or run.state not in _SERVED_RUN_STATES:
            # Before anything is kept for the attempt: one that the run left behind leaves nothing behind here.
            msg = f"Run {run.run_id} takes no more calls from attempt {attempt.run_id}."
            raise SupersededAttemptError(msg)
        lanes = self._dirty.setdefault(attempt.run_id, DirtyLanes())
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
        if saved is not None and action != "return" and saved.state in _LIVE_NODE_STATES:
            # Left live by an attempt that ended: its handle says how it ended, or that its job still runs, before
            # any rule may run it again. The call that replaces a node whose job still runs stops that job.
            live = await self._reconcile(run, saved, epoch)
            left, saved = saved, self.store.get_node(run.run_id, key)
            action = decision(saved, live=live).action
            if live and action == "run":
                await self._reconcile(run, left, epoch, stop=True)
        if saved is not None and action != "run":
            # Reused: the node is this attempt's now, and a fan-out whose items changed dirties what follows it.
            stamp: dict[str, object] = {"attempt": run.attempt, "stale": False}
            if regrouped := members != saved.members:
                stamp["members"] = members
                # What follows the group is dirty and no member lane is, which is what marking the group does.
                lanes.mark(key)
            restamp = partial(self.store.update_node, run.run_id, key, saved.generation, epoch, dirty_after=regrouped)
            await asyncio.to_thread(partial(restamp, **stamp))
            if action == "raise_indeterminate":
                # It may have run, so nothing is dispatched until a resume names it.
                return replace(_receipt(saved), state=WorkflowNodeState.FAILED)
            if action == "attach":
                # The job an earlier attempt started still runs: this attempt waits for it, and starts none. The run
                # waits for a person if that job does, which the attempt's start did not know.
                self._wait_for_agent(run, saved, epoch)
                await self._restate(run.run_id)
            return replace(_receipt(saved), cached=action == "hit")
        # The completed node this call replaces although no rule before it said so: the call itself changed, or it
        # runs on every resume.
        completed = saved is not None and saved.state is WorkflowNodeState.COMPLETED and not (dirty or rerun)
        previous = saved if completed else None
        changed = previous is not None and previous.digest != digest
        if previous is not None and changed and cached and run.source_digest == run.previous_source_digest:
            notice = _CHANGED_WITHOUT_EDIT.format(seq=previous.seq)
            await asyncio.to_thread(self.store.append_log, run.run_id, epoch, notice)
        lane, index = split_key(key)
        generation = 1 if saved is None else saved.generation + 1
        # A script's `tool("delegate", ...)` is the agent call it stands for, with a gate before it.
        delegated = kind == "tool" and request.args.get("toolkit") == "delegate"
        shown, asked = ("agent", delegated_request(_tool_grant(request.args)[1])) if delegated else (kind, request.args)
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
            # The label goes into the job, the room and the run's view: redacted whole, then cut.
            label=redact_sensitive_text(
                str(meta.get("label") or _default_label(shown, asked)),
                max_length=_MAX_LABEL_CHARS,
            ),
            name=cast("str | None", meta.get("name")),
            phase=cast("str | None", meta.get("phase")),
            members=members,
            cached=cached,
            started_at=utc_timestamp(),
        )
        if shown == "agent":
            lanes.mark(key)
            start = partial(self._enter, run, node, partial(self._dispatch_agent, run, timeout=_timeout(meta)))
            return await (self._ask_gate(run, attempt, node, *_tool_grant(request.args)) if delegated else start())
        if kind == "tool":
            same_as = None
            if previous is not None and not cached:
                # A check that runs on every resume, also one whose arguments were edited, in a lane that was clean.
                same_as = result_digest(await asyncio.to_thread(self.store.whole_result, previous))
            start = partial(self._dispatch_tool, run, attempt, lanes=lanes, same_as=same_as)
            return await self._enter(run, node, start, dirty_after=same_as is None)
        if kind == "value":
            node = replace(node, result=_value(request.args))
        elif kind == "error":
            # What the script's own code raised in a lane of a fan-out: nothing ran, and the run shows where.
            message = f"{request.args.get('type')}: {request.args.get('message')}"
            node = replace(node, state=WorkflowNodeState.FAILED, error={"kind": "error", "message": message})
        elif kind != "group":
            msg = f"This MindRoom cannot run a '{kind}' call in a workflow yet."
            raise WorkflowRunStoreError(msg)
        # A value, a group and an error have ended when they are submitted, so no restart finds one half done. What earlier
        # attempts left after the call runs again in any attempt, which this one remembers and the same write saves.
        lanes.mark(key)
        put = partial(self.store.put_node, replace(node, ended_at=node.started_at), epoch, dirty_after=True)
        return _receipt(await asyncio.to_thread(put))

    async def _enter(
        self,
        run: WorkflowRun,
        node: WorkflowNode,
        dispatch: Callable[..., Awaitable[NodeReceipt]],
        *,
        dirty_after: bool = True,
    ) -> NodeReceipt:
        """Start a call that works, an agent's or a tool's, if its run has a place for it, or save it as queued.

        A queued call has started nothing and names nothing that could have: it starts when a place is free, in the
        order the calls came. The caller holds the run's lock.
        """
        places = self._places.setdefault(run.run_id, _Places(max(1, run.max_concurrent)))
        if len(places.working) < places.limit:
            places.working.add(node.key)
            try:
                return await dispatch(node)
            except BaseException:
                # A call whose node names its work by now keeps its place, for the wait that settles it.
                saved = self.store.get_node(run.run_id, node.key)
                if saved is None or saved.generation != node.generation or saved.handle is None:
                    self._leave(run.run_id, node.key)
                raise
        if node.kind == "tool":
            _tool_grant(node.request)
        queued = replace(node, state=WorkflowNodeState.QUEUED)
        queued = await asyncio.to_thread(partial(self.store.put_node, queued, run.epoch, dirty_after=dirty_after))
        places.queued.append((queued.key, partial(dispatch, queued, queued=True)))
        # A place that became free while the node was saved is taken now.
        self._leave(run.run_id, None)
        return _receipt(queued)

    def _leave(self, run_id: str, key: str | None) -> None:
        """Give up the place of the call at *key*, which ended or waits for a person, and start the calls there is room for."""
        places = self._places.get(run_id)
        if places is None:
            return
        places.working.discard(key or "")
        while places.queued and len(places.working) < places.limit and not self._closed:
            queued, start = places.queued.popleft()
            places.working.add(queued)
            self._spawn(run_id, self._start_queued(run_id, queued, start))

    async def _start_queued(self, run_id: str, key: str, start: Callable[[], Awaitable[object]]) -> None:
        """Start a call that waited for a place, under the run's lock, again while its agent cannot be decided yet."""
        try:
            while True:
                try:
                    async with self._condition(run_id):
                        await start()
                    break
                except ScriptRuntimeUnavailableError:
                    # The bot of the call's agent is still starting, or its owner's access is not known yet.
                    await asyncio.sleep(_RECHECK_SECONDS)
        except SupersededAttemptError:
            # The run moved on: what it left queued is the next attempt's to run.
            self._leave(run_id, key)
        except BaseException:
            node = self.store.get_node(run_id, key)
            if node is None or node.handle is None:
                self._leave(run_id, key)
            raise
        await self._notify(run_id)

    async def _ask_gate(
        self,
        run: WorkflowRun,
        attempt: ScriptRunRecord,
        node: WorkflowNode,
        grant: ScriptToolGrant,
        arguments: dict[str, object],
    ) -> NodeReceipt:
        """Save the agent call a script made through `tool()`, and ask the broker whether that tool call may be made.

        The node is saved as a queued one is, with no handle: nothing is started before the gate answers, so a
        restart or a stop meanwhile finds a call that never ran. The caller holds the run's lock.
        """
        if grant.function_name != FRESH_AGENT_FUNCTION:
            refused = {"kind": "refused", "message": _NO_SESSIONS.format(function=grant.function_name)}
            node = replace(node, state=WorkflowNodeState.FAILED, error=refused, ended_at=node.started_at)
            return _receipt(await asyncio.to_thread(partial(self.store.put_node, node, run.epoch, dirty_after=True)))
        node = replace(node, state=WorkflowNodeState.QUEUED)
        node = await asyncio.to_thread(partial(self.store.put_node, node, run.epoch, dirty_after=True))
        gate = partial(
            self.broker.execute_for_workflow,
            attempt,
            call_id=uuid5(NAMESPACE_OID, f"{run.run_id}:{node.key}:{node.generation}").hex,
            grant=grant,
            arguments=arguments,
            waits=partial(self._node_waits, node, run.epoch),
            gate_only=True,
        )
        self._spawn(run.run_id, self._start_gated(run, node, gate))
        return _receipt(node)

    async def _start_gated(
        self,
        run: WorkflowRun,
        node: WorkflowNode,
        gate: Callable[[], Awaitable[ScriptCallRecord]],
    ) -> None:
        """Start the agent of a call the broker let pass, in its turn among the run's calls; a refusal is its end."""
        try:
            outcome = _call_outcome(node, await _within_rate(gate))
        except (ScriptRunStoreError, ScriptRuntimeUnavailableError) as exc:
            outcome = WorkflowNodeState.FAILED, None, {"kind": "failed", "message": str(exc)}
        async with self._condition(run.run_id):
            if outcome[0] is WorkflowNodeState.COMPLETED:
                places = self._places.setdefault(run.run_id, _Places(max(1, run.max_concurrent)))
                # A call whose approval came counts as working since then, and has no place yet.
                places.working.discard(node.key)
                start = partial(self._dispatch_agent, run, node, timeout=None, queued=True)
                places.queued.append((node.key, start))
                self._leave(run.run_id, None)
            else:
                settle = partial(self.store.settle_node, run.run_id, node.key, node.generation, run.epoch)
                with suppress(SupersededAttemptError):
                    await asyncio.to_thread(partial(settle, state=outcome[0], error=outcome[2]))
        await self._notify(run.run_id)

    async def nest(self, child: WorkflowRun) -> None:
        """Show the nested run *child* in the run it is in, as a group under the call that started it.

        The group names the run, so the end of the outer run ends it, and it is part of the attempt it began in.
        """
        outer = self.store.get_run(str(child.parent_run_id))
        group = WorkflowNode(
            run_id=outer.run_id,
            key=f"{child.parent_node_key}/{child.run_id}",
            kind="group",
            lane=str(child.parent_node_key),
            idx=0,
            digest=child.source_digest,
            request={"op": "workflow"},
            state=WorkflowNodeState.RUNNING,
            handle=DispatchHandle("run", child.run_id, dispatched=True),
            attempt=outer.attempt,
            label=child.title or "workflow",
            group_key=child.parent_node_key,
            started_at=utc_timestamp(),
            links={"child_run_id": child.run_id},
        )
        with suppress(SupersededAttemptError):
            await asyncio.to_thread(self.store.put_node, group, outer.epoch)

    async def nested_call(
        self,
        context: ToolRuntimeContext,
        outer: tuple[str, str, int],
        call_id: str,
        arguments: Mapping[str, Any],
    ) -> str | None:
        """Return the run inside a run that the `start_workflow` call *call_id* of an agent's turn provably started.

        The proof is the call as it was saved with its run when it reserved it, for the script and the arguments
        the call names now: a provider may use one call ID twice in a turn, and a script file may have changed
        since. Without it the answer is None, and nobody can say what the call did.
        """
        try:
            text = await self.scripts.resolve_source(
                context,
                source=arguments.get("source"),
                path=arguments.get("path"),
            )
        except ValueError:
            return None
        key = nested_key(outer, text, arguments.get("args"))
        runs = await asyncio.to_thread(self.store.bound_runs, *outer, call_id)
        return next((run.run_id for run in runs if run.origin_job_id == key), None)

    async def nested_report(self, outer: tuple[str, str, int], run_id: str) -> str | None:
        """Wait for a run inside the call *outer* as the call that started it did, and return what it delivers.

        The wait is one more call of the run's owner: it starts the run's attempts while it waits, and when it is
        cancelled and no other call is left, the run is cancelled with it. None for a run that is not inside that call.
        """
        run = self.store.get_run(run_id)
        if (run.parent_run_id, run.parent_node_key) != outer[:2]:
            return None

        async def found() -> tuple[WorkflowRun, bool]:
            return run, False

        access = self.owner_access(run)
        attended = self.attend(str(run.owner), found, *access) if isinstance(access, tuple) else self.wait(run_id)
        return await self.delivery(await attended)

    async def delivery(self, run: WorkflowRun) -> str:
        """Return what an ended run delivers to whoever started it: its report, or why it did not finish.

        A run that completed although calls of it failed, which its script caught, says so in a first line.
        """
        text = run.outcome_text()
        if run.state is not WorkflowRunState.COMPLETED:
            return text
        nodes = await asyncio.to_thread(self.store.nodes, run.run_id)
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

    async def _dispatch_tool(
        self,
        run: WorkflowRun,
        attempt: ScriptRunRecord,
        node: WorkflowNode,
        *,
        lanes: DirtyLanes,
        same_as: str | None,
        queued: bool = False,
    ) -> NodeReceipt:
        """Save a tool node with the handle of its call, then run the call as the engine's own task.

        With *same_as*, the digest of its earlier result, the call's lane is dirty only if it answers something else;
        without, it ran because a rule said so, and its lane is dirty whatever it answers. The attempt's epoch is
        the run's here, and the call's task keeps it. A *queued* node is saved already and gets its handle.
        """
        grant, arguments = _tool_grant(node.request)
        call_id = uuid5(NAMESPACE_OID, f"{run.run_id}:{node.key}:{node.generation}").hex
        if same_as is None:
            lanes.mark(node.key)
        # Saved before the dispatch, so a crash after the tool ran reads the broker's receipt for this call.
        handle = DispatchHandle("broker", f"{attempt.run_id}:{call_id}", dispatched=False, same_as=same_as)
        node = replace(node, state=WorkflowNodeState.RUNNING, handle=handle)
        if queued:
            stamp = partial(self.store.update_node, run.run_id, node.key, node.generation, run.epoch)
            if not await asyncio.to_thread(partial(stamp, state=node.state, handle=handle)):
                # The node that waited for its place has an end by now: its run ended.
                raise SupersededAttemptError(_RUN_ENDED)
        else:
            node = await asyncio.to_thread(partial(self.store.put_node, node, run.epoch, dirty_after=same_as is None))
        kept = cast("list[str] | None", (run.persona or {}).get("tools"))
        call = partial(
            self.broker.execute_for_workflow,
            attempt,
            call_id=call_id,
            grant=grant,
            arguments=arguments,
            waits=partial(self._node_waits, node, run.epoch),
        )
        # The call has its task before anything else may fail: a saved call nobody runs would never end.
        self._spawn(run.run_id, self._run_tool_node(node, run.epoch, grant, call, kept, lanes))
        # A run that waited for a person works again from here.
        await self._restate(run.run_id)
        return _receipt(node)

    async def _dispatch_agent(
        self,
        run: WorkflowRun,
        node: WorkflowNode,
        *,
        timeout: float | None,  # noqa: ASYNC109 - the seconds of work the turn gets, not a deadline of this call.
        queued: bool = False,
    ) -> NodeReceipt:
        """Start the job of an agent node and wait for it as the engine's own task; a refusal is the node's end.

        The turn starts in the live context of the run's owner, as an attempt does. While that cannot be answered
        the request fails as a transport error does, which the script's SDK repeats, and a *queued* node, which is
        saved already, is tried again.
        """
        access = self.owner_access(run)
        if access == "pending":
            raise ScriptRuntimeUnavailableError(_OWNER_NOT_READY)
        try:
            if not isinstance(access, tuple) or (runtime := self.agents()) is None:
                raise AgentNodeError(NO_ACCESS if access == "revoked" else _NO_AGENTS)  # noqa: TRY301
            if node.name in access[0].current_config.agents and not node.request.get("agent"):
                notice = _NAME_LABELS.format(name=node.name)
                await asyncio.to_thread(self.store.append_log, run.run_id, run.epoch, notice)
            launch = partial(launch_child, access[0], run, node, run.epoch, store=self.store, jobs=runtime.jobs)
            node = await launch(timeout=timeout, queued=queued)
        except AgentNotReadyError as exc:
            raise ScriptRuntimeUnavailableError(str(exc)) from exc
        except AgentNodeError as exc:
            # Nothing ran. The node is saved already when the job runtime refused the job it names.
            end = {"state": WorkflowNodeState.FAILED, "error": {"kind": "refused", "message": str(exc)}}
            settle = partial(self.store.settle_node, run.run_id, node.key, node.generation, run.epoch, **end)
            if not await asyncio.to_thread(settle) and not queued:
                refused = replace(node, **end, ended_at=utc_timestamp())
                await asyncio.to_thread(partial(self.store.put_node, refused, run.epoch, dirty_after=True))
            self._leave(run.run_id, node.key)
            return _receipt(cast("WorkflowNode", self.store.get_node(run.run_id, node.key)))
        except BaseException:
            # The start failed in some other way, or the request was cancelled. A node that names its job already
            # is settled from that job, which ran nothing if it never started; the request itself fails, and the
            # script's SDK sends it again.
            saved = self.store.get_node(run.run_id, node.key)
            if saved is not None and saved.generation == node.generation and saved.handle is not None:
                self._wait_for_agent(run, saved, run.epoch)
            raise
        self._wait_for_agent(run, node, run.epoch)
        await self._restate(run.run_id)
        return _receipt(node)

    def _wait_for_agent(self, run: WorkflowRun, node: WorkflowNode, epoch: int) -> None:
        """Start the engine's wait for the job of an agent node, in place of an earlier attempt's wait for it.

        The wait for a turn with a timeout is not stopped with its attempt (`_end_run`), so somebody stops the turn
        on time between two attempts as well. What it reads then it cannot save, and leaves to the next wait.
        """
        assert node.handle is not None
        if self._closed:
            # A closed engine starts nothing: the job stays as it is, for the next start to continue or to read.
            return
        name = f"{_TIMED}{node.handle.ref}"
        for stale in [task for task in self._tasks.get(run.run_id, ()) if task.get_name() == name]:
            stale.cancel()
        if node.state is not WorkflowNodeState.WAITING:
            # An agent that an earlier attempt started works too: it holds a place, over the limit if it must.
            self._places.setdefault(run.run_id, _Places(max(1, run.max_concurrent))).working.add(node.key)
        task = self._spawn(run.run_id, self._run_agent_node(run, node, epoch))
        if node.handle.work_until is not None or node.handle.work_left is not None:
            task.set_name(name)

    async def _run_agent_node(self, run: WorkflowRun, node: WorkflowNode, epoch: int) -> None:
        """Wait for the job of an agent node and settle the node as its turn ended, then wake the run's waiters."""
        try:
            await self._settle_agent(run, node, epoch)
        finally:
            self._leave(node.run_id, node.key)
        await self._notify(node.run_id)

    async def _settle_agent(self, run: WorkflowRun, node: WorkflowNode, epoch: int, *, block: bool = True) -> None:
        """Settle an agent node from its job, once that has ended, for the attempt that began with *epoch*.

        The turn's usage and its answer are saved before the job's outcome is acknowledged, so a primary that dies
        in between reads the same job again. A run that moved on meanwhile reads the job itself. Without *block*,
        for a caller that holds the run's lock, a job that cannot be read now raises `AgentNotReadyError`.
        """
        assert node.handle is not None
        runtime = cast("AgentRuntime", self.agents())
        try:
            waits = partial(self._node_waits, node, epoch) if block else None
            (kind, text), usage, claim = await await_child(run, node, runtime, on_waiting=waits)
        except AgentNotReadyError:
            raise
        except Exception as exc:
            logger.exception("workflow_agent_outcome_unreadable", run_id=node.run_id, key=node.key)
            # What the turn did is unknown, so its call is not one to run again without being asked to.
            (kind, text), usage, claim = ("indeterminate", str(exc)), {}, None
        outcome: _Outcome = WorkflowNodeState.FAILED, None, {"kind": kind, "message": text}
        if kind == "completed":
            outcome = WorkflowNodeState.COMPLETED, text, None
        elif kind in ("cancelled", "indeterminate"):
            outcome = _indeterminate(node) if kind == "indeterminate" else (WorkflowNodeState.CANCELLED, *outcome[1:])
        state, result, error = outcome
        settle = partial(self.store.settle_node, node.run_id, node.key, node.generation, epoch)
        try:
            for turn, used in usage.items():
                add = partial(self.store.add_usage, node.run_id, node.key, node.generation, turn, run.attempt, used)
                await asyncio.to_thread(add)
            with suppress(SupersededAttemptError):
                await asyncio.to_thread(partial(settle, state=state, result=result, error=error))
                if claim is not None:
                    await runtime.jobs.acknowledge_wait(node.handle.ref, claim)
        finally:
            # An outcome this wait did not save, because the run moved on or the wait was cancelled, is the next
            # reader's. After the acknowledgement there is no claim left to give back.
            await runtime.jobs.release_wait(node.handle.ref, claim)

    async def _node_waits(self, node: WorkflowNode, epoch: int, on: str | None, left: float | None = None) -> None:
        """Show a node as waiting for a person, for what *on* says, or as running again with None; its run follows.

        *left* is the working time an agent's turn has left, which its handle keeps for whoever waits for it next.
        """
        changes: dict[str, object] = {"state": WorkflowNodeState.WAITING if on else WorkflowNodeState.RUNNING}
        if left is not None and node.handle is not None:
            until = None if on else time.time() + left
            changes["handle"] = replace(node.handle, work_left=left if on else None, work_until=until)
        stamp = partial(self.store.update_node, node.run_id, node.key, node.generation, epoch)
        with suppress(SupersededAttemptError):
            await asyncio.to_thread(partial(stamp, waiting_on=on, **changes))
        if on:
            # A call that waits for a person is no working call: the next one starts in its place.
            self._leave(node.run_id, node.key)
        elif (places := self._places.get(node.run_id)) is not None:
            places.working.add(node.key)
        await self._notify(node.run_id)

    async def _reconcile(self, run: WorkflowRun, node: WorkflowNode, epoch: int, *, stop: bool = False) -> bool:
        """Settle a node that an ended attempt left live from what its handle names; return whether its job still runs.

        A job that still runs is left as it is, for the attempt that reaches its node to wait for. With *stop* it is
        cancelled and its node with it: the run has ended, or another call replaces the node. The caller holds the
        run's lock.
        """
        settle = partial(self.store.settle_node, node.run_id, node.key, node.generation, epoch)
        if node.handle is None or node.handle.kind == "run":
            # Still queued for a place when its attempt or its run ended: it started nothing. Or the group of a run
            # inside this one, which does not outlive it and is reached only as it ends: the end of that run
            # settles the group, or this does.
            end = WorkflowNodeState.CANCELLED if stop else WorkflowNodeState.FAILED
            error = {"kind": "cancelled", "message": _RUN_ENDED} if stop else _INTERRUPTED
            await (self.cancel(node.handle.ref, reason=_RUN_ENDED) if node.handle else asyncio.sleep(0))
            await asyncio.to_thread(partial(settle, state=end, error=error))
            return False
        if node.handle.kind != "job":
            await asyncio.to_thread(self.settle_from_handle, node, epoch)
            return False
        runtime, job_id = self.agents(), node.handle.ref
        if runtime is None and not stop:
            # Nobody can say how the job stands: the script's SDK asks again.
            raise ScriptRuntimeUnavailableError(_NO_AGENTS)
        if (
            runtime is not None
            and not stop
            and any(job.job_id == job_id for job in runtime.jobs.awaiting_continuation())
        ):
            # A restart stopped the job, and whether it goes on is not decided: until then it is neither live nor
            # ended, and the call must not attach to a job that may yet end without having done anything.
            raise ScriptRuntimeUnavailableError(_NOT_CONTINUED_YET)
        jobs = runtime.jobs.retained_jobs() if runtime is not None else ()
        if runtime is None or any(job.job_id == job_id and job.status not in TERMINAL_STATUSES for job in jobs):
            if not stop:
                return True
            if runtime is not None:
                await runtime.jobs.request_cancel(job_id)
            cancelled = {"state": WorkflowNodeState.CANCELLED, "error": {"kind": "cancelled", "message": _RUN_ENDED}}
            await asyncio.to_thread(partial(settle, **cancelled))
            return False
        try:
            await self._settle_agent(run, node, epoch, block=False)
        except AgentNotReadyError as exc:
            if not stop:
                # The lock is not held while access resolves: the script's SDK asks again, and a Stop gets in.
                raise ScriptRuntimeUnavailableError(str(exc)) from exc
            # A Stop waits for nobody's access: the turn it may not read may have done anything.
            state, _, error = _indeterminate(node)
            await asyncio.to_thread(partial(settle, state=state, error=error))
        return False

    async def _restate(self, run_id: str) -> None:
        """Show the run as waiting while a call of it waits for a person and none runs, and as running otherwise.

        Time spent waiting is no working time, so the change charges what the run used up to it. The caller holds
        the run's lock. The clock and the time of the last charge are read and written here, on the loop: the
        thread that looks at the run is handed both.
        """
        now = self._clock()
        if await asyncio.to_thread(self._restated, run_id, (self._charged.get(run_id, now), now)):
            self._charged[run_id] = now

    def _restated(self, run_id: str, span: tuple[float, float]) -> bool:
        """Save the state the run has now and what it used in *span*; return whether its state changed."""
        with suppress(SupersededAttemptError, WorkflowRunStoreError):
            run = self.store.get_run(run_id)
            if run.state in _SERVED_RUN_STATES:
                states = self.store.node_states(run_id)
                waits = WorkflowNodeState.WAITING in states and not states & _UNSETTLED_NODE_STATES
                state = WorkflowRunState.WAITING if waits else WorkflowRunState.RUNNING
                if state is not run.state:
                    changes = {"state": state, **self._time_left(run, span)}
                    partial(self.store.update_run, run_id, run.epoch, when=_SERVED_RUN_STATES)(**changes)
                    return True
        return False

    async def node_receipt(self, attempt: ScriptRunRecord, node_id: str, *, wait: float) -> NodeReceipt:
        """Return a node's receipt, held up to *wait* seconds until the node has ended, while the attempt is served."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait
        while True:
            # Asked again after each wait: the end of the attempt ends its calls too, and those are not its to read.
            run = await self._current_run(attempt)
            async with (condition := self._condition(run.run_id)):
                node = self._node(run.run_id, node_id)
                remaining = deadline - loop.time()
                if node.state not in _LIVE_NODE_STATES or remaining <= 0:
                    return _receipt(node)
                with suppress(TimeoutError):
                    await asyncio.wait_for(condition.wait(), min(remaining, _RECHECK_SECONDS))

    def node_prompt(self, run_id: str, key: str | None, generation: int | None) -> str | None:
        """Return the prompt of the agent call that is *generation* of the node at *key*, while it waits for its agent.

        None once the call has an outcome, was replaced, or went with its run: nothing waits for its job any more.
        """
        node = self.store.get_node(run_id, key) if key else None
        live = node is not None and node.generation == generation and node.state in _LIVE_NODE_STATES
        if live and (runtime := self.agents()) is not None:
            # A Stop of the run's own job ends the run at its next pass: until then nothing of the run goes on.
            owner, jobs = self.store.get_run(run_id).job_id, runtime.jobs.retained_jobs()
            live = not any(job.job_id == owner and job.status in _STOPPED_JOB_STATUSES for job in jobs)
        prompt = node.request.get("prompt") if node is not None and live else None
        return prompt if isinstance(prompt, str) else None

    async def node(self, attempt: ScriptRunRecord, node_id: str) -> WorkflowNode:
        """Return one node of the attempt's run, for its whole result."""
        return self._node((await self._current_run(attempt)).run_id, node_id)

    async def append_log(self, attempt: ScriptRunRecord, text: str) -> None:
        """Add one progress line to the attempt's run."""
        run = await self._current_run(attempt)
        await asyncio.to_thread(self.store.append_log, run.run_id, run.epoch, text)

    async def finish(self, attempt: ScriptRunRecord, *, result: object, error: str | None) -> None:
        """Store what the script's `main` returned or raised; the run ends when the attempt's process has."""
        run = await self._current_run(attempt)
        report = partial(self.store.update_run, run.run_id, run.epoch, when={WorkflowRunState.RUNNING})
        await asyncio.to_thread(partial(report, result=result, error=error, finished_at=utc_timestamp()))
        self._spawn(run.run_id, self._observe_exit(attempt.run_id))

    def settle_from_handle(self, node: WorkflowNode, epoch: int) -> WorkflowNodeState:
        """Settle a node an ended attempt left unsettled from the broker receipt its handle names; return its state.

        The work is never done again here: a finished receipt is copied, and a call that never reached its tool fails
        as safe to run again. *epoch* is the run's as whoever settles the node found it: the attempt that reached
        the node again, or the look that ends the attempt.
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
        state, result, error = outcome
        settle = partial(self.store.settle_node, node.run_id, node.key, node.generation, epoch)
        settle(state=state, result=result, error=error, dirty_after=_changed(node, outcome))
        return state

    def _condition(self, run_id: str) -> asyncio.Condition:
        condition = self._changed.get(run_id)
        if condition is None:
            condition = self._changed[run_id] = asyncio.Condition()
        return condition

    async def _notify(self, run_id: str) -> None:
        """Wake the run's waiters after a call of it changed, and show whether the run waits for a person now."""
        async with (condition := self._condition(run_id)):
            await self._restate(run_id)
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
        if run.epoch != self._launched.get(attempt.run_id):
            msg = f"Attempt {attempt.run_id} is no longer the attempt of {run.run_id}."
            raise SupersededAttemptError(msg)
        return run

    def _node(self, run_id: str, node_id: str) -> WorkflowNode:
        seq = int(node_id[1:]) if node_id[1:].isdecimal() else 0
        # Only the ID a call is shown with names it: `n` and its sequence number, written as `get_workflow` writes it.
        node = self.store.node_by_seq(run_id, seq) if node_id == f"n{seq}" and seq < 2**63 else None
        if node is None:
            msg = f"Run {run_id} has no call {node_id}."
            raise LookupError(msg)
        return node

    async def _run_tool_node(
        self,
        node: WorkflowNode,
        epoch: int,
        grant: ScriptToolGrant,
        call: Callable[[], Awaitable[ScriptCallRecord]],
        kept: list[str] | None,
        lanes: DirtyLanes,
    ) -> None:
        """Run one tool call and settle its node; *kept* is the tool list of the narrowed agent that started the run."""
        outcome: _Outcome
        # A `start_workflow` this call makes starts a run inside this one, under this node.
        _CALLING_NODE.set((node.run_id, node.key, node.generation))
        try:
            _require_kept(kept, grant)
            outcome = _call_outcome(node, await _within_rate(call))
        except (ScriptRunStoreError, ScriptRuntimeUnavailableError) as exc:
            # Refused before any claim: a revoked attempt, a broker that takes no calls now.
            outcome = WorkflowNodeState.FAILED, None, {"kind": "failed", "message": str(exc)}
        finally:
            self._leave(node.run_id, node.key)
        if dirty := _changed(node, outcome):
            # Before the node shows its end, so the script's next call already finds the lane dirty.
            lanes.mark(node.key)
        await hold("before_settle")
        state, result, error = outcome
        settle = partial(self.store.settle_node, node.run_id, node.key, node.generation, epoch)
        # A run that moved on meanwhile reads the call's receipt itself.
        with suppress(SupersededAttemptError):
            await asyncio.to_thread(settle, state=state, result=result, error=error, dirty_after=dirty)
        await self._notify(node.run_id)

    async def _watch_time(self, run_id: str, epoch: int, hours: float) -> None:
        """Save the working time the attempt that began with *epoch* uses, and stop it when none is left."""
        left = self.store.get_run(run_id).runtime_seconds_left
        with suppress(SupersededAttemptError):
            while left is not None and left > 0:
                await asyncio.sleep(min(_CHARGE_SECONDS, left))
                run = self.store.get_run(run_id)
                if charge := self._time_left(run):
                    run = await asyncio.to_thread(partial(self.store.update_run, run_id, epoch, **charge))
                left = run.runtime_seconds_left
            if left is not None:
                # An attempt that had ended already is judged as the pass judges it, before the run is stopped.
                await self.settle(self.store.get_run(run_id), replay=True)
                out_of_time = _OUT_OF_TIME.format(hours=hours)
                await self.cancel(run_id, reason=out_of_time, state=WorkflowRunState.INTERRUPTED, epoch=epoch)

    def _time_left(self, run: WorkflowRun, span: tuple[float, float] | None = None) -> dict[str, object]:
        """Return, as a change to save, the working time *run* has left after what it used since the last charge.

        Only a running run uses any, up to where its script ended: time spent waiting for a person, starting or
        ended is not charged, and neither is the time until somebody looked at a script that had ended. Off the
        loop, *span* is the time of the last charge and now, which the caller read there and records itself.
        """
        since, now = span or (None, self._clock())
        if since is None:
            since, self._charged[run.run_id] = self._charged.get(run.run_id, now), now
        if run.state is not WorkflowRunState.RUNNING or run.runtime_seconds_left is None:
            return {}
        with suppress(ScriptRunNotFoundError):
            if ended := self.scripts.store.get_run(run.script_run_id or "").finished_at:
                now -= (datetime.now(UTC) - datetime.fromisoformat(ended)).total_seconds()
        return {"runtime_seconds_left": max(0.0, run.runtime_seconds_left - max(0.0, now - since))}

    async def _observe_exit(self, script_run_id: str) -> None:
        """End the run soon after its script reported: look for the exit instead of waiting for the next pass."""
        for delay in _EXIT_POLL_SECONDS:
            await asyncio.sleep(delay)
            if (await self.scripts.reconcile_durable(run_id=script_run_id)).state in TERMINAL_SCRIPT_RUN_STATES:
                # Only the run that reported: another run's ended attempt is the next pass's to judge.
                run = await asyncio.to_thread(self.store.run_for_script, script_run_id)
                if run is not None:
                    await self.settle(run)
                break

    async def _end_run(
        self,
        run: WorkflowRun,
        state: WorkflowRunState,
        error: str | None,
        **changes: object,
    ) -> WorkflowRun:
        """End *run*: stop its node tasks, read each unsettled node from its handle, store the state, wake waiters.

        With the state `starting` only the run's attempt is over. A run that has ended, or that moved on from the
        epoch *run* has, is returned as it is: what was decided about *run* says nothing about it.
        """
        condition = self._condition(run.run_id)
        # Held to the run's last write, so no call is admitted between the look at its nodes and its end.
        async with condition:
            current = self.store.get_run(run.run_id)
            if current.epoch != run.epoch or current.state in TERMINAL_RUN_STATES:
                return current
            ended = state in TERMINAL_RUN_STATES
            # Before any call of the run is stopped: the place it gives up starts no call that waited for one.
            self._places.pop(run.run_id, None)
            mine = self._tasks.get(run.run_id, set()) if not ended else self._tasks.pop(run.run_id, set())
            # The wait for a timed agent outlives the attempt: it stops the turn on time while no attempt waits.
            kept = () if ended else [task for task in mine if task.get_name().startswith(_TIMED)]
            tasks = mine - {asyncio.current_task(), *kept}
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            current = self.store.get_run(run.run_id)
            nodes = self.store.nodes(run.run_id, include_stale=True)
            for node in nodes:
                # An agent's job outlives an attempt, and so does a nested run: neither outlives its run.
                waits_for_job = node.handle is not None and node.handle.kind in ("job", "run")
                if node.state in _LIVE_NODE_STATES and (ended or not waits_for_job):
                    await self._reconcile(current, node, run.epoch, stop=ended)
            if state is WorkflowRunState.COMPLETED:
                # What the completed script no longer reaches leaves the run's current view.
                stale = stale_keys(nodes, current.attempt)
                await asyncio.to_thread(self.store.mark_stale, run.run_id, run.epoch, stale)
            ended_at = current.finished_at or utc_timestamp()
            changes = {"state": state, "error": error, "finished_at": ended_at, **self._time_left(current), **changes}
            attempt = current.script_run_id or ""

            def forget(written: asyncio.Future[WorkflowRun]) -> None:
                if not written.cancelled() and written.exception() is None:
                    self._dirty.pop(attempt, None)
                    self._launched.pop(attempt, None)

            write = partial(self.store.update_run, run.run_id, run.epoch, **changes)
            end = asyncio.ensure_future(asyncio.to_thread(write))
            # What is kept per attempt goes with the write: a task cancelled at the commit leaves none of it behind.
            end.add_done_callback(forget)
            ended = await asyncio.shield(end)
            self._charged.pop(run.run_id, None)
            self._refused.discard(run.run_id)
            condition.notify_all()
            if ended.state in TERMINAL_RUN_STATES and ended.parent_run_id is not None:
                await self._settle_group(ended)
        return ended

    async def _settle_group(self, child: WorkflowRun) -> None:
        """Show how the nested run *child* ended on its group in the run it is in.

        Only the group's row is written, under the outer run's epoch and without its lock, which the end of the
        outer run holds while it ends this one.
        """
        ends = {
            WorkflowRunState.COMPLETED: WorkflowNodeState.COMPLETED,
            WorkflowRunState.CANCELLED: WorkflowNodeState.CANCELLED,
        }
        end = ends.get(child.state, WorkflowNodeState.FAILED)
        error = None if end is WorkflowNodeState.COMPLETED else {"kind": child.state.value, "message": child.error}
        with suppress(SupersededAttemptError, WorkflowRunStoreError):
            outer = self.store.get_run(str(child.parent_run_id))
            key = f"{child.parent_node_key}/{child.run_id}"
            settle = partial(self.store.settle_node, outer.run_id, key, 1, outer.epoch, state=end, error=error)
            await asyncio.to_thread(settle)


async def _within_rate(call: Callable[[], Awaitable[ScriptCallRecord]]) -> ScriptCallRecord:
    """Make a call of the broker, again each moment while its attempt's minute has no room for it: nothing was claimed."""
    while True:
        try:
            return await call()
        except ScriptCallRateLimitError:
            await asyncio.sleep(_RATE_WAIT_SECONDS)


def _changed(node: WorkflowNode, outcome: _Outcome) -> bool:
    """Return whether a call that runs on every resume answered something else than before, which dirties what follows.

    Its handle holds the digest of the earlier result, compared whole and as JSON. The live call and the look at the
    receipt of a call whose attempt died both ask, and save the answer with the node's end.
    """
    same_as = node.handle.same_as if node.handle is not None else None
    state, result, _ = outcome
    return same_as is not None and not (state is WorkflowNodeState.COMPLETED and result_digest(result) == same_as)


def _tool_grant(request: dict[str, object]) -> tuple[ScriptToolGrant, dict[str, object]]:
    """Return the function a tool call's request names and its arguments; a request that names neither is refused."""
    arguments = request.get("arguments")
    if not isinstance(arguments, dict) or not all(
        isinstance(request.get(name), str) for name in ("toolkit", "function")
    ):
        msg = "A tool call needs a toolkit, a function and its arguments."
        raise WorkflowRunStoreError(msg)
    return ScriptToolGrant(str(request["toolkit"]), str(request["function"])), arguments


def _require_kept(kept: list[str] | None, grant: ScriptToolGrant) -> None:
    """Refuse a tool outside *kept*; checked at every call beside the owner's live tools, so a run never has more."""
    if kept is not None and not persona_allows(tuple(kept), grant.toolkit_name, grant.function_name):
        msg = f"{grant.toolkit_name}.{grant.function_name} is not a tool of the agent that started this run."
        raise ScriptRunStoreError(msg)


def _default_label(kind: str, args: dict[str, object]) -> object:
    """Return what a node is called when its script gave no label: its function, name, prompt or operation."""
    if kind == "agent":
        # Where the label goes the prompt does not: its first line stands for it.
        return args.get("name") or str(args.get("prompt") or kind).strip().partition("\n")[0]
    return (
        f"{args.get('toolkit')}.{args.get('function')}"
        if kind == "tool"
        else args.get("op") or args.get("type") or kind
    )


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
