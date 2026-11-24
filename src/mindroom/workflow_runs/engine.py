"""The workflow engine: a run's attempts, its tool nodes and their dispatch handles, and the run's state."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import secrets
import time
from collections import Counter, deque
from contextlib import suppress
from contextvars import ContextVar, copy_context
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import NAMESPACE_OID, uuid5
from weakref import WeakValueDictionary

from mindroom.background_tasks import run_coroutine_until_complete, wait_for_future_until_complete
from mindroom.delegation.personas import persona_allows
from mindroom.delegation.state import RUN_SUBAGENT, SubagentPersona
from mindroom.logging_config import get_logger
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
    AgentNodeError,
    AgentRuntime,
    await_child,
    delegated_request,
    first_prompt,
    launch_child,
    load_follow_up_child,
    schema_failure,
)
from mindroom.workflow_runs.faults import hold
from mindroom.workflow_runs.graph import node_label
from mindroom.workflow_runs.models import (
    LIVE_NODE_STATES,
    RECHECK_SECONDS,
    SERVED_RUN_STATES,
    TERMINAL_RUN_STATES,
    UNSETTLED_NODE_STATES,
    DispatchHandle,
    NodeReceipt,
    WorkflowNode,
    WorkflowNodeState,
    WorkflowRun,
    WorkflowRunState,
)
from mindroom.workflow_runs.notices import RETRY_SECONDS
from mindroom.workflow_runs.peers import (
    NodeRef,
    read_call,
    script_op,
    session_turn_message,
    withdraw_unread,
)
from mindroom.workflow_runs.peers import send as send_peer_message
from mindroom.workflow_runs.questions import MAX_OPTION_BYTES, MAX_OPTIONS, show_question
from mindroom.workflow_runs.recovery import AUTO_RESUME_REASONS, NO_ACCESS
from mindroom.workflow_runs.replay import (
    DIRTY_DIGEST,
    decide,
    may_have_written,
    node_digest,
    result_digest,
    split_key,
)
from mindroom.workflow_runs.saved import WORKFLOWS_DIR, validate_workflow_name
from mindroom.workflow_runs.store import SupersededAttemptError, WorkflowRunNotFoundError, WorkflowRunStoreError
from mindroom.workflow_runs.structured import (
    SCHEMA_TURNS,
    SchemaError,
    extract_json,
    validation_error,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence

    import nio

    from mindroom.delegation.state import DelegationChild
    from mindroom.script_runs.broker import ScriptToolBroker
    from mindroom.script_runs.manager import ScriptResourceProfileName, ScriptRunManager
    from mindroom.script_runs.models import ScriptRunRecord
    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from mindroom.workflow_runs.models import NodeRequest
    from mindroom.workflow_runs.store import WorkflowRunStore

logger = get_logger(__name__)

# How many failed calls the first line of a report names; `get_workflow` lists them all.
_MAX_FAILED_NAMED = 10
_SHELL_COMMAND = ScriptToolGrant("shell", "run_shell_command")
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
_CORRECTION = "Your answer did not match the schema: {error}. Reply with only the corrected fenced json block."
_NO_MATCH = "Agent '{name}' did not return JSON matching the schema after {turns} attempts. Last error: {error}."
_CORRECTING = "The agent of this call is correcting its answer. Try again in a moment."
_NOT_ASKED = "The question could not be posted in the run's thread yet. Try again in a moment."
_NOT_ANSWERED = "The run ended before the question was answered."
_OVER_BUDGET = "Stopped at the cost limit (${spent} of ${limit})."
_TOO_MANY_OPTIONS = "A question takes at most 20 options of at most 200 bytes each."
_NO_SESSIONS = 'A workflow script cannot call delegate.{function} yet. Start a fresh agent with agent() or tool("delegate", "run_subagent", ...).'
_SESSION_BUSY = "Session '{name}' is already in a turn. Wait for it or spawn another agent."
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


def _fits_ceiling(persona: SubagentPersona | None, cap: SubagentPersona) -> bool:
    """Return whether *persona* stays within *cap*, using the same intersection as `ceiling()`."""
    tools = None if persona is None else persona.tools
    skills = None if persona is None else persona.kept_skills
    knowledge = None if persona is None else persona.kept_knowledge
    return (
        _both(tools, cap.tools, toolkits=True) == tools
        and _both(skills, cap.kept_skills) == skills
        and _both(knowledge, cap.kept_knowledge) == knowledge
    )


# How long the end of a run waits for its agents' cancelled jobs, all together, before each node is settled as it
# stands: one whose job has not ended is one nobody could read.
_STOP_SECONDS, _STOP_POLL_SECONDS = 30.0, 0.05
_STOPPING = "The agent this call replaces is stopping. Try again in a moment."
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
    # What a run may be projected to cost before its card says it is large; 0 never says so for its cost.
    large_run_notice_usd: float = 10


@dataclass(slots=True)
class _Places:
    """The places among one run's working calls: at most `limit` agent and tool calls of the run work at one time.

    A call holds its place from its start to its end (except while waiting on peer messages or a child ask_user).
    Waking waiters take freed places before `queued` calls.
    """

    limit: int
    working: set[str] = field(default_factory=set)
    queued: deque[tuple[str, Callable[[], Awaitable[object]]]] = field(default_factory=deque)
    waking: int = 0


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
        self._clock = clock
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
        # The runs that are ending and wait, without their lock, for their agents to stop: they take no call.
        self._stopping: set[str] = set()
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

    def cancel_as_task(self, run_id: str, **how: Any) -> asyncio.Task[WorkflowRun]:  # noqa: ANN401 - as `cancel`
        """Stop a run as the engine's own task, for a caller with less time than the end of a run may take."""

        def failed(ended: asyncio.Task[WorkflowRun]) -> None:
            error = None if ended.cancelled() else ended.exception()
            if error is not None and not isinstance(error, WorkflowRunNotFoundError):
                logger.error("workflow_run_stop_failed", run_id=run_id, exc_info=error)

        task = self._spawn(run_id, self.cancel(run_id, **how))
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
            spent = self.run_usage(run_id)["cost_usd"]
            if _limit_reached(spent, effective_limit):
                msg = (
                    f"Run {run_id} has already spent ${_usd(spent)} (limit ${_usd(effective_limit)}). "
                    f"Pass max_cost_usd above ${_usd(spent)} to resume it."
                )
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
            async with self._condition(run_id):
                # Waited for also by a caller that is cancelled here, so the run it finds says whether it took it.
                reopened = await wait_for_future_until_complete(asyncio.ensure_future(asyncio.to_thread(take)))
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
                    await _wait_for_change(condition)
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
                if group.generation == 1:
                    used["attempt_cost_usd"] += inner["cost_usd"]
                elif child.state is not WorkflowRunState.STARTING:
                    used["attempt_cost_usd"] += inner["attempt_cost_usd"]
        return used

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
            raise ScriptRuntimeUnavailableError(_OWNER_NOT_READY)
        if isinstance(context, str):
            raise WorkflowRunStoreError(context)
        try:
            raw = await self.scripts.resolve_source(
                context,
                source=None,
                path=f"{WORKFLOWS_DIR}/{name}.py",
                create_workspace=False,
            )
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
        return await asyncio.shield(self._spawn(_ADMISSIONS, self._admit(run, attempt, request)))

    async def _admit(self, asked: WorkflowRun, attempt: ScriptRunRecord, request: NodeRequest) -> NodeReceipt:
        # Held from the lookup to the dispatch: two requests with one key cannot both find it free, and the run
        # cannot end in between.
        async with (condition := self._condition(asked.run_id)):
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
        if run.epoch != epoch or run.state not in SERVED_RUN_STATES or run.run_id in self._stopping:
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
            return saved, None, _receipt(await self._asked(run, saved, epoch))
        live, handle = False, None
        if saved.state in LIVE_NODE_STATES:
            live, handle = await self._reconcile(run, saved, epoch), saved.handle
            saved = cast("WorkflowNode", self.store.get_node(run.run_id, request.key))
        cached = request.meta.get("cached") is not False
        action = decide(saved, digest, cached=cached)
        if action != "run":
            return saved, None, await self._reuse_saved(run, epoch, saved, request.meta, members, action)
        if live and saved.kind == "ask":
            await self._close_card(run, saved, "cancelled")
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
        await self._stamp(saved, epoch, dirty_after=regrouped, **stamp)
        if action == "raise_indeterminate":
            return replace(_receipt(saved), state=WorkflowNodeState.FAILED)
        if action == "attach":
            if saved.kind == "ask":
                await self._asked(run, saved, epoch)
            else:
                self._wait_for_agent(run, saved, epoch)
            await self._restate(run.run_id)
        return replace(_receipt(saved), cached=action == "hit")

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
            return _receipt(await asyncio.to_thread(self.store.put_node, failed, epoch, dirty_after=True))
        if saved is not None and shown in ("agent", "turn", "send"):
            await asyncio.to_thread(withdraw_unread, self.store, run.run_id, key, saved.generation)
        if shown in ("agent", "turn"):
            if grant is not None:
                return await self._ask_gate(run, attempt, node, (grant, arguments))
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
            dispatch = partial(self._dispatch_agent, run, timeout=_timeout(meta), follow_up=follow_up)
            return await self._enter(run, node, dispatch)
        if grant is not None:
            cached = meta.get("cached") is not False
            same_as = (
                result_digest(await asyncio.to_thread(self.store.whole_result, previous))
                if previous is not None and not cached
                else None
            )
            start = partial(self._dispatch_tool, run, attempt, tool_call=(grant, arguments), same_as=same_as)
            return await self._enter(run, node, start, dirty_after=same_as is None)
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
            self._validate_question_options(request.args.get("options"))
            over = await self._check_budget(run)
            ask_node = replace(
                node,
                state=WorkflowNodeState.QUEUED if over else WorkflowNodeState.WAITING,
                waiting_on=None if over else "user",
                ended_at=None,
            )
            ask_node = await asyncio.to_thread(self.store.put_node, ask_node, epoch, dirty_after=True)
            if ask_node.state is WorkflowNodeState.WAITING:
                await self._restate(run.run_id)
            return _receipt(await self._asked(run, ask_node, epoch))
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
                    self._spawn(run.run_id, self._wait_inbox(run, wait_node, epoch, float(value)))
                    return _receipt(wait_node)
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
        return _receipt(await asyncio.to_thread(self.store.put_node, done_node, epoch, dirty_after=True))

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
                raise ScriptRuntimeUnavailableError(_OWNER_NOT_READY)
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

    def poster(self, run: WorkflowRun) -> nio.AsyncClient | None:
        """Return the client of the bot that serves *run*, which posts for it, or None while the run may not act."""
        context = self.authorize(run)
        return None if context is None or isinstance(context, str) else context.client

    async def _asked(self, run: WorkflowRun, node: WorkflowNode, epoch: int) -> WorkflowNode:
        """Send the card of a question that waits and has none yet, and return the node with it.

        While the card cannot be sent the request fails as a transport error does, which the script's SDK repeats.
        """
        if node.kind != "ask" or node.state is not WorkflowNodeState.WAITING or "event_id" in node.links:
            return node
        client = self.poster(run)
        event_id = None if client is None else await show_question(client, run, node)
        if event_id is None:
            raise ScriptRuntimeUnavailableError(_NOT_ASKED)
        links = {**node.links, "event_id": event_id}
        await self._stamp(node, epoch, links=links)
        return replace(node, links=links)

    async def _close_card(
        self,
        run: WorkflowRun,
        node: WorkflowNode,
        status: Literal["answered", "cancelled"],
        answer: str | None = None,
        retries: tuple[float, ...] = (),
    ) -> None:
        """Show on a question's card that it was answered or that nobody can answer it any more; best effort."""
        if (client := self.poster(run)) is not None:
            await show_question(client, run, node, status, answer, retries)

    async def ask_again(self, run_id: str) -> None:
        """Post the card of each question of a run that waits without one: a card that could not be sent then."""
        async with self._condition(run_id):
            run = self.store.get_run(run_id)
            for light in self.store.nodes(run_id, light=True) if run.state not in TERMINAL_RUN_STATES else ():
                node = self.store.get_node(run_id, light.key) if light.kind == "ask" else None
                with suppress(ScriptRuntimeUnavailableError, SupersededAttemptError):
                    await self._asked(run, node or light, run.epoch)

    @staticmethod
    def _validate_question_options(options: object) -> None:
        if options is None:
            return
        if not (isinstance(options, list) and options and all(isinstance(o, str) and o.strip() for o in options)):
            msg = "options must be a list of strings, such as ['Yes', 'No']."
            raise WorkflowRunStoreError(msg)
        if len(options) > MAX_OPTIONS or any(len(o.encode()) > MAX_OPTION_BYTES for o in options):
            raise WorkflowRunStoreError(_TOO_MANY_OPTIONS)

    async def _wait_child_question(self, run_id: str, ask_key: str) -> str:
        condition = self._condition(run_id)
        while True:
            async with condition:
                cur = await asyncio.to_thread(self.store.get_node, run_id, ask_key)
                run = await asyncio.to_thread(self.store.get_run, run_id)
                ended = run_id in self._stopping or run.state in TERMINAL_RUN_STATES
                if cur is None or cur.state is WorkflowNodeState.CANCELLED or ended:
                    raise WorkflowRunStoreError(_NOT_ANSWERED)
                if cur.state is WorkflowNodeState.COMPLETED:
                    return str(cur.result)
                if "event_id" in cur.links:
                    await _wait_for_change(condition)
                    continue
            posted = False
            with suppress(ScriptRuntimeUnavailableError, SupersededAttemptError):
                posted = "event_id" in (await self._asked(run, cur, run.epoch)).links
            if not posted:
                async with condition:
                    await _wait_for_change(condition)

    async def ask_child_question(
        self,
        run_id: str,
        parent: WorkflowNode,
        question: str,
        options: list[str] | None,
    ) -> str:
        """Post (or reattach to) an agent's own question card, release its slot while waiting, and return the answer."""
        self._validate_question_options(options)
        ask_key = f"{parent.key}?{parent.generation}:{result_digest([question, options])[:12]}"
        req: dict[str, object] = {"question": question, "options": options}
        condition, cancelled = self._condition(run_id), False
        async with condition:
            run, saved = self.store.get_run(run_id), self.store.get_node(run_id, ask_key)
            if saved is not None and saved.state in (WorkflowNodeState.COMPLETED, WorkflowNodeState.WAITING):
                if saved.attempt != run.attempt or saved.stale:
                    await self._stamp(saved, run.epoch, ignore=True, attempt=run.attempt, stale=False)
                ask_node = saved
            elif run.state in TERMINAL_RUN_STATES or run_id in self._stopping:
                raise WorkflowRunStoreError(_NOT_ANSWERED)
            else:
                gen = 1 if saved is None else saved.generation + 1
                ask_node = WorkflowNode(
                    run_id=run_id,
                    key=ask_key,
                    kind="ask",
                    lane=parent.lane,
                    idx=parent.idx,
                    digest=node_digest("ask", req),
                    request=req,
                    generation=gen,
                    state=WorkflowNodeState.WAITING,
                    waiting_on="user",
                    attempt=run.attempt,
                    label=node_label("ask", req),
                    phase=parent.phase,
                    started_at=utc_timestamp(),
                )
                ask_node = await asyncio.to_thread(self.store.put_node, ask_node, run.epoch, dirty_after=False)
            if ask_node.state is WorkflowNodeState.COMPLETED:
                return str(ask_node.result)
            self._drop_slot(run_id, parent.key)
            await self._restate(run_id)
            condition.notify_all()
        runtime, job_id = self.agents(), parent.links.get("job_id")
        wait_job = (
            (runtime.jobs, job_id)
            if runtime is not None and isinstance(job_id, str) and runtime.jobs.retained_job(job_id)
            else None
        )
        try:
            if wait_job is None:
                return await self._wait_child_question(run_id, ask_key)
            jobs, jid = wait_job
            await jobs.save_adapter(jid, wait="user")
            async with jobs.human_decision_wait(jid):
                return await self._wait_child_question(run_id, ask_key)
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            if not cancelled:
                # Cleared only on normal/error exit (not CancelledError), so shutdown keeps wait="user" for recovery.
                if wait_job is not None:
                    await wait_job[0].save_adapter(wait_job[1], wait=None)
                await self._reacquire_place(run_id, parent.key)

    async def answer(self, run_id: str, key: str, generation: int, answer: str) -> bool:
        """Complete the question at *key* with a person's *answer*; false when that generation no longer waits.

        The answer is saved under the run's lock and epoch, as every write of the run is, also while no attempt
        runs: the next attempt reads it back and does not ask again.
        """
        async with (condition := self._condition(run_id)):
            run, node = self.store.get_run(run_id), self.store.get_node(run_id, key)
            waits = node is not None and (node.state, node.waiting_on) == (WorkflowNodeState.WAITING, "user")
            # An answer is an act of the run, to a card somebody was shown: both are checked as they are now.
            context = self.authorize(run)
            asked = waits and "event_id" in node.links and context is not None and not isinstance(context, str)
            if run.state in TERMINAL_RUN_STATES or node is None or node.generation != generation or not asked:
                return False
            if not await self._settle(node, run.epoch, state=WorkflowNodeState.COMPLETED, result=answer):
                return False
            await self._restate(run_id)
            condition.notify_all()
        # Without the lock, which no send to the room holds up for long.
        await self._close_card(run, node, "answered", answer, RETRY_SECONDS)
        return True

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
        over, places = await self._check_budget(run), self._places_for(run)
        if not over and not places.waking and len(places.working) < places.limit:
            places.working.add(node.key)
            try:
                return await dispatch(node)
            except BaseException:
                # A call whose node names its work by now keeps its place, for the wait that settles it.
                saved = self.store.get_node(run.run_id, node.key)
                if saved is None or saved.generation != node.generation or saved.handle is None:
                    self._drop_slot(run.run_id, node.key)
                raise
        queued = replace(node, state=WorkflowNodeState.QUEUED)
        queued = await asyncio.to_thread(self.store.put_node, queued, run.epoch, dirty_after=dirty_after)
        if not over:
            places.queued.append((queued.key, partial(dispatch, queued)))
            self._drop_slot(run.run_id, None)
        return _receipt(queued)

    def _places_for(self, run: WorkflowRun) -> _Places:
        return self._places.setdefault(run.run_id, _Places(max(1, run.max_concurrent)))

    def _drop_slot(self, run_id: str, key: str | None) -> None:
        """Give up the place of *key*, then start queued calls while places remain and nobody is waking."""
        places = self._places.get(run_id)
        if places is None:
            return
        places.working.discard(key or "")
        while not places.waking and places.queued and len(places.working) < places.limit and not self._closed:
            queued, start = places.queued.popleft()
            places.working.add(queued)
            self._spawn(run_id, self._start_queued(run_id, queued, start))

    async def _release_slot(self, run_id: str, key: str) -> None:
        """Release *key*, enforce budget before pumping queued work, restate the run, and wake waiters."""
        async with (condition := self._condition(run_id)):
            if (places := self._places.get(run_id)) is not None:
                places.working.discard(key)
            with suppress(WorkflowRunNotFoundError):
                await self._check_budget(self.store.get_run(run_id))
            self._drop_slot(run_id, None)
            await self._restate(run_id)
            condition.notify_all()

    async def _reacquire_place(self, run_id: str, key: str) -> None:
        """Take a concurrency place back for *key* before a peer or child-question wait returns."""
        async with (condition := self._condition(run_id)):
            if self._closed or self._run_ended(run_id):
                return
            places = self._places_for(self.store.get_run(run_id))
            if key in places.working or len(places.working) < places.limit:
                places.working.add(key)
                return
            places.waking += 1
            try:
                while (
                    (cur := self._places.get(run_id)) is places
                    and len(places.working) >= places.limit
                    and not self._closed
                    and not self._run_ended(run_id)
                ):
                    await _wait_for_change(condition)
                if cur is places and not self._closed and not self._run_ended(run_id):
                    places.working.add(key)
            finally:
                places.waking -= 1
                self._drop_slot(run_id, None)

    async def _start_queued(self, run_id: str, key: str, start: Callable[[], Awaitable[object]]) -> None:
        """Start a call that waited for a place, under the run's lock, again while its agent cannot be decided yet."""
        try:
            while True:
                try:
                    async with self._condition(run_id):
                        places = self._places.get(run_id)
                        if places is None or run_id in self._stopping:
                            return
                        places.working.discard(key)
                        if await self._check_budget(self.store.get_run(run_id)):
                            return
                        places.working.add(key)
                        await start()
                    break
                except ScriptRuntimeUnavailableError:
                    # The bot of the call's agent is still starting, or its owner's access is not known yet.
                    await asyncio.sleep(RECHECK_SECONDS)
        except SupersededAttemptError:
            # The run moved on: what it left queued is the next attempt's to run.
            self._drop_slot(run_id, key)
        except BaseException:
            node = self.store.get_node(run_id, key)
            if node is None or node.handle is None:
                self._drop_slot(run_id, key)
            raise
        await self._notify(run_id)

    async def _ask_gate(
        self,
        run: WorkflowRun,
        attempt: ScriptRunRecord,
        node: WorkflowNode,
        tool_call: tuple[ScriptToolGrant, dict[str, object]],
    ) -> NodeReceipt:
        """Save the agent call a script made through `tool()`, and ask the broker whether that tool call may be made.

        The node is saved as a queued one is, with no handle: nothing is started before the gate answers, so a
        restart or a stop meanwhile finds a call that never ran. The caller holds the run's lock.
        """
        queued = replace(node, state=WorkflowNodeState.QUEUED)
        node = await asyncio.to_thread(self.store.put_node, queued, run.epoch, dirty_after=True)
        if not await self._check_budget(run):
            gate = partial(self._broker_outcome, attempt, node, run.epoch, tool_call, gate_only=True)
            self._spawn(run.run_id, self._start_gated(run, node, gate))
        return _receipt(node)

    async def _start_gated(
        self,
        run: WorkflowRun,
        node: WorkflowNode,
        gate: Callable[[], Awaitable[_Outcome]],
    ) -> None:
        """Start the agent of a call the broker let pass, in its turn among the run's calls; a refusal is its end."""
        outcome = await gate()
        async with self._condition(run.run_id):
            now = await asyncio.to_thread(self.store.get_run, run.run_id)
            if now.epoch != run.epoch or run.run_id in self._stopping:
                # The run ended while the gate was open: its end settled the call, and nothing of it starts now.
                return
            if outcome[0] is WorkflowNodeState.COMPLETED:
                if await self._check_budget(now):
                    return
                self._places_for(run).queued.append((node.key, partial(self._dispatch_agent, run, node, timeout=None)))
                self._drop_slot(run.run_id, None)
            else:
                await self._settle(node, run.epoch, state=outcome[0], error=outcome[2], ignore=True)
        await self._notify(run.run_id)

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
            text = await self.resolve_source(context, arguments)
        except ValueError:
            return None
        key = nested_key(outer, text, arguments.get("args"))
        runs = await asyncio.to_thread(self.store.bound_runs, *outer, call_id)
        return next((run.run_id for run in runs if run.origin_job_id == key), None)

    async def nested_report(self, outer: tuple[str, str, int], run_id: str) -> str | None:
        """Wait for a run inside the call *outer* as the call that started it did, and return what it delivers.

        The wait is one more call of the run's owner: it starts the run's attempts while it waits, and when it is
        cancelled and no other call is left, the run is cancelled with it. None for a run that is not inside that
        call. A run that another call resumed since is that call's to wait for and to stop: the call that started
        it gets the report its own attempt ended with, which the run kept.
        """
        try:
            run = self.store.get_run(run_id)
        except WorkflowRunNotFoundError:
            return None
        if (run.parent_run_id, run.parent_node_key) != outer[:2]:
            return None
        if run.owner != run.origin_job_id:
            kept = run.claims.get(str(run.origin_job_id))
            return _TAKEN.format(run_id) if kept is None else kept

        async def found() -> WorkflowRun:
            return run

        access = self.owner_access(run)
        attended = (
            self.attend(str(run.owner), found, access)
            if access is not None and not isinstance(access, str)
            else self.wait(run_id)
        )
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
        tool_call: tuple[ScriptToolGrant, dict[str, object]],
        same_as: str | None,
    ) -> NodeReceipt:
        """Save a tool node with the handle of its call, then run the call as the engine's own task.

        With *same_as*, the digest of its earlier result, the call's lane is dirty only if it answers something else;
        without, it ran because a rule said so, and its lane is dirty whatever it answers. The attempt's epoch is
        the run's here, and the call's task keeps it.
        """
        call_id = uuid5(NAMESPACE_OID, f"{run.run_id}:{node.key}:{node.generation}").hex
        # Saved before the dispatch, so a crash after the tool ran reads the broker's receipt for this call.
        handle = DispatchHandle("broker", f"{attempt.run_id}:{call_id}", dispatched=False, same_as=same_as)
        node = replace(node, state=WorkflowNodeState.RUNNING, handle=handle)
        saved = await asyncio.to_thread(self.store.save_dispatched, node, run.epoch, dirty_after=same_as is None)
        if saved is None:
            # The node that waited for its place has an end by now: its run ended.
            raise SupersededAttemptError(_RUN_ENDED)
        node = saved
        call = partial(self._broker_outcome, attempt, node, run.epoch, tool_call, call_id=call_id)
        # The call has its task before anything else may fail: a saved call nobody runs would never end.
        self._spawn(run.run_id, self._run_tool_node(node, run.epoch, call))
        # A run that waited for a person works again from here.
        await self._restate(run.run_id)
        return _receipt(node)

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

    async def peer_send(self, run_id: str, *, op_id: str, sender: NodeRef, to: str, text: str) -> dict[str, object]:
        """Send one peer message in a single transaction and wake any waiters of *run_id*."""
        send_call = partial(send_peer_message, self.store, run_id, op_id=op_id, sender=sender)
        result = await asyncio.to_thread(send_call, to=to, text=text)
        async with (condition := self._condition(run_id)):
            condition.notify_all()
        return result

    def _run_ended(self, run_id: str) -> bool:
        return run_id in self._stopping or self.store.get_run(run_id).state in TERMINAL_RUN_STATES

    async def wait_for_messages(
        self,
        run_id: str,
        reader: NodeRef,
        wait_seconds: float,
        *,
        op_id: str,
        names: tuple[str, ...],
    ) -> dict[str, object]:
        """Read unread messages, or wait on *run_id*'s condition without holding a concurrency slot."""
        read_fn = partial(read_call, self.store, run_id, op_id=op_id, reader=reader, names=names)
        condition, deadline, cancelled = self._condition(run_id), self._clock() + wait_seconds, False
        async with condition:
            if (found := await asyncio.to_thread(read_fn, record_empty=wait_seconds <= 0)) is not None:
                return found
            if self._run_ended(run_id):
                return {"messages": []}
            node = self.store.get_node(run_id, reader.key)
            same = node if node is not None and node.generation == reader.generation else None

            async def stamp_state(state: WorkflowNodeState, waiting_on: str | None = None) -> None:
                if same is None:
                    return
                with suppress(WorkflowRunNotFoundError):
                    if (cur := self.store.get_run(run_id)).state not in TERMINAL_RUN_STATES:
                        await self._stamp(same, cur.epoch, ignore=True, state=state, waiting_on=waiting_on)

            await stamp_state(WorkflowNodeState.WAITING, "peer")
            self._drop_slot(run_id, reader.key)
            await self._restate(run_id)
            condition.notify_all()
        try:
            async with condition:
                result = await self._await_peer_mail(condition, run_id, read_fn, wait_seconds, deadline)
                await stamp_state(WorkflowNodeState.QUEUED)
                await self._restate(run_id)
                condition.notify_all()
        except asyncio.CancelledError:
            cancelled = True
            raise
        else:
            return result
        finally:
            if not cancelled:
                await self._reacquire_place(run_id, reader.key)
                await stamp_state(WorkflowNodeState.RUNNING)
                await self._notify(run_id)

    async def _await_peer_mail(
        self,
        condition: asyncio.Condition,
        run_id: str,
        read_fn: Callable[..., dict[str, object] | None],
        wait_seconds: float,
        deadline: float,
    ) -> dict[str, object]:
        while True:
            if (found := await asyncio.to_thread(read_fn, record_empty=False)) is not None:
                return found
            if self._run_ended(run_id):
                return {"messages": []}
            if (remaining := deadline - self._clock()) <= 0:
                note = f"No message arrived within {wait_seconds:g} s."
                return cast("dict[str, object]", await asyncio.to_thread(read_fn, record_empty=True, empty_note=note))
            await _wait_for_change(condition, remaining)

    async def _wait_inbox(self, run: WorkflowRun, node: WorkflowNode, epoch: int, wait_seconds: float) -> None:
        reader = NodeRef("script", 1, "script")
        op_id = f"{node.key}:{node.generation}"
        read_fn = partial(read_call, self.store, run.run_id, op_id=op_id, reader=reader, names=("script",))
        condition, deadline = self._condition(run.run_id), self._clock() + wait_seconds
        async with condition:
            outcome = await self._await_peer_mail(condition, run.run_id, read_fn, wait_seconds, deadline)
        await self._settle(node, epoch, state=WorkflowNodeState.COMPLETED, result=outcome, ignore=True)
        await self._notify(run.run_id)

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
                raise ScriptRuntimeUnavailableError(_OWNER_NOT_READY)
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
                    and not _fits_ceiling(child.persona, SubagentPersona.from_dict(run.persona))
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

    async def _dispatch_agent(
        self,
        run: WorkflowRun,
        node: WorkflowNode,
        *,
        timeout: float | None,  # noqa: ASYNC109 - the seconds of work the turn gets, not a deadline of this call.
        follow_up: DelegationChild | None = None,
    ) -> NodeReceipt:
        """Start the job of an agent node and wait for it as the engine's own task; a refusal is the node's end.

        The turn starts in the live context of the run's owner, as an attempt does. While that cannot be answered
        the request fails as a transport error does, which the script's SDK repeats, and an already-saved queued
        node is tried again.
        """
        try:
            node = await self._launch_agent(run, node, run.epoch, timeout=timeout, follow_up=follow_up)
        except AgentNodeError as exc:
            # Nothing ran. The node is saved already when the job runtime refused the job it names.
            error: dict[str, object] = {"kind": "refused", "message": str(exc)}
            if not await self._settle(node, run.epoch, state=WorkflowNodeState.FAILED, error=error) and not node.seq:
                refused = replace(node, state=WorkflowNodeState.FAILED, error=error, ended_at=utc_timestamp())
                await asyncio.to_thread(self.store.put_node, refused, run.epoch, dirty_after=True)
            self._drop_slot(run.run_id, node.key)
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

    async def _launch_agent(
        self,
        run: WorkflowRun,
        node: WorkflowNode,
        epoch: int,
        *,
        timeout: float | None = None,  # noqa: ASYNC109 - seconds of work the turn gets, not a call deadline.
        turn: int = 1,
        correction: str | None = None,
        made: bool = False,
        follow_up: DelegationChild | None = None,
    ) -> WorkflowNode:
        """Authorize and start one turn of an agent node; turn 1 logs the label-vs-agent notice when applicable."""
        context, runtime = self.authorize(run), self.agents()
        if context is None:
            raise ScriptRuntimeUnavailableError(_OWNER_NOT_READY)
        if isinstance(context, str) or runtime is None:
            raise AgentNodeError(context if isinstance(context, str) else _NO_AGENTS)
        session = self.store.get_node(run.run_id, node.session_key) if node.session_key else None
        source = session.request if session is not None else node.request
        first_turn = node.kind != "turn" or int(node.request.get("turn", 1)) == 1
        if turn == 1 and first_turn and not source.get("agent") and node.name in context.current_config.agents:
            await asyncio.to_thread(self.store.append_log, run.run_id, epoch, _NAME_LABELS.format(name=node.name))
        return await launch_child(
            context,
            run,
            node,
            epoch,
            store=self.store,
            jobs=runtime.jobs,
            timeout=timeout,
            turn=turn,
            correction=correction,
            made=made,
            session=session,
            follow_up=follow_up,
        )

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
        # An agent that an earlier attempt started holds a place (including while waiting on approval), over the limit if it must.
        self._places_for(run).working.add(node.key)
        task = self._spawn(run.run_id, self._run_agent_node(run, node, epoch))
        if node.handle.work_until is not None or node.handle.work_left is not None:
            task.set_name(name)

    async def _run_agent_node(self, run: WorkflowRun, node: WorkflowNode, epoch: int) -> None:
        """Wait for the job of an agent node and settle the node as its turn ended, then wake the run's waiters."""
        try:
            await self._settle_agent(run, node, epoch)
            await self._release_slot(node.run_id, node.key)
        except BaseException:
            self._drop_slot(node.run_id, node.key)
            raise

    def _budget_exceeded(self, run: WorkflowRun) -> str | None:
        """Return the stop reason when *run* or any ancestor has reached its `max_cost_usd`, else None."""
        current: WorkflowRun | None = run
        while current is not None:
            if current.max_cost_usd is not None:
                spent = self.run_usage(current.run_id)["cost_usd"]
                if _limit_reached(spent, current.max_cost_usd):
                    return _OVER_BUDGET.format(spent=_usd(spent), limit=_usd(current.max_cost_usd))
            current = self.store.get_run(current.parent_run_id) if current.parent_run_id is not None else None
        return None

    async def _check_budget(self, run: WorkflowRun) -> bool:
        """Return whether *run* or an ancestor reached its cost limit; drop queued work and stop once idle."""
        if run.state not in SERVED_RUN_STATES or (run.max_cost_usd is None and run.parent_run_id is None):
            return False
        if (reason := await asyncio.to_thread(self._budget_exceeded, run)) is None:
            return False
        if (places := self._places.get(run.run_id)) is not None:
            places.queued.clear()
        if (places is None or (not places.working and not places.waking)) and run.run_id not in self._stopping:
            self.cancel_as_task(run.run_id, reason=reason, state=WorkflowRunState.INTERRUPTED, epoch=run.epoch)
        return True

    async def _settle_agent(self, run: WorkflowRun, node: WorkflowNode, epoch: int, *, block: bool = True) -> None:
        """Settle an agent node from its job, once that has ended, for the attempt that began with *epoch*.

        The turn's usage and its answer are saved before the job's outcome is acknowledged, so a primary that dies
        in between reads the same job again. A run that moved on meanwhile reads the job itself. Without *block*,
        for a caller that holds the run's lock, a job that cannot be read now raises `ScriptRuntimeUnavailableError`.

        An answer that does not match the call's schema starts a further turn of the same agent, which is waited
        for here, unless the run is ending or the turn was the last the agent gets.
        """
        while (further := await self._settle_turn(run, node, epoch, block=block)) is not None:
            node = further
            if not block:
                # The further turn runs now: its end is the next reader's.
                raise ScriptRuntimeUnavailableError(_CORRECTING)
            task, timed = asyncio.current_task(), f"{_TIMED}{cast('DispatchHandle', node.handle).ref}"
            if task is not None and task.get_name().startswith(_TIMED):
                # The wait is found by the job it waits for, by the attempt that takes it over.
                task.set_name(timed)

    async def _settle_turn(
        self,
        run: WorkflowRun,
        node: WorkflowNode,
        epoch: int,
        *,
        block: bool,
    ) -> WorkflowNode | None:
        """Read the job of one turn of an agent node and settle the node, or return it with its further turn started."""
        assert node.handle is not None
        if (runtime := self.agents()) is None:
            # Without a job runtime nobody can read the job, now.
            raise ScriptRuntimeUnavailableError(_NO_AGENTS)
        try:
            waits = partial(self._node_waits, node, epoch) if block else None
            (kind, text), usage, claim = await await_child(run, node, runtime, on_waiting=waits)
        except ScriptRuntimeUnavailableError:
            raise
        except Exception as exc:
            logger.exception("workflow_agent_outcome_unreadable", run_id=node.run_id, key=node.key)
            # What the turn did is unknown, so its call is not one to run again without being asked to.
            (kind, text), usage, claim = ("indeterminate", str(exc)), [], None
        # A further turn of the node is an entry of its own, and the node keeps whether any turn started a tool call.
        turn = int(node.links.get("turn", 1))
        made = bool(node.links.get("made")) or any(used.get("tool_calls") for _, _, used in usage)
        stopping = node.run_id in self._stopping
        outcome: _Outcome | str | WorkflowNode
        further = None
        try:
            with suppress(SupersededAttemptError):
                add = partial(self.store.add_usage, node.run_id, node.key, node.generation, epoch=epoch)
                for entry, attempt, used in usage:
                    await asyncio.to_thread(add, entry, attempt, used)
                outcome = _turn_outcome(node, kind, text, last=turn >= SCHEMA_TURNS, made=made, stopping=stopping)
                if isinstance(outcome, str):
                    correct = partial(self._correct, run, node, epoch, outcome, turn=turn + 1, made=made)
                    outcome = await correct(block=block)
                if isinstance(outcome, WorkflowNode):
                    further = outcome
                else:
                    state, result, error = outcome
                    await self._settle(node, epoch, state=state, result=result, error=error)
                if claim is not None:
                    await runtime.jobs.acknowledge_wait(node.handle.ref, claim)
        finally:
            # An outcome this wait did not save, because the run moved on or the wait was cancelled, is the next
            # reader's. After the acknowledgement there is no claim left to give back.
            await runtime.jobs.release_wait(node.handle.ref, claim)
        return further

    async def _correct(
        self,
        run: WorkflowRun,
        node: WorkflowNode,
        epoch: int,
        correction: str,
        *,
        turn: int,
        made: bool,
        block: bool,
    ) -> WorkflowNode | _Outcome:
        """Start the further *turn* of an agent node whose answer did not match its schema; return the node with it.

        The turn is authorized as every call of the run is, and gets what the call has left of its working time.
        One that may not start is the node's end, which is returned. While that cannot be decided the turn waits,
        or raises `ScriptRuntimeUnavailableError` without *block*.
        """
        saved = self.store.get_node(node.run_id, node.key)
        current = saved.handle if saved is not None and saved.generation == node.generation else None
        handle = current or cast("DispatchHandle", node.handle)
        while True:
            left = handle.work_left if handle.work_until is None else max(0.0, handle.work_until - time.time())
            try:
                if (
                    reason := await asyncio.to_thread(self._budget_exceeded, self.store.get_run(node.run_id))
                ) is not None:
                    stopped: _Outcome = WorkflowNodeState.CANCELLED, None, {"kind": "cancelled", "message": reason}
                    return _unless_proven(node, made, stopped)
                return await self._launch_agent(
                    run,
                    node,
                    epoch,
                    timeout=left,
                    turn=turn,
                    correction=correction,
                    made=made,
                )
            except ScriptRuntimeUnavailableError:
                if not block:
                    raise
                await asyncio.sleep(RECHECK_SECONDS)
            except AgentNodeError as exc:
                refused: _Outcome = WorkflowNodeState.FAILED, None, {"kind": "refused", "message": str(exc)}
                return _unless_proven(node, made, refused)

    async def _node_waits(
        self,
        node: WorkflowNode,
        epoch: int,
        on: str | None,
        left: float | None = None,
        *,
        gate_only: bool = False,
    ) -> None:
        """Show a node as waiting for a person, for what *on* says, or as running again with None; its run follows.

        A call waiting on a human approval keeps its concurrency place until it finishes; a *gate_only* call holds no
        place yet and returns to `queued` for admission. *left* is the working time an agent's turn has left, which its
        handle keeps for whoever waits for it next.
        """
        state = (
            WorkflowNodeState.WAITING if on else (WorkflowNodeState.QUEUED if gate_only else WorkflowNodeState.RUNNING)
        )
        changes: dict[str, object] = {"state": state, "waiting_on": on}
        if left is not None and node.handle is not None:
            until = None if on else time.time() + left
            changes["handle"] = replace(node.handle, work_left=left if on else None, work_until=until)
        await self._stamp(node, epoch, ignore=True, **changes)
        await self._notify(node.run_id)

    async def _reconcile(  # noqa: C901
        self,
        run: WorkflowRun,
        node: WorkflowNode,
        epoch: int,
        *,
        stop: bool = False,
    ) -> bool:
        """Settle a node that an ended attempt left live from what its handle names; return whether its job still runs.

        A job that still runs is left as it is, for the attempt that reaches its node to wait for. With *stop* the
        run has ended and `_stop_agents` waited for the job: it is read as it stands, and so is one that did not
        end in time. The caller holds the run's lock.
        """
        if node.kind == "ask":
            # A question outlives its attempt and not its run: nothing of it ran, and its card says it is closed.
            if stop:
                error = {"kind": "cancelled", "message": _NOT_ANSWERED}
                await self._settle(node, epoch, state=WorkflowNodeState.CANCELLED, error=error)
                await self._close_card(run, node, "cancelled")
            return not stop
        if node.handle is None or node.handle.kind == "run":
            # Still queued for a place when its attempt or its run ended: it started nothing. Or the group of a run
            # inside this one, which does not outlive it and is reached only as it ends: the end of that run
            # settles the group, or this does.
            end = WorkflowNodeState.CANCELLED if stop else WorkflowNodeState.FAILED
            error = {"kind": "cancelled", "message": _RUN_ENDED} if stop else _INTERRUPTED
            if node.handle is not None:
                await self.cancel(node.handle.ref, reason=_RUN_ENDED)
            await self._settle(node, epoch, state=end, error=error)
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
        if runtime is not None and _works(runtime, job_id):
            if not stop:
                return True
            await runtime.jobs.request_cancel(job_id)
        try:
            await self._settle_agent(run, node, epoch, block=False)
            if not stop:
                await self._check_budget(self.store.get_run(run.run_id))
        except ScriptRuntimeUnavailableError:
            if not stop:
                # The lock is not held while access resolves: the script's SDK asks again, and a Stop gets in.
                raise
            # A Stop waits for nobody's access and for no job runtime: the turn it may not read may have done
            # anything. A handle without the mark that its job was handed over proves nothing either, since the job
            # starts before that mark is saved.
            state, _, error = _indeterminate(node)
            await self._settle(node, epoch, state=state, error=error)
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
            if run.state in SERVED_RUN_STATES:
                states = self.store.node_states(run_id)
                places = self._places.get(run_id)
                blocked = places is not None and len(places.working) >= places.limit
                # A queued call behind full approval-held slots cannot start until a person acts.
                working = states & UNSETTLED_NODE_STATES - ({WorkflowNodeState.QUEUED} if blocked else set())
                waits = WorkflowNodeState.WAITING in states and not working
                state = WorkflowRunState.WAITING if waits else WorkflowRunState.RUNNING
                if state is not run.state:
                    changes = {"state": state, **self._time_left(run, span)}
                    self.store.update_run(run_id, run.epoch, when=SERVED_RUN_STATES, **changes)
                    return True
        return False

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
            async with (condition := self._condition(run.run_id)):
                node = self._node(run.run_id, node_id)
                remaining = deadline - loop.time()
                waiting_changed = known_state is not None and (
                    (node.state is WorkflowNodeState.WAITING) != (known_state == WorkflowNodeState.WAITING)
                )
                if node.state not in LIVE_NODE_STATES or waiting_changed or remaining <= 0:
                    return _receipt(node)
                await _wait_for_change(condition, remaining)

    def node_prompt(self, run_id: str, key: str | None, generation: int | None) -> str | None:
        """Return the prompt of the agent call that is *generation* of the node at *key*, while it waits for its agent.

        None once the call has an outcome, was replaced, or went with its run: nothing waits for its job any more.
        """
        node = self.store.get_node(run_id, key) if key else None
        live = node is not None and node.generation == generation and node.state in LIVE_NODE_STATES
        if live and (runtime := self.agents()) is not None:
            # A Stop of the run's own job ends the run at its next pass: until then nothing of the run goes on.
            owner = self.store.get_run(run_id).job_id
            job = runtime.jobs.retained_job(owner) if owner is not None else None
            live = job is None or job.status not in _STOPPED_JOB_STATUSES
        if node is None or not live or not isinstance(node.request.get("prompt"), str):
            return None
        return first_prompt(node)

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
        self._spawn(run.run_id, self._observe_exit(run.run_id, attempt.run_id))

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
                started = False
            except ScriptRunNotFoundError:
                started = None
            outcome = _unless_proven(node, started, (WorkflowNodeState.FAILED, None, _INTERRUPTED))
        return self._settle_broker(node, epoch, outcome)

    def _settle_broker(self, node: WorkflowNode, epoch: int, outcome: _Outcome) -> WorkflowNodeState:
        if outcome[0] is WorkflowNodeState.COMPLETED and (interrupted := self._nested_interrupted(node)) is not None:
            outcome = interrupted
        state, result, error = outcome
        settle = partial(self.store.settle_node, node.run_id, node.key, node.generation, epoch)
        settle(state=state, result=result, error=error, dirty_after=_changed(node, outcome))
        return state

    async def _stamp(self, node: WorkflowNode, epoch: int, *, ignore: bool = False, **changes: object) -> bool:
        return await self._fenced(self.store.update_node, node, epoch, ignore, **changes)

    async def _settle(self, node: WorkflowNode, epoch: int, *, ignore: bool = False, **changes: object) -> bool:
        return await self._fenced(self.store.settle_node, node, epoch, ignore, **changes)

    async def _fenced(self, op: Callable[..., bool], n: WorkflowNode, epoch: int, ignore: bool, **kw: object) -> bool:
        try:
            return await asyncio.to_thread(op, n.run_id, n.key, n.generation, epoch, **kw)
        except SupersededAttemptError:
            if not ignore:
                raise
            return False

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
        # In no script's tool call, whatever starts it: a task started as a tool call ends is not inside that call.
        context = copy_context()
        context.run(_CALLING_NODE.set, None)
        task = asyncio.create_task(work, name=f"workflow:{owner}", context=context)
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
        node = self.store.node_by_id(run_id, node_id)
        if node is None:
            msg = f"Run {run_id} has no call {node_id}."
            raise LookupError(msg)
        return node

    async def _broker_outcome(
        self,
        attempt: ScriptRunRecord,
        node: WorkflowNode,
        epoch: int,
        tool_call: tuple[ScriptToolGrant, dict[str, object]],
        *,
        call_id: str | None = None,
        gate_only: bool = False,
    ) -> _Outcome:
        """Run one broker call for *node* within the rate limit and return its node outcome."""
        cid = call_id or uuid5(NAMESPACE_OID, f"{node.run_id}:{node.key}:{node.generation}").hex
        waits = partial(self._node_waits, node, epoch, gate_only=gate_only)
        call = partial(
            self.broker.execute_for_workflow,
            attempt,
            call_id=cid,
            grant=tool_call[0],
            arguments=tool_call[1],
            waits=waits,
            gate_only=gate_only,
        )
        while True:
            try:
                return _call_outcome(node, await call())
            except ScriptCallRateLimitError:
                await asyncio.sleep(_RATE_WAIT_SECONDS)
            except (ScriptRunStoreError, ScriptRuntimeUnavailableError) as exc:
                return WorkflowNodeState.FAILED, None, {"kind": "failed", "message": str(exc)}

    async def _run_tool_node(
        self,
        node: WorkflowNode,
        epoch: int,
        call: Callable[[], Awaitable[_Outcome]],
    ) -> None:
        """Run one tool call and settle its node."""
        # A `start_workflow` this call makes starts a run inside this one, under this node.
        _CALLING_NODE.set((node.run_id, node.key, node.generation))
        try:
            outcome = await call()
            await hold("before_settle")
            # A run that moved on meanwhile reads the call's receipt itself.
            with suppress(SupersededAttemptError):
                await asyncio.to_thread(self._settle_broker, node, epoch, outcome)
            await self._release_slot(node.run_id, node.key)
        except BaseException:
            self._drop_slot(node.run_id, node.key)
            raise

    async def _watch_time(self, run_id: str, epoch: int, hours: float) -> None:
        """Save the working time the attempt that began with *epoch* uses, and stop it when none is left."""
        left = self.store.get_run(run_id).runtime_seconds_left
        with suppress(SupersededAttemptError):
            while left is not None and left > 0:
                await asyncio.sleep(min(_CHARGE_SECONDS, left))
                # Under the run's lock, as a change of the run's state charges it: the time left is read and written
                # by one of them at a time, so neither takes the same time off twice or writes back an older time.
                async with self._condition(run_id):
                    run = self.store.get_run(run_id)
                    if charge := self._time_left(run):
                        run = await asyncio.to_thread(self.store.update_run, run_id, epoch, **charge)
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

    async def _stop_agents(self, run: WorkflowRun) -> None:
        """Ask every agent of the ending *run*, and every run inside it, to stop, and wait for all of them together.

        The run is marked under its lock, so it takes no call from then on, and the lock is not held for the wait.
        The end of a job says what its turn did, which `_end_run` reads then; one deadline is for all the jobs.
        """
        async with self._condition(run.run_id):
            current = self.store.get_run(run.run_id)
            if current.epoch != run.epoch or current.state in TERMINAL_RUN_STATES:
                return
            self._stopping.add(run.run_id)
            # Before any call of the run is stopped: the place it gives up starts no call that waited for one.
            self._places.pop(run.run_id, None)
            self._condition(run.run_id).notify_all()
            nodes = self.store.nodes(run.run_id, include_stale=True)
        deadline = asyncio.get_running_loop().time() + _STOP_SECONDS
        handles = [node.handle for node in nodes if node.state in LIVE_NODE_STATES and node.handle is not None]
        runtime = self.agents()
        jobs = [handle.ref for handle in handles if handle.kind == "job" and runtime is not None]
        inside = [self.cancel(handle.ref, reason=_RUN_ENDED) for handle in handles if handle.kind == "run"]
        await asyncio.gather(*inside, *(cast("AgentRuntime", runtime).jobs.request_cancel(job) for job in jobs))
        with suppress(TimeoutError):
            async with asyncio.timeout_at(deadline):
                while any(_works(runtime, job) for job in jobs):  # noqa: ASYNC110 - no event for a job's end
                    await asyncio.sleep(_STOP_POLL_SECONDS)

    async def _end_run(
        self,
        run: WorkflowRun,
        state: WorkflowRunState,
        error: str | None,
        **changes: object,
    ) -> WorkflowRun:
        """End *run*: stop its node tasks, read each unsettled node from its handle, store the state, wake waiters.

        A run that ends for good first stops its agents (`_stop_agents`), so each node is read from a job that has
        ended. With the state `starting` only the run's attempt is over. A run that has ended, or that moved on
        from the epoch *run* has, is returned as it is: what was decided about *run* says nothing about it.
        """
        condition = self._condition(run.run_id)
        try:
            if state in TERMINAL_RUN_STATES:
                # Without the lock, which the wait for the run's agents must not hold.
                await self._stop_agents(run)
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
                    # An agent's job outlives an attempt, and so do a nested run and a question: none outlives its run.
                    waits_for_job = node.kind == "ask" or (
                        node.handle is not None and node.handle.kind in ("job", "run")
                    )
                    if node.state in LIVE_NODE_STATES and (ended or not waits_for_job):
                        await self._reconcile(current, node, run.epoch, stop=ended)
                if state is WorkflowRunState.COMPLETED:
                    # What the completed script no longer reaches leaves the run's current view.
                    await asyncio.to_thread(self.store.mark_stale, run.run_id, run.epoch)
                at = current.finished_at or utc_timestamp()
                changes = {"state": state, "error": error, "finished_at": at, **self._time_left(current), **changes}
                attempt = current.script_run_id or ""

                def forget(written: asyncio.Future[WorkflowRun]) -> None:
                    if not written.cancelled() and written.exception() is None:
                        self._launched.pop(attempt, None)

                end = asyncio.ensure_future(asyncio.to_thread(self.store.update_run, run.run_id, run.epoch, **changes))
                # What is kept per attempt goes with the write: a task cancelled at the commit leaves none of it behind.
                end.add_done_callback(forget)
                ended = await asyncio.shield(end)
                self._charged.pop(run.run_id, None)
                self._refused.discard(run.run_id)
                condition.notify_all()
                if ended.state in TERMINAL_RUN_STATES and ended.parent_run_id is not None:
                    await self._settle_group(ended)
        finally:
            self._stopping.discard(run.run_id)
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
            group = self.store.get_node(outer.run_id, key)
            generation = 1 if group is None else group.generation
            settle = partial(self.store.settle_node, outer.run_id, key, generation, outer.epoch, state=end, error=error)
            await asyncio.to_thread(settle)

    def _nested_interrupted(self, node: WorkflowNode) -> _Outcome | None:
        """Return a non-completed outcome when this generation's nested run ended interrupted."""
        if (node.request.get("toolkit"), node.request.get("function")) != ("workflow", "start_workflow"):
            return None
        for child in self.store.child_runs(node.run_id):
            if child.parent_node_key != node.key or child.state is not WorkflowRunState.INTERRUPTED:
                continue
            expected = nested_key((node.run_id, node.key, node.generation), child.source.encode(), child.args)
            if child.owner == expected:
                reason = child.error or f"Nested run {child.run_id} ended interrupted."
                return WorkflowNodeState.CANCELLED, None, {"kind": "interrupted", "message": reason}
        return None


def _transcript_prompt(earlier: list[tuple[str, str]], message: str) -> str:
    """Build the first prompt of a fresh session child from saved earlier exchanges and the new *message*."""
    sections = ["Earlier conversation:", *(f"User: {asked}\nAssistant: {answered}" for asked, answered in earlier)]
    return "\n\n".join(sections) + f"\n\n{message}"


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


def _works(runtime: AgentRuntime | None, job_id: str) -> bool:
    """Return whether the job still works in *runtime*."""
    job = None if runtime is None else runtime.jobs.retained_job(job_id)
    return job is not None and job.status not in TERMINAL_STATUSES


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


async def _wait_for_change(condition: asyncio.Condition, remaining: float | None = None) -> None:
    timeout = RECHECK_SECONDS if remaining is None else min(remaining, RECHECK_SECONDS)
    with suppress(TimeoutError):
        await asyncio.wait_for(condition.wait(), timeout)


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


def _limit_reached(spent: float, limit: float) -> bool:
    """Return whether *spent* has reached *limit*, tolerating binary float drift on summed turn costs."""
    return spent >= limit or math.isclose(spent, limit, rel_tol=1e-9, abs_tol=0.0)


def _usd(amount: float) -> str:
    """Return an amount of money as a person reads it: two decimal places, and more for one under a cent."""
    return f"{amount:.2f}" if amount >= 0.01 or amount == 0 else f"{amount:.6f}".rstrip("0")


def _indeterminate(node: WorkflowNode, said: object = None) -> _Outcome:
    """Return the end of a call that may have run, with what its tool *said* when it failed after it was invoked."""
    message = (
        f"This call may already have run (attempt {node.attempt}). Check its effect; "
        f'to run it again call resume_workflow(run_id, rerun=["n{node.seq}"]).'
    )
    message = f"{said}\n\n{message}" if said else message
    return WorkflowNodeState.INDETERMINATE, None, {"kind": "indeterminate", "message": message}


def _unless_proven(node: WorkflowNode, started: bool | None, safe: _Outcome, said: object = None) -> _Outcome:
    """Return how a node that ended without a result ends: as *safe* to run again only if its record proves that."""
    return _indeterminate(node, said) if may_have_written(started) else safe


def _turn_outcome(
    node: WorkflowNode,
    kind: str,
    text: str,
    *,
    last: bool,
    made: bool,
    stopping: bool,
) -> _Outcome | str:
    """Return how an agent's call ends on the end of one of its turns, or what a further turn tells the agent.

    A turn that did not complete ends the call. Whatever ended it proves nothing of a tool call that an earlier
    turn of the call made, so such a call is not run again unasked.
    """
    if kind == "completed":
        return _answer_outcome(node, text, last=last, made=made, stopping=stopping)
    if kind == "indeterminate" or node.links.get("made"):
        return _indeterminate(node, None if kind == "indeterminate" else text)
    state = WorkflowNodeState.CANCELLED if kind == "cancelled" else WorkflowNodeState.FAILED
    return state, None, {"kind": kind, "message": text}


def _answer_outcome(node: WorkflowNode, text: str, *, last: bool, made: bool, stopping: bool) -> _Outcome | str:
    """Return how an agent's call ends on its answer, or what a further turn tells an agent that missed its schema.

    The answer is the call's result, or with a schema the value in it when that matches. A run that is ending
    starts no further turn, and after the *last* turn the call fails with kind `schema`: neither is run again
    unasked when a turn *made* a tool call.
    """
    if "schema" not in node.request:
        return WorkflowNodeState.COMPLETED, text, None
    value = None
    try:
        value = extract_json(text)
    except SchemaError as exc:
        wrong: str | None = str(exc)
    else:
        try:
            wrong = validation_error(value, node.request["schema"])
        except SchemaError as exc:
            # The schema itself cannot be applied, which no further turn changes.
            broken: _Outcome = WorkflowNodeState.FAILED, None, {"kind": "schema", "message": str(exc)}
            return _unless_proven(node, made, broken, str(exc))
    if wrong is None:
        return WorkflowNodeState.COMPLETED, value, None
    if stopping:
        ended: _Outcome = WorkflowNodeState.CANCELLED, None, {"kind": "cancelled", "message": _RUN_ENDED}
        return _unless_proven(node, made, ended)
    if not last:
        return _CORRECTION.format(error=wrong)
    said = _NO_MATCH.format(name=node.name or node.label, turns=SCHEMA_TURNS, error=wrong)
    return _unless_proven(node, made, (WorkflowNodeState.FAILED, None, {"kind": "schema", "message": said}), said)


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
    refusal = "denied" if details.get("kind") == "approval_denied" else "failed"
    # A failed receipt without the stamp is a refusal. Without either, the call was claimed and still waited for
    # hooks or approval when its attempt ended.
    failed = call.state is ScriptCallState.FAILED
    safe = {"kind": refusal, "message": details.get("message")} if failed else _INTERRUPTED
    # With the stamp the tool was invoked: whatever failed then, the tool, a check of its result or the way back,
    # the call may have had its effect, unless the call's own word says it never left for its tool.
    started = call.effect_started_at is not None and not details.get("not_started")
    said = details.get("message") if failed else None
    return _unless_proven(node, started, (WorkflowNodeState.FAILED, None, safe), said)


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
