"""Agent/turn node dispatch: launch, wait, approval/user waits, schema correction, settlement, and node_prompt."""

from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING, cast

from mindroom.logging_config import get_logger
from mindroom.script_runs.broker import ScriptRuntimeUnavailableError
from mindroom.script_runs.models import utc_timestamp
from mindroom.tool_jobs.runtime import TERMINAL_STATUSES
from mindroom.workflow_runs.agent_nodes import (
    AgentNodeError,
    await_child,
    first_prompt,
    launch_child,
)
from mindroom.workflow_runs.coordination import receipt
from mindroom.workflow_runs.limits import budget_exceeded
from mindroom.workflow_runs.models import (
    LIVE_NODE_STATES,
    RECHECK_SECONDS,
    WorkflowNode,
    WorkflowNodeState,
    WorkflowRun,
)
from mindroom.workflow_runs.replay import Outcome, indeterminate, unless_proven
from mindroom.workflow_runs.store import SupersededAttemptError
from mindroom.workflow_runs.structured import (
    SCHEMA_TURNS,
    SchemaError,
    extract_json,
    validation_error,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.delegation.state import DelegationChild
    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from mindroom.workflow_runs.agent_nodes import AgentRuntime
    from mindroom.workflow_runs.coordination import RunCoordinator
    from mindroom.workflow_runs.models import DispatchHandle, NodeReceipt
    from mindroom.workflow_runs.store import WorkflowRunStore

logger = get_logger(__name__)

# How the engine's wait for an agent with a timeout is named, before its job's ID.
TIMED_PREFIX = "timed:"
OWNER_NOT_READY = "The run's agent is not ready in the run's room yet. Try again in a moment."
NO_AGENTS = "A workflow runs agents as background tool jobs. Set background_tool_jobs.enabled: true."
RUN_ENDED = "The run ended before the agent did."
_NAME_LABELS = 'name="{name}" only labels the node. Use agent="{name}" to run that agent.'
_STOPPED_JOB_STATUSES = frozenset({"cancel_requested", "cancelled", "denied"})
_CORRECTION = "Your answer did not match the schema: {error}. Reply with only the corrected fenced json block."
_NO_MATCH = "Agent '{name}' did not return JSON matching the schema after {turns} attempts. Last error: {error}."
_CORRECTING = "The agent of this call is correcting its answer. Try again in a moment."


def works(runtime: AgentRuntime | None, job_id: str) -> bool:
    """Return whether the job still works in *runtime*."""
    job = None if runtime is None else runtime.jobs.retained_job(job_id)
    return job is not None and job.status not in TERMINAL_STATUSES


def _turn_outcome(
    node: WorkflowNode,
    kind: str,
    text: str,
    *,
    last: bool,
    made: bool,
    stopping: bool,
) -> Outcome | str:
    """Return how an agent's call ends on the end of one of its turns, or what a further turn tells the agent.

    A turn that did not complete ends the call. Whatever ended it proves nothing of a tool call that an earlier
    turn of the call made, so such a call is not run again unasked.
    """
    if kind == "completed":
        return _answer_outcome(node, text, last=last, made=made, stopping=stopping)
    if kind == "indeterminate" or node.links.get("made"):
        return indeterminate(node, None if kind == "indeterminate" else text)
    state = WorkflowNodeState.CANCELLED if kind == "cancelled" else WorkflowNodeState.FAILED
    return state, None, {"kind": kind, "message": text}


def _answer_outcome(node: WorkflowNode, text: str, *, last: bool, made: bool, stopping: bool) -> Outcome | str:
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
            broken: Outcome = WorkflowNodeState.FAILED, None, {"kind": "schema", "message": str(exc)}
            return unless_proven(node, made, broken, str(exc))
    if wrong is None:
        return WorkflowNodeState.COMPLETED, value, None
    if stopping:
        ended: Outcome = WorkflowNodeState.CANCELLED, None, {"kind": "cancelled", "message": RUN_ENDED}
        return unless_proven(node, made, ended)
    if not last:
        return _CORRECTION.format(error=wrong)
    said = _NO_MATCH.format(name=node.name or node.label, turns=SCHEMA_TURNS, error=wrong)
    return unless_proven(node, made, (WorkflowNodeState.FAILED, None, {"kind": "schema", "message": said}), said)


class AgentDispatcher:
    """Turn an agent or turn node into a background job and its job into a durable node outcome."""

    def __init__(
        self,
        store: WorkflowRunStore,
        live: RunCoordinator,
        authorize: Callable[[WorkflowRun], ToolRuntimeContext | str | None],
        agents: Callable[[], AgentRuntime | None],
    ) -> None:
        self.store = store
        self.live = live
        self._authorize = authorize
        self._agents = agents

    async def dispatch_agent(
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
            if (
                not await self.live.settle(node, run.epoch, state=WorkflowNodeState.FAILED, error=error)
                and not node.seq
            ):
                refused = replace(node, state=WorkflowNodeState.FAILED, error=error, ended_at=utc_timestamp())
                await asyncio.to_thread(self.store.put_node, refused, run.epoch, dirty_after=True)
            self.live.drop_slot(run.run_id, node.key)
            return receipt(cast("WorkflowNode", self.store.get_node(run.run_id, node.key)))
        except BaseException:
            # The start failed in some other way, or the request was cancelled. A node that names its job already
            # is settled from that job, which ran nothing if it never started; the request itself fails, and the
            # script's SDK sends it again.
            saved = self.store.get_node(run.run_id, node.key)
            if saved is not None and saved.generation == node.generation and saved.handle is not None:
                self.wait_for_agent(run, saved, run.epoch)
            raise
        self.wait_for_agent(run, node, run.epoch)
        await self.live.restate(run.run_id)
        return receipt(node)

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
        context, runtime = self._authorize(run), self._agents()
        if context is None:
            raise ScriptRuntimeUnavailableError(OWNER_NOT_READY)
        if isinstance(context, str) or runtime is None:
            raise AgentNodeError(context if isinstance(context, str) else NO_AGENTS)
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

    def wait_for_agent(self, run: WorkflowRun, node: WorkflowNode, epoch: int) -> None:
        """Start the engine's wait for the job of an agent node, in place of an earlier attempt's wait for it.

        The wait for a turn with a timeout is not stopped with its attempt (`_end_run`), so somebody stops the turn
        on time between two attempts as well. What it reads then it cannot save, and leaves to the next wait.
        """
        assert node.handle is not None
        if self.live.closed:
            # A closed engine starts nothing: the job stays as it is, for the next start to continue or to read.
            return
        name = f"{TIMED_PREFIX}{node.handle.ref}"
        for stale in [task for task in self.live.tasks.get(run.run_id, ()) if task.get_name() == name]:
            stale.cancel()
        # An agent that an earlier attempt started holds a place (including while waiting on approval), over the limit if it must.
        self.live.places_for(run).working.add(node.key)
        task = self.live.spawn(run.run_id, self._run_agent_node(run, node, epoch))
        if node.handle.work_until is not None or node.handle.work_left is not None:
            task.set_name(name)

    async def _run_agent_node(self, run: WorkflowRun, node: WorkflowNode, epoch: int) -> None:
        """Wait for the job of an agent node and settle the node as its turn ended, then wake the run's waiters."""
        try:
            await self.settle_agent(run, node, epoch)
            await self.live.release_slot(node.run_id, node.key)
        except BaseException:
            self.live.drop_slot(node.run_id, node.key)
            raise

    async def settle_agent(self, run: WorkflowRun, node: WorkflowNode, epoch: int, *, block: bool = True) -> None:
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
            task, timed = asyncio.current_task(), f"{TIMED_PREFIX}{cast('DispatchHandle', node.handle).ref}"
            if task is not None and task.get_name().startswith(TIMED_PREFIX):
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
        if (runtime := self._agents()) is None:
            # Without a job runtime nobody can read the job, now.
            raise ScriptRuntimeUnavailableError(NO_AGENTS)
        try:
            waits = partial(self.node_waits, node, epoch) if block else None
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
        stopping = node.run_id in self.live.stopping
        outcome: Outcome | str | WorkflowNode
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
                    await self.live.settle(node, epoch, state=state, result=result, error=error)
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
    ) -> WorkflowNode | Outcome:
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
                    reason := await asyncio.to_thread(budget_exceeded, self.store, self.store.get_run(node.run_id))
                ) is not None:
                    stopped: Outcome = WorkflowNodeState.CANCELLED, None, {"kind": "cancelled", "message": reason}
                    return unless_proven(node, made, stopped)
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
                refused: Outcome = WorkflowNodeState.FAILED, None, {"kind": "refused", "message": str(exc)}
                return unless_proven(node, made, refused)

    async def node_waits(
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
        await self.live.stamp(node, epoch, ignore=True, **changes)
        await self.live.notify(node.run_id)

    def node_prompt(self, run_id: str, key: str | None, generation: int | None) -> str | None:
        """Return the prompt of the agent call that is *generation* of the node at *key*, while it waits for its agent.

        None once the call has an outcome, was replaced, or went with its run: nothing waits for its job any more.
        """
        node = self.store.get_node(run_id, key) if key else None
        live = node is not None and node.generation == generation and node.state in LIVE_NODE_STATES
        if live and (runtime := self._agents()) is not None:
            # A Stop of the run's own job ends the run at its next pass: until then nothing of the run goes on.
            owner = self.store.get_run(run_id).job_id
            job = runtime.jobs.retained_job(owner) if owner is not None else None
            live = job is None or job.status not in _STOPPED_JOB_STATUSES
        if node is None or not live or not isinstance(node.request.get("prompt"), str):
            return None
        return first_prompt(node)
