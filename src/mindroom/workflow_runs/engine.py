"""Workflow engine: run lifecycle, per-run coordination state, slot pool, and attempt endings."""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections import Counter, deque
from contextlib import asynccontextmanager, suppress
from contextvars import copy_context
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Any, Literal, cast
from weakref import WeakValueDictionary

from mindroom.background_tasks import (
    run_blocking_until_complete,
    run_coroutine_until_complete,
    wait_for_future_until_complete,
)
from mindroom.delegation.personas import persona_allows
from mindroom.logging_config import get_logger
from mindroom.script_runs.broker import ScriptRuntimeUnavailableError
from mindroom.script_runs.manager import ScriptRunLimits, ScriptRuntimeNotReadyError
from mindroom.script_runs.models import TERMINAL_SCRIPT_RUN_STATES, ScriptRunState, utc_timestamp
from mindroom.script_runs.store import ScriptRunNotFoundError
from mindroom.workflow_runs import interactions, nodes
from mindroom.workflow_runs.agent_nodes import AgentRuntime, node_prompt, works
from mindroom.workflow_runs.limits import WorkflowLimits, budget_exceeded, ceiling, limit_reached, run_usage, usd
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
from mindroom.workflow_runs.recovery import AUTO_RESUME_REASONS, NO_ACCESS
from mindroom.workflow_runs.replay import CALLING_NODE, OUT_OF_TIME, OWNER_NOT_READY, RUN_ENDED, TIMED_PREFIX, receipt
from mindroom.workflow_runs.store import SupersededAttemptError, WorkflowRunNotFoundError, WorkflowRunStoreError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Mapping, Sequence

    import nio

    from mindroom.script_runs.broker import ScriptToolBroker
    from mindroom.script_runs.manager import ScriptResourceProfileName, ScriptRunManager
    from mindroom.script_runs.models import ScriptRunRecord, ScriptToolGrant
    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from mindroom.workflow_runs.store import WorkflowRunStore

logger = get_logger(__name__)

_MAX_FAILED_NAMED = 10
_ABANDONED = "The call that waited was cancelled."
_TAKEN = "Run {} was resumed by another call."
_IDLE = "Stopped resuming after {attempts} attempts that finished nothing. Fix the script and call resume_workflow."
_MAX_IDLE_RESUMES = 5
_STOP_SECONDS, _STOP_POLL_SECONDS = 30.0, 0.05
_ERROR_TAIL_BYTES = 4096
_NO_RETURN = "The script ended without returning from main(args)."
_ATTEMPT_GONE = "The record of the run's attempt is gone."
_CHARGE_SECONDS = 60.0

type ResumeAccess = ToolRuntimeContext | Literal["pending", "revoked"]


@dataclass(slots=True)
class Places:
    """Concurrency slots for one run: at most `limit` agent and tool calls work at once."""

    limit: int
    working: set[str] = field(default_factory=set)
    queued: deque[tuple[str, Callable[[], Awaitable[object]]]] = field(default_factory=deque)
    waking: int = 0


@dataclass(slots=True)
class _RunState:
    """All live in-process coordination state for one active workflow run."""

    places: Places | None = None
    tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    stopping: bool = False
    charged_at: float | None = None
    refused: bool = False

    def is_idle(self) -> bool:
        return self.places is None and self.charged_at is None and not (self.tasks or self.stopping or self.refused)


def run_outcome(
    run: WorkflowRun,
    attempt: ScriptRunRecord | None,
    stopped: WorkflowRunState = WorkflowRunState.CANCELLED,
) -> tuple[WorkflowRunState, str | None]:
    """Map an ended script attempt and stored report to the run's terminal state and error."""
    state = ScriptRunState.INTERRUPTED if attempt is None else attempt.state
    reason = _ATTEMPT_GONE if attempt is None else attempt.cancellation_reason
    if state is ScriptRunState.CANCELLED:
        return stopped, reason
    if attempt is not None and state is ScriptRunState.FAILED:
        tail = (attempt.error or attempt.output).encode()[-_ERROR_TAIL_BYTES:]
        return WorkflowRunState.FAILED, run.error or tail.decode(errors="ignore")
    if run.finished_at is not None:
        return (WorkflowRunState.COMPLETED if run.error is None else WorkflowRunState.FAILED), run.error
    if state is ScriptRunState.EXITED:
        return WorkflowRunState.FAILED, _NO_RETURN
    return WorkflowRunState.INTERRUPTED, reason


class WorkflowEngine:
    """Own live workflow runs, attempt lifecycles, concurrency slots, and per-run coordination."""

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
        self.clock = clock
        self.agents: Callable[[], AgentRuntime | None] = lambda: None
        self.owner_access: Callable[[WorkflowRun], ResumeAccess] = lambda _run: "pending"
        self.owner_limits: Callable[[ToolRuntimeContext, str], WorkflowLimits] = lambda _ctx, _agent: WorkflowLimits()
        self.served_by: Callable[[WorkflowRun], str | None] = lambda run: run.agent_name
        self.launched: dict[str, int] = {}
        self.closed = False
        self._runs: dict[str, _RunState] = {}
        self._admissions: set[asyncio.Task[Any]] = set()
        self._conditions: WeakValueDictionary[str, asyncio.Condition] = WeakValueDictionary()
        self._attending: Counter[str | None] = Counter()
        self._inline_runs: dict[str, str] = {}

    def condition(self, run_id: str) -> asyncio.Condition:
        """Return the per-run condition variable, creating one if absent."""
        if (cond := self._conditions.get(run_id)) is None:
            cond = self._conditions[run_id] = asyncio.Condition()
        return cond

    async def wait_for_change(self, condition: asyncio.Condition, remaining: float | None = None) -> None:
        """Sleep on *condition* until notified or up to `RECHECK_SECONDS` (or *remaining* if shorter)."""
        timeout = RECHECK_SECONDS if remaining is None else min(remaining, RECHECK_SECONDS)
        with suppress(TimeoutError):
            await asyncio.wait_for(condition.wait(), timeout)

    def _state(self, run_id: str) -> _RunState:
        return self._runs.setdefault(run_id, _RunState())

    def _gc(self, run_id: str) -> None:
        if (state := self._runs.get(run_id)) is not None and state.is_idle():
            self._runs.pop(run_id, None)

    def _record_charge(self, run_id: str, at: float) -> None:
        self._state(run_id).charged_at = at

    def is_stopping(self, run_id: str) -> bool:
        """Return whether *run_id* is currently shutting down its live nodes."""
        return (state := self._runs.get(run_id)) is not None and state.stopping

    def run_ended(self, run_id: str) -> bool:
        """Return whether *run_id* is stopping or already terminal."""
        return self.is_stopping(run_id) or self.store.get_run(run_id).state in TERMINAL_RUN_STATES

    def places_for(self, run: WorkflowRun) -> Places:
        """Return the slot pool for *run*, initializing it if absent."""
        state = self._state(run.run_id)
        if state.places is None:
            state.places = Places(max(1, run.max_concurrent))
        return state.places

    def spawn[T](self, owner: str | None, work: Coroutine[None, None, T]) -> asyncio.Task[T]:
        """Spawn an engine task under *owner* (`None` for admission tasks) with cleared `CALLING_NODE`."""
        bucket = self._admissions if owner is None else self._state(owner).tasks
        context = copy_context()
        context.run(CALLING_NODE.set, None)
        task = asyncio.create_task(work, name=f"workflow:{owner or 'admissions'}", context=context)
        bucket.add(task)

        def done(finished: asyncio.Task[T]) -> None:
            bucket.discard(finished)
            if owner is not None:
                self._gc(owner)

        task.add_done_callback(done)
        return task

    async def notify(self, run_id: str) -> None:
        """Recompute run waiting state and wake waiters on *run_id*'s condition."""
        async with (cond := self.condition(run_id)):
            await self.restate(run_id)
            cond.notify_all()

    async def enter(
        self,
        run: WorkflowRun,
        node: WorkflowNode,
        dispatch: Callable[..., Awaitable[NodeReceipt]],
        *,
        dirty_after: bool = True,
    ) -> NodeReceipt:
        """Start a slot-consuming node immediately if a slot is open, otherwise persist it as queued."""
        over, places = await self.check_budget(run), self.places_for(run)
        if not over and not places.waking and len(places.working) < places.limit:
            places.working.add(node.key)
            try:
                return await dispatch(node)
            except BaseException:
                saved = self.store.get_node(run.run_id, node.key)
                if saved is None or saved.generation != node.generation or saved.handle is None:
                    self.drop_slot(run.run_id, node.key)
                raise
        put = partial(self.store.put_node, replace(node, state=WorkflowNodeState.QUEUED), run.epoch)
        queued = await asyncio.to_thread(put, dirty_after=dirty_after)
        if not over:
            self.enqueue(run, queued.key, partial(dispatch, queued))
        return receipt(queued)

    def enqueue(self, run: WorkflowRun, key: str, start: Callable[[], Awaitable[object]]) -> None:
        """Append *key* to *run*'s slot queue and pump if capacity is available."""
        self.places_for(run).queued.append((key, start))
        self.drop_slot(run.run_id, None)

    def drop_slot(self, run_id: str, key: str | None) -> None:
        """Release *key*'s slot and pump queued work while capacity remains and nobody is waking."""
        state = self._runs.get(run_id)
        if state is None or (places := state.places) is None:
            return
        places.working.discard(key or "")
        while not places.waking and places.queued and len(places.working) < places.limit and not self.closed:
            queued, start = places.queued.popleft()
            places.working.add(queued)
            self.spawn(run_id, self.start_queued(run_id, queued, start))

    async def release_slot(self, run_id: str, key: str) -> None:
        """Release *key*, check budget before pumping queued work, restate, and notify waiters."""
        async with (cond := self.condition(run_id)):
            if (state := self._runs.get(run_id)) is not None and state.places is not None:
                state.places.working.discard(key)
            with suppress(WorkflowRunNotFoundError):
                await self.check_budget(self.store.get_run(run_id))
            self.drop_slot(run_id, None)
            await self.restate(run_id)
            cond.notify_all()

    async def reacquire_place(self, run_id: str, key: str) -> None:
        """Reacquire a concurrency slot for *key* with priority over queued calls."""
        async with (cond := self.condition(run_id)):
            if self.closed or self.run_ended(run_id):
                return
            places = self.places_for(self.store.get_run(run_id))
            if key in places.working or len(places.working) < places.limit:
                places.working.add(key)
                return
            places.waking += 1
            try:

                def active() -> bool:
                    st = self._runs.get(run_id)
                    return st is not None and st.places is places and not self.closed and not self.run_ended(run_id)

                while active() and len(places.working) >= places.limit:
                    await self.wait_for_change(cond)
                if active():
                    places.working.add(key)
            finally:
                places.waking -= 1
                self.drop_slot(run_id, None)

    @asynccontextmanager
    async def yield_slot(
        self,
        run_id: str,
        key: str,
        *,
        before_reacquire: Callable[[], Awaitable[object]] | None = None,
        after_reacquire: Callable[[], Awaitable[object]] | None = None,
    ) -> AsyncIterator[None]:
        """Run a slot-yielding wait, preserving paused state on `CancelledError` and reacquiring on normal exit."""
        cancelled = False
        try:
            yield
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            if not cancelled:
                if before_reacquire is not None:
                    await before_reacquire()
                await self.reacquire_place(run_id, key)
                if after_reacquire is not None:
                    await after_reacquire()

    async def start_queued(self, run_id: str, key: str, start: Callable[[], Awaitable[object]]) -> None:
        """Start a queued call under the run's lock, retrying while authorization is temporarily pending."""
        try:
            while True:
                try:
                    async with self.condition(run_id):
                        state = self._runs.get(run_id)
                        if state is None or state.places is None or state.stopping:
                            return
                        state.places.working.discard(key)
                        if await self.check_budget(self.store.get_run(run_id)):
                            return
                        state.places.working.add(key)
                        await start()
                    break
                except ScriptRuntimeUnavailableError:
                    await asyncio.sleep(RECHECK_SECONDS)
        except SupersededAttemptError:
            self.drop_slot(run_id, key)
        except BaseException:
            node = self.store.get_node(run_id, key)
            if node is None or node.handle is None:
                self.drop_slot(run_id, key)
            raise
        await self.notify(run_id)

    async def check_budget(self, run: WorkflowRun) -> bool:
        """Return whether *run* or an ancestor reached `max_cost_usd`; clear queued work and stop once idle."""
        if run.state not in SERVED_RUN_STATES or (run.max_cost_usd is None and run.parent_run_id is None):
            return False
        if (reason := await asyncio.to_thread(budget_exceeded, self.store, run)) is None:
            return False
        state = self._runs.get(run.run_id)
        places = None if state is None else state.places
        if places is not None:
            places.queued.clear()
        if (places is None or (not places.working and not places.waking)) and not self.is_stopping(run.run_id):
            self.cancel_as_task(run.run_id, reason=reason, state=WorkflowRunState.INTERRUPTED, epoch=run.epoch)
        return True

    async def restate(self, run_id: str) -> None:
        """Update `RUNNING` vs `WAITING` and charge elapsed working time on the event loop."""
        now = self.clock()
        state = self._runs.get(run_id)
        since = now if state is None or state.charged_at is None else state.charged_at
        if await asyncio.to_thread(self._restated, run_id, (since, now)):
            self._record_charge(run_id, now)

    def _restated(self, run_id: str, span: tuple[float, float]) -> bool:
        with suppress(SupersededAttemptError, WorkflowRunStoreError):
            run = self.store.get_run(run_id)
            if run.state in SERVED_RUN_STATES:
                states = self.store.node_states(run_id)
                st = self._runs.get(run_id)
                blocked = st is not None and st.places is not None and len(st.places.working) >= st.places.limit
                working = states & UNSETTLED_NODE_STATES - ({WorkflowNodeState.QUEUED} if blocked else set())
                waits = WorkflowNodeState.WAITING in states and not working
                next_state = WorkflowRunState.WAITING if waits else WorkflowRunState.RUNNING
                if next_state is not run.state:
                    changes = {"state": next_state, **self.time_left(run, span)}
                    self.store.update_run(run_id, run.epoch, when=SERVED_RUN_STATES, **changes)
                    return True
        return False

    def time_left(self, run: WorkflowRun, span: tuple[float, float] | None = None) -> dict[str, object]:
        """Compute updated `runtime_seconds_left` after subtracting running time since the last charge."""
        if span is None:
            now = self.clock()
            state = self._state(run.run_id)
            since = now if state.charged_at is None else state.charged_at
            self._record_charge(run.run_id, now)
        else:
            since, now = span
        if run.state is not WorkflowRunState.RUNNING or run.runtime_seconds_left is None:
            return {}
        with suppress(ScriptRunNotFoundError):
            if ended := self.scripts.store.get_run(run.script_run_id or "").finished_at:
                now -= (datetime.now(UTC) - datetime.fromisoformat(ended)).total_seconds()
        return {"runtime_seconds_left": max(0.0, run.runtime_seconds_left - max(0.0, now - since))}

    async def stamp(self, node: WorkflowNode, epoch: int, *, ignore: bool = False, **changes: object) -> bool:
        """Apply a fenced `update_node` off the event loop."""
        return await self._fenced(self.store.update_node, node, epoch, ignore, **changes)

    async def settle_node(self, node: WorkflowNode, epoch: int, *, ignore: bool = False, **changes: object) -> bool:
        """Apply a fenced `settle_node` off the event loop."""
        return await self._fenced(self.store.settle_node, node, epoch, ignore, **changes)

    async def _fenced(self, op: Callable[..., bool], n: WorkflowNode, epoch: int, ignore: bool, **kw: object) -> bool:
        try:
            return await asyncio.to_thread(op, n.run_id, n.key, n.generation, epoch, **kw)
        except SupersededAttemptError:
            if not ignore:
                raise
            return False

    def authorize(self, run: WorkflowRun, grant: ScriptToolGrant | None = None) -> ToolRuntimeContext | str | None:
        """Check frozen persona ceiling and live owner access; return context, refusal text, or None if pending."""
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
        """Validate whether *run* may start its next attempt now, returning `(context, limits)`, refusal, or None."""
        replay = run.state is WorkflowRunState.STARTING and run.script_run_id is not None
        context = self.authorize(run)
        if context is None or isinstance(context, str):
            return context
        if (
            caller is not None
            and not replay
            and (run.room_id, run.thread_root_event_id)
            == (
                caller.room_id,
                caller.resolved_thread_id,
            )
        ):
            context, resolved_limits = caller, limits or self.owner_limits(caller, run.agent_name)
        else:
            resolved_limits = self.owner_limits(context, run.agent_name)
        if run.state is WorkflowRunState.STARTING and run.runtime_seconds_left == 0:
            return OUT_OF_TIME.format(hours=resolved_limits.max_runtime_hours)
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
        """Launch the next attempt for a `STARTING` run as a shielded engine admission task."""
        if self.closed:
            return self.store.get_run(run_id)
        task = self.spawn(None, self._launch(run_id, caller, limits, owner, epoch))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if not task.cancelled() or (current is not None and current.cancelling()):
                raise
            return self.store.get_run(run_id)

    async def _launch(
        self,
        run_id: str,
        caller: ToolRuntimeContext | None,
        limits: WorkflowLimits | None,
        owner: str | None,
        epoch: int | None,
    ) -> WorkflowRun:
        async with self.condition(run_id):
            run = self.store.get_run(run_id)
            moved = owner not in (None, run.owner) or epoch not in (None, run.epoch)
            waits = run.state is WorkflowRunState.STARTING and not self.closed and not moved
            admitted = self.admit(run, caller, limits) if waits else None
            if admitted is None:
                return run
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
                    run_state = self._state(run_id)
                    if not run_state.refused:
                        logger.warning("workflow_launch_refused", run_id=run_id, error=str(exc))
                    run_state.refused = True
                    if replay or (run.job_id is not None and isinstance(exc, ScriptRuntimeNotReadyError)):
                        return run
                    state, refusal = WorkflowRunState.FAILED, str(exc)
                else:
                    return await self._begin(run, attempt, limits.max_runtime_hours)
        return await self.end_run(run, state, cast("str", refusal))

    async def _begin(self, run: WorkflowRun, attempt: ScriptRunRecord, hours: float) -> WorkflowRun:
        self._record_charge(run.run_id, self.clock())
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
            launched = self.store.get_run(run.run_id)
            await self.scripts.revoke(attempt.run_id, reason=launched.error or launched.state.value)
            await self.scripts.reconcile_durable(run_id=attempt.run_id)
            if (state := self._runs.get(run.run_id)) is not None:
                state.charged_at = None
                self._gc(run.run_id)
        else:
            self.launched[attempt.run_id] = launched.epoch
            self.spawn(run.run_id, self.watch_time(run.run_id, launched.epoch, hours))
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
        """Revoke the current attempt, stop live nodes, and end *run_id* in *state*."""
        async with self.condition(run_id):
            run = self.store.get_run(run_id)
        while run.state not in TERMINAL_RUN_STATES and owner in (None, run.owner) and epoch in (None, run.epoch):
            outcome: tuple[WorkflowRunState, str | None] = (state, reason)
            if (run_state := self._runs.get(run_id)) is not None:
                run_state.places = None
            if run.script_run_id is not None and run.state is not WorkflowRunState.STARTING:
                await self.scripts.revoke(run.script_run_id, reason=reason)
                attempt = await self.scripts.reconcile_durable(run_id=run.script_run_id)
                if attempt.state in TERMINAL_SCRIPT_RUN_STATES:
                    outcome = run_outcome(self.store.get_run(run_id), attempt, stopped=state)
            run = await self.end_run(run, *outcome)
        return run

    def cancel_as_task(self, run_id: str, **how: Any) -> asyncio.Task[WorkflowRun]:  # noqa: ANN401 - forwards to cancel
        """Run `cancel` as an engine-owned background task."""

        def failed(ended: asyncio.Task[WorkflowRun]) -> None:
            error = None if ended.cancelled() else ended.exception()
            if error is not None and not isinstance(error, WorkflowRunNotFoundError):
                logger.error("workflow_run_stop_failed", run_id=run_id, exc_info=error)

        task = self.spawn(run_id, self.cancel(run_id, **how))
        task.add_done_callback(failed)
        return task

    def job_stop(self, run: WorkflowRun, reason: str) -> dict[str, Any]:
        """Return cancel arguments for a stopped job, mapping revoked access to `INTERRUPTED`."""
        if self.owner_access(run) == "revoked":
            return {"state": WorkflowRunState.INTERRUPTED, "reason": NO_ACCESS}
        return {"reason": reason}

    async def _stop_agents(self, run: WorkflowRun) -> None:
        async with (cond := self.condition(run.run_id)):
            current = self.store.get_run(run.run_id)
            if current.epoch != run.epoch or current.state in TERMINAL_RUN_STATES:
                return
            state = self._state(run.run_id)
            state.stopping, state.places = True, None
            cond.notify_all()
            live_nodes = await asyncio.to_thread(self.store.nodes, run.run_id, include_stale=True, light=True)
        deadline = asyncio.get_running_loop().time() + _STOP_SECONDS
        handles = [node.handle for node in live_nodes if node.state in LIVE_NODE_STATES and node.handle is not None]
        runtime = self.agents()
        jobs = [handle.ref for handle in handles if handle.kind == "job" and runtime is not None]
        inside = [self.cancel(handle.ref, reason=RUN_ENDED) for handle in handles if handle.kind == "run"]
        await asyncio.gather(*inside, *(cast("AgentRuntime", runtime).jobs.request_cancel(job) for job in jobs))
        with suppress(TimeoutError):
            async with asyncio.timeout_at(deadline):
                while any(works(runtime, job) for job in jobs):  # noqa: ASYNC110 - no event for job end
                    await asyncio.sleep(_STOP_POLL_SECONDS)

    async def _stop_and_reconcile(self, run: WorkflowRun, *, ended: bool) -> WorkflowRun:
        if (run_state := self._runs.get(run.run_id)) is not None:
            run_state.places = None
            mine = set(run_state.tasks)
            if ended:
                run_state.tasks.clear()
            kept = () if ended else [task for task in mine if task.get_name().startswith(TIMED_PREFIX)]
            tasks = mine - {asyncio.current_task(), *kept}
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        current = self.store.get_run(run.run_id)
        for node in await asyncio.to_thread(self.store.nodes, run.run_id, include_stale=True, light=True):
            if node.state not in LIVE_NODE_STATES:
                continue
            outlives = node.kind == "ask" or (node.handle is not None and node.handle.kind in ("job", "run"))
            if not ended and outlives:
                continue
            if (full := await asyncio.to_thread(self.store.get_node, run.run_id, node.key)) is not None:
                await nodes.reconcile(self, current, full, run.epoch, stop=ended)
        return current

    async def end_run(
        self,
        run: WorkflowRun,
        state: WorkflowRunState,
        error: str | None,
        **changes: object,
    ) -> WorkflowRun:
        """Stop live tasks, reconcile unsettled nodes, commit *state*, and wake waiters."""
        cond = self.condition(run.run_id)
        try:
            if state in TERMINAL_RUN_STATES:
                await self._stop_agents(run)
            async with cond:
                current = self.store.get_run(run.run_id)
                if current.epoch != run.epoch or current.state in TERMINAL_RUN_STATES:
                    return current
                ended = state in TERMINAL_RUN_STATES
                current = await self._stop_and_reconcile(run, ended=ended)
                if state is WorkflowRunState.COMPLETED:
                    await asyncio.to_thread(self.store.mark_stale, run.run_id, run.epoch)
                at = current.finished_at or utc_timestamp()
                changes = {"state": state, "error": error, "finished_at": at, **self.time_left(current), **changes}
                attempt = current.script_run_id or ""

                def forget(written: asyncio.Future[WorkflowRun]) -> None:
                    if not written.cancelled() and written.exception() is None:
                        self.launched.pop(attempt, None)

                end = asyncio.ensure_future(asyncio.to_thread(self.store.update_run, run.run_id, run.epoch, **changes))
                end.add_done_callback(forget)
                ended_run = await asyncio.shield(end)
                if (run_state := self._runs.get(run.run_id)) is not None:
                    run_state.charged_at, run_state.refused = None, False
                cond.notify_all()
                if ended_run.state in TERMINAL_RUN_STATES and ended_run.parent_run_id is not None:
                    await self._settle_group(ended_run)
        finally:
            if (run_state := self._runs.get(run.run_id)) is not None:
                run_state.stopping = False
                self._gc(run.run_id)
        return ended_run

    async def _settle_group(self, child: WorkflowRun) -> None:
        ends = {
            WorkflowRunState.COMPLETED: WorkflowNodeState.COMPLETED,
            WorkflowRunState.CANCELLED: WorkflowNodeState.CANCELLED,
        }
        end = ends.get(child.state, WorkflowNodeState.FAILED)
        error = None if end is WorkflowNodeState.COMPLETED else {"kind": child.state.value, "message": child.error}
        with suppress(SupersededAttemptError, WorkflowRunStoreError):
            outer = self.store.get_run(str(child.parent_run_id))
            key = f"{child.parent_node_key}/{child.run_id}"
            gen = 1 if (group := self.store.get_node(outer.run_id, key)) is None else group.generation
            settle = partial(self.store.settle_node, outer.run_id, key, gen, outer.epoch, state=end, error=error)
            await asyncio.to_thread(settle)

    async def watch_time(self, run_id: str, epoch: int, hours: float) -> None:
        """Persist running time periodically and interrupt *run_id* when its working allowance reaches zero."""
        left = self.store.get_run(run_id).runtime_seconds_left
        with suppress(SupersededAttemptError):
            while left is not None and left > 0:
                await asyncio.sleep(min(_CHARGE_SECONDS, left))
                async with self.condition(run_id):
                    run = self.store.get_run(run_id)
                    if charge := self.time_left(run):
                        run = await asyncio.to_thread(self.store.update_run, run_id, epoch, **charge)
                left = run.runtime_seconds_left
            if left is not None:
                await self.settle(self.store.get_run(run_id), replay=True)
                msg = OUT_OF_TIME.format(hours=hours)
                await self.cancel(run_id, reason=msg, state=WorkflowRunState.INTERRUPTED, epoch=epoch)

    def resumable(
        self,
        run_id: str,
        caller: ToolRuntimeContext,
        rerun: Sequence[str] = (),
        limits: WorkflowLimits | None = None,
        *,
        max_cost_usd: float | None = None,
    ) -> frozenset[str]:
        """Validate a resume request before mutating anything and return the node keys named in *rerun*."""
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
            keys = frozenset(nodes.node_by_id(self, run_id, str(node_id)).key for node_id in rerun)
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
        """Transition a terminal run back to `STARTING` with a fresh allowance, owner, and rerun markers."""
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
            async with self.condition(run_id):
                return await run_blocking_until_complete(take)
        except SupersededAttemptError:
            raise WorkflowRunStoreError(_TAKEN.format(run_id)) from None

    async def wait(
        self,
        run_id: str,
        launch: Callable[[], Awaitable[WorkflowRun]] | None = None,
        owner: str | None = None,
    ) -> WorkflowRun:
        """Wait until *run_id* reaches a terminal state, driving *launch* whenever it is `STARTING`."""
        cond = self.condition(run_id)
        while True:
            async with cond:
                while True:
                    run = self.store.get_run(run_id)
                    if owner not in (None, run.owner):
                        raise WorkflowRunStoreError(_TAKEN.format(run_id))
                    if run.state in TERMINAL_RUN_STATES or (launch and run.state is WorkflowRunState.STARTING):
                        break
                    await self.wait_for_change(cond)
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
        """Reserve, launch, and wait for an inline run, cancelling it if all callers abandon it."""
        self._attending[origin] += 1
        reserving = asyncio.ensure_future(reserve())
        try:
            run = await wait_for_future_until_complete(reserving)
            return await self.wait(run.run_id, partial(self.launch, run.run_id, context, limits, owner=origin), origin)
        finally:
            if not reserving.cancelled() and not reserving.exception():
                self._inline_runs[origin] = reserving.result().run_id
            self._attending -= Counter({origin: 1})
            stopping = self.closed or context.orchestrator is None or not context.orchestrator.running
            if not self._attending[origin] and (run_id := self._inline_runs.pop(origin, None)) and not stopping:
                await run_coroutine_until_complete(self.cancel(run_id, reason=_ABANDONED, owner=origin))

    def attended(self, owner: str | None) -> bool:
        """Return whether an inline caller on this primary is currently attending *owner*."""
        return self._attending[owner] > 0

    async def settle(self, run: WorkflowRun, *, replay: bool = False) -> WorkflowRun:
        """Settle *run* if its script process has ended, returning `STARTING` on auto-resume when *replay*."""
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
            same = {"finished_at": None, "previous_source_digest": run.source_digest}
            return await self.end_run(run, WorkflowRunState.STARTING, None, **same)
        return await self.end_run(run, state, error)

    async def close(self) -> None:
        """Stop all engine-owned tasks without mutating durable state."""
        self.closed = True
        tasks = [*self._admissions, *(task for st in self._runs.values() for task in st.tasks)]
        self._admissions.clear()
        self._runs.clear()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def nest(self, child: WorkflowRun) -> None:
        """Record nested run *child* as a group node under its parent call."""
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
        """Format the final report or error delivered when *run* ends, prefixing any caught failed nodes."""
        text = run.outcome_text()
        if run.state is not WorkflowRunState.COMPLETED:
            return text
        all_nodes = await asyncio.to_thread(self.store.nodes, run.run_id, light=True)
        bad = (WorkflowNodeState.FAILED, WorkflowNodeState.INDETERMINATE)
        failed = [f"n{node.seq}" for node in all_nodes if node.state in bad]
        if not failed:
            return text
        named = ", ".join(failed[:_MAX_FAILED_NAMED]) + (", ..." if len(failed) > _MAX_FAILED_NAMED else "")
        calls = "call" if len(failed) == 1 else "calls"
        return f'{len(failed)} {calls} failed ({named}); get_workflow(run_id, nodes="failed") shows them.\n\n{text}'

    def node_prompt(self, run_id: str, key: str | None, generation: int | None) -> str | None:
        """Return the initial prompt for a live agent node generation, or None once ended/stopped."""
        return node_prompt(self, run_id, key, generation)

    def poster(self, run: WorkflowRun) -> nio.AsyncClient | None:
        """Return the Matrix client that posts for *run*, or None while the run may not act."""
        return interactions.poster(self, run)

    async def answer(self, run_id: str, key: str, generation: int, answer_text: str) -> bool:
        """Complete an open question with *answer_text*."""
        return await interactions.answer(self, run_id, key, generation, answer_text)
