"""Live per-run coordination: conditions, tasks, concurrency places, restate, and working-time charge."""

from __future__ import annotations

import asyncio
from collections import deque
from contextlib import suppress
from contextvars import copy_context
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Any
from weakref import WeakValueDictionary

from mindroom.script_runs.broker import ScriptRuntimeUnavailableError
from mindroom.script_runs.store import ScriptRunNotFoundError
from mindroom.workflow_runs.limits import budget_exceeded
from mindroom.workflow_runs.models import (
    RECHECK_SECONDS,
    SERVED_RUN_STATES,
    TERMINAL_RUN_STATES,
    UNSETTLED_NODE_STATES,
    WorkflowNode,
    WorkflowNodeState,
    WorkflowRun,
    WorkflowRunState,
)
from mindroom.workflow_runs.replay import CALLING_NODE, receipt
from mindroom.workflow_runs.store import SupersededAttemptError, WorkflowRunNotFoundError, WorkflowRunStoreError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine

    from mindroom.script_runs.manager import ScriptRunManager
    from mindroom.workflow_runs.agent_nodes import AgentRuntime
    from mindroom.workflow_runs.models import NodeReceipt
    from mindroom.workflow_runs.store import WorkflowRunStore


# The owner of the tasks that admit calls: they outlive their requests, and a run's end waits for them, not stops them.
ADMISSIONS = "admissions"


@dataclass(slots=True)
class Places:
    """The places among one run's working calls: at most `limit` agent and tool calls of the run work at one time.

    A call holds its place from its start to its end (except while waiting on peer messages or a child ask_user).
    Waking waiters take freed places before `queued` calls.
    """

    limit: int
    working: set[str] = field(default_factory=set)
    queued: deque[tuple[str, Callable[[], Awaitable[object]]]] = field(default_factory=deque)
    waking: int = 0


async def wait_for_change(condition: asyncio.Condition, remaining: float | None = None) -> None:
    """Sleep on *condition* until notified or up to `RECHECK_SECONDS` (or *remaining* if shorter)."""
    timeout = RECHECK_SECONDS if remaining is None else min(remaining, RECHECK_SECONDS)
    with suppress(TimeoutError):
        await asyncio.wait_for(condition.wait(), timeout)


class RunCoordinator:
    """Single owner of live per-run state: store, scripts, agents, launched/refused attempts, conditions, tasks, places, and clock."""

    def __init__(
        self,
        store: WorkflowRunStore,
        scripts: ScriptRunManager,
        clock: Callable[[], float],
        cancel_run: Callable[..., asyncio.Task[None]],
    ) -> None:
        self.store = store
        self.scripts = scripts
        self.clock = clock
        self._cancel_run = cancel_run
        # What agent turns run on, None while there is no job runtime; set by the runtime that knows it.
        self.agents: Callable[[], AgentRuntime | None] = lambda: None
        # The attempts this engine started, each with the epoch its run got when it began: all it may ever write
        # with. No other attempt is served: one an earlier primary left is a leftover process.
        self.launched: dict[str, int] = {}
        # The runs whose wait for an attempt was logged as refused, so the tries that follow say nothing.
        self.refused: set[str] = set()
        # The runs that are ending and wait, without their lock, for their agents to stop: they take no call.
        self.stopping: set[str] = set()
        # When each running run's working time was last charged, by the engine's clock.
        self.charged: dict[str, float] = {}
        # One per run with a waiter or a launch in progress: polls and `wait` sleep on it, a launch holds its lock.
        self._changed: WeakValueDictionary[str, asyncio.Condition] = WeakValueDictionary()
        self.tasks: dict[str, set[asyncio.Task[Any]]] = {}
        # Which calls of each run work now and which wait for a place, counted here and not from the saved nodes: a
        # node that an ended attempt left running holds no place until the attempt that reaches it waits for it.
        self.places: dict[str, Places] = {}
        self.closed = False

    def condition(self, run_id: str) -> asyncio.Condition:
        """Return the per-run condition variable, creating one if absent."""
        condition = self._changed.get(run_id)
        if condition is None:
            condition = self._changed[run_id] = asyncio.Condition()
        return condition

    async def notify(self, run_id: str) -> None:
        """Wake the run's waiters after a call of it changed, and show whether the run waits for a person now."""
        async with (condition := self.condition(run_id)):
            await self.restate(run_id)
            condition.notify_all()

    def spawn[T](self, owner: str, work: Coroutine[None, None, T]) -> asyncio.Task[T]:
        """Start *work* as the engine's own task, stopped when the run *owner* ends or the engine closes."""
        tasks = self.tasks.setdefault(owner, set())
        # In no script's tool call, whatever starts it: a task started as a tool call ends is not inside that call.
        context = copy_context()
        context.run(CALLING_NODE.set, None)
        task = asyncio.create_task(work, name=f"workflow:{owner}", context=context)
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return task

    def run_ended(self, run_id: str) -> bool:
        """Return whether *run_id* is stopping or already terminal."""
        return run_id in self.stopping or self.store.get_run(run_id).state in TERMINAL_RUN_STATES

    def places_for(self, run: WorkflowRun) -> Places:
        """Return the concurrency-places state for *run*, creating it if absent."""
        return self.places.setdefault(run.run_id, Places(max(1, run.max_concurrent)))

    async def enter(
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
        over, places = await self.check_budget(run), self.places_for(run)
        if not over and not places.waking and len(places.working) < places.limit:
            places.working.add(node.key)
            try:
                return await dispatch(node)
            except BaseException:
                # A call whose node names its work by now keeps its place, for the wait that settles it.
                saved = self.store.get_node(run.run_id, node.key)
                if saved is None or saved.generation != node.generation or saved.handle is None:
                    self.drop_slot(run.run_id, node.key)
                raise
        queued = replace(node, state=WorkflowNodeState.QUEUED)
        queued = await asyncio.to_thread(self.store.put_node, queued, run.epoch, dirty_after=dirty_after)
        if not over:
            places.queued.append((queued.key, partial(dispatch, queued)))
            self.drop_slot(run.run_id, None)
        return receipt(queued)

    def drop_slot(self, run_id: str, key: str | None) -> None:
        """Give up the place of *key*, then start queued calls while places remain and nobody is waking."""
        places = self.places.get(run_id)
        if places is None:
            return
        places.working.discard(key or "")
        while not places.waking and places.queued and len(places.working) < places.limit and not self.closed:
            queued, start = places.queued.popleft()
            places.working.add(queued)
            self.spawn(run_id, self.start_queued(run_id, queued, start))

    async def release_slot(self, run_id: str, key: str) -> None:
        """Release *key*, enforce budget before pumping queued work, restate the run, and wake waiters."""
        async with (condition := self.condition(run_id)):
            if (places := self.places.get(run_id)) is not None:
                places.working.discard(key)
            with suppress(WorkflowRunNotFoundError):
                await self.check_budget(self.store.get_run(run_id))
            self.drop_slot(run_id, None)
            await self.restate(run_id)
            condition.notify_all()

    async def reacquire_place(self, run_id: str, key: str) -> None:
        """Take a concurrency place back for *key* before a peer or child-question wait returns."""
        async with (condition := self.condition(run_id)):
            if self.closed or self.run_ended(run_id):
                return
            places = self.places_for(self.store.get_run(run_id))
            if key in places.working or len(places.working) < places.limit:
                places.working.add(key)
                return
            places.waking += 1
            try:
                while (
                    (cur := self.places.get(run_id)) is places
                    and len(places.working) >= places.limit
                    and not self.closed
                    and not self.run_ended(run_id)
                ):
                    await wait_for_change(condition)
                if cur is places and not self.closed and not self.run_ended(run_id):
                    places.working.add(key)
            finally:
                places.waking -= 1
                self.drop_slot(run_id, None)

    async def start_queued(self, run_id: str, key: str, start: Callable[[], Awaitable[object]]) -> None:
        """Start a call that waited for a place, under the run's lock, again while its agent cannot be decided yet."""
        try:
            while True:
                try:
                    async with self.condition(run_id):
                        places = self.places.get(run_id)
                        if places is None or run_id in self.stopping:
                            return
                        places.working.discard(key)
                        if await self.check_budget(self.store.get_run(run_id)):
                            return
                        places.working.add(key)
                        await start()
                    break
                except ScriptRuntimeUnavailableError:
                    # The bot of the call's agent is still starting, or its owner's access is not known yet.
                    await asyncio.sleep(RECHECK_SECONDS)
        except SupersededAttemptError:
            # The run moved on: what it left queued is the next attempt's to run.
            self.drop_slot(run_id, key)
        except BaseException:
            node = self.store.get_node(run_id, key)
            if node is None or node.handle is None:
                self.drop_slot(run_id, key)
            raise
        await self.notify(run_id)

    async def check_budget(self, run: WorkflowRun) -> bool:
        """Return whether *run* or an ancestor reached its cost limit; drop queued work and stop once idle."""
        if run.state not in SERVED_RUN_STATES or (run.max_cost_usd is None and run.parent_run_id is None):
            return False
        if (reason := await asyncio.to_thread(budget_exceeded, self.store, run)) is None:
            return False
        if (places := self.places.get(run.run_id)) is not None:
            places.queued.clear()
        if (places is None or (not places.working and not places.waking)) and run.run_id not in self.stopping:
            self._cancel_run(run.run_id, reason=reason, state=WorkflowRunState.INTERRUPTED, epoch=run.epoch)
        return True

    async def restate(self, run_id: str) -> None:
        """Show the run as waiting while a call of it waits for a person and none runs, and as running otherwise.

        Time spent waiting is no working time, so the change charges what the run used up to it. The caller holds
        the run's lock. The clock and the time of the last charge are read and written here, on the loop: the
        thread that looks at the run is handed both.
        """
        now = self.clock()
        if await asyncio.to_thread(self._restated, run_id, (self.charged.get(run_id, now), now)):
            self.charged[run_id] = now

    def _restated(self, run_id: str, span: tuple[float, float]) -> bool:
        """Save the state the run has now and what it used in *span*; return whether its state changed."""
        with suppress(SupersededAttemptError, WorkflowRunStoreError):
            run = self.store.get_run(run_id)
            if run.state in SERVED_RUN_STATES:
                states = self.store.node_states(run_id)
                places = self.places.get(run_id)
                blocked = places is not None and len(places.working) >= places.limit
                # A queued call behind full approval-held slots cannot start until a person acts.
                working = states & UNSETTLED_NODE_STATES - ({WorkflowNodeState.QUEUED} if blocked else set())
                waits = WorkflowNodeState.WAITING in states and not working
                state = WorkflowRunState.WAITING if waits else WorkflowRunState.RUNNING
                if state is not run.state:
                    changes = {"state": state, **self.time_left(run, span)}
                    self.store.update_run(run_id, run.epoch, when=SERVED_RUN_STATES, **changes)
                    return True
        return False

    def time_left(self, run: WorkflowRun, span: tuple[float, float] | None = None) -> dict[str, object]:
        """Return, as a change to save, the working time *run* has left after what it used since the last charge.

        Only a running run uses any, up to where its script ended: time spent waiting for a person, starting or
        ended is not charged, and neither is the time until somebody looked at a script that had ended. Off the
        loop, *span* is the time of the last charge and now, which the caller read there and records itself.
        """
        since, now = span or (None, self.clock())
        if since is None:
            since, self.charged[run.run_id] = self.charged.get(run.run_id, now), now
        if run.state is not WorkflowRunState.RUNNING or run.runtime_seconds_left is None:
            return {}
        with suppress(ScriptRunNotFoundError):
            if ended := self.scripts.store.get_run(run.script_run_id or "").finished_at:
                now -= (datetime.now(UTC) - datetime.fromisoformat(ended)).total_seconds()
        return {"runtime_seconds_left": max(0.0, run.runtime_seconds_left - max(0.0, now - since))}

    async def stamp(self, node: WorkflowNode, epoch: int, *, ignore: bool = False, **changes: object) -> bool:
        """Apply a fenced `update_node` write off the loop."""
        return await self._fenced(self.store.update_node, node, epoch, ignore, **changes)

    async def settle(self, node: WorkflowNode, epoch: int, *, ignore: bool = False, **changes: object) -> bool:
        """Apply a fenced `settle_node` write off the loop."""
        return await self._fenced(self.store.settle_node, node, epoch, ignore, **changes)

    async def _fenced(self, op: Callable[..., bool], n: WorkflowNode, epoch: int, ignore: bool, **kw: object) -> bool:
        try:
            return await asyncio.to_thread(op, n.run_id, n.key, n.generation, epoch, **kw)
        except SupersededAttemptError:
            if not ignore:
                raise
            return False
