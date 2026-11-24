"""Broker tool calls, delegated gates, and reconciliation of unsettled nodes from their handles."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING
from uuid import NAMESPACE_OID, uuid5

from mindroom.script_runs.broker import ScriptRuntimeUnavailableError
from mindroom.script_runs.store import (
    ScriptCallNotFoundError,
    ScriptCallRateLimitError,
    ScriptRunNotFoundError,
    ScriptRunStoreError,
)
from mindroom.workflow_runs.agent_nodes import works
from mindroom.workflow_runs.faults import hold
from mindroom.workflow_runs.models import (
    DispatchHandle,
    WorkflowNode,
    WorkflowNodeState,
    WorkflowRun,
    WorkflowRunState,
)
from mindroom.workflow_runs.replay import (
    CALLING_NODE,
    INTERRUPTED,
    NO_AGENTS,
    RUN_ENDED,
    Outcome,
    call_outcome,
    changed,
    indeterminate,
    nested_key,
    receipt,
    unless_proven,
)
from mindroom.workflow_runs.store import SupersededAttemptError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mindroom.script_runs.broker import ScriptToolBroker
    from mindroom.script_runs.models import ScriptRunRecord, ScriptToolGrant
    from mindroom.workflow_runs.agent_dispatch import AgentDispatcher
    from mindroom.workflow_runs.coordination import RunCoordinator
    from mindroom.workflow_runs.models import NodeReceipt

_NOT_CONTINUED_YET = "The agent of this call has not been continued yet after a restart. Try again in a moment."
_NOT_ANSWERED = "The run ended before the question was answered."
# How long a tool call above its attempt's calls per minute waits before it asks for room again.
_RATE_WAIT_SECONDS = 1.0


def _broker_call_id(node: WorkflowNode) -> str:
    return uuid5(NAMESPACE_OID, f"{node.run_id}:{node.key}:{node.generation}").hex


class NodeDispatcher:
    """Turn broker tool calls and delegated gates into durable outcomes, and reconcile unsettled nodes."""

    def __init__(
        self,
        live: RunCoordinator,
        broker: ScriptToolBroker,
        agent_dispatch: AgentDispatcher,
        *,
        close_card: Callable[..., Awaitable[None]],
        cancel: Callable[..., Awaitable[WorkflowRun]],
    ) -> None:
        self.live = live
        self.broker = broker
        self.agent_dispatch = agent_dispatch
        self._close_card = close_card
        self._cancel = cancel

    async def ask_gate(
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
        node = await asyncio.to_thread(self.live.store.put_node, queued, run.epoch, dirty_after=True)
        if not await self.live.check_budget(run):
            gate = partial(self._broker_outcome, attempt, node, run.epoch, tool_call, gate_only=True)
            self.live.spawn(run.run_id, self.start_gated(run, node, gate))
        return receipt(node)

    async def start_gated(
        self,
        run: WorkflowRun,
        node: WorkflowNode,
        gate: Callable[[], Awaitable[Outcome]],
    ) -> None:
        """Start the agent of a call the broker let pass, in its turn among the run's calls; a refusal is its end."""
        outcome = await gate()
        async with self.live.condition(run.run_id):
            now = await asyncio.to_thread(self.live.store.get_run, run.run_id)
            if now.epoch != run.epoch or run.run_id in self.live.stopping:
                # The run ended while the gate was open: its end settled the call, and nothing of it starts now.
                return
            if outcome[0] is WorkflowNodeState.COMPLETED:
                if await self.live.check_budget(now):
                    return
                self.live.places_for(run).queued.append(
                    (node.key, partial(self.agent_dispatch.dispatch_agent, run, node, timeout=None)),
                )
                self.live.drop_slot(run.run_id, None)
            else:
                await self.live.settle(node, run.epoch, state=outcome[0], error=outcome[2], ignore=True)
        await self.live.notify(run.run_id)

    async def dispatch_tool(
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
        call_id = _broker_call_id(node)
        # Saved before the dispatch, so a crash after the tool ran reads the broker's receipt for this call.
        handle = DispatchHandle("broker", f"{attempt.run_id}:{call_id}", dispatched=False, same_as=same_as)
        node = replace(node, state=WorkflowNodeState.RUNNING, handle=handle)
        saved = await asyncio.to_thread(self.live.store.save_dispatched, node, run.epoch, dirty_after=same_as is None)
        if saved is None:
            # The node that waited for its place has an end by now: its run ended.
            raise SupersededAttemptError(RUN_ENDED)
        node = saved
        call = partial(self._broker_outcome, attempt, node, run.epoch, tool_call)
        # The call has its task before anything else may fail: a saved call nobody runs would never end.
        self.live.spawn(run.run_id, self._run_tool_node(node, run.epoch, call))
        # A run that waited for a person works again from here.
        await self.live.restate(run.run_id)
        return receipt(node)

    async def _broker_outcome(
        self,
        attempt: ScriptRunRecord,
        node: WorkflowNode,
        epoch: int,
        tool_call: tuple[ScriptToolGrant, dict[str, object]],
        *,
        gate_only: bool = False,
    ) -> Outcome:
        """Run one broker call for *node* within the rate limit and return its node outcome."""
        cid = _broker_call_id(node)
        waits = partial(self.agent_dispatch.node_waits, node, epoch, gate_only=gate_only)
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
                return call_outcome(node, await call())
            except ScriptCallRateLimitError:
                await asyncio.sleep(_RATE_WAIT_SECONDS)
            except (ScriptRunStoreError, ScriptRuntimeUnavailableError) as exc:
                return WorkflowNodeState.FAILED, None, {"kind": "failed", "message": str(exc)}

    async def _run_tool_node(
        self,
        node: WorkflowNode,
        epoch: int,
        call: Callable[[], Awaitable[Outcome]],
    ) -> None:
        """Run one tool call and settle its node."""
        # A `start_workflow` this call makes starts a run inside this one, under this node.
        CALLING_NODE.set((node.run_id, node.key, node.generation))
        try:
            outcome = await call()
            await hold("before_settle")
            # A run that moved on meanwhile reads the call's receipt itself.
            with suppress(SupersededAttemptError):
                await asyncio.to_thread(self._settle_broker, node, epoch, outcome)
            await self.live.release_slot(node.run_id, node.key)
        except BaseException:
            self.live.drop_slot(node.run_id, node.key)
            raise

    def settle_from_handle(self, node: WorkflowNode, epoch: int) -> WorkflowNodeState:
        """Settle a node an ended attempt left unsettled from the broker receipt its handle names; return its state.

        The work is never done again here: a finished receipt is copied, and a call that never reached its tool fails
        as safe to run again. *epoch* is the run's as whoever settles the node found it: the attempt that reached
        the node again, or the look that ends the attempt.
        """
        assert node.handle is not None
        script_run_id, _, call_id = node.handle.ref.partition(":")
        try:
            outcome = call_outcome(node, self.live.scripts.store.get_call(script_run_id, call_id))
        except ScriptCallNotFoundError:
            # No claim proves the call never started only while the attempt's records are still there to hold one.
            try:
                self.live.scripts.store.get_run(script_run_id)
                started = False
            except ScriptRunNotFoundError:
                started = None
            outcome = unless_proven(node, started, (WorkflowNodeState.FAILED, None, INTERRUPTED))
        return self._settle_broker(node, epoch, outcome)

    def _settle_broker(self, node: WorkflowNode, epoch: int, outcome: Outcome) -> WorkflowNodeState:
        if outcome[0] is WorkflowNodeState.COMPLETED and (interrupted := self._nested_interrupted(node)) is not None:
            outcome = interrupted
        state, result, error = outcome
        settle = partial(self.live.store.settle_node, node.run_id, node.key, node.generation, epoch)
        settle(state=state, result=result, error=error, dirty_after=changed(node, outcome))
        return state

    async def reconcile(  # noqa: C901
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
                await self.live.settle(node, epoch, state=WorkflowNodeState.CANCELLED, error=error)
                await self._close_card(run, node, "cancelled")
            return not stop
        if node.handle is None or node.handle.kind == "run":
            # Still queued for a place when its attempt or its run ended: it started nothing. Or the group of a run
            # inside this one, which does not outlive it and is reached only as it ends: the end of that run
            # settles the group, or this does.
            end = WorkflowNodeState.CANCELLED if stop else WorkflowNodeState.FAILED
            error = {"kind": "cancelled", "message": RUN_ENDED} if stop else INTERRUPTED
            if node.handle is not None:
                await self._cancel(node.handle.ref, reason=RUN_ENDED)
            await self.live.settle(node, epoch, state=end, error=error)
            return False
        if node.handle.kind != "job":
            await asyncio.to_thread(self.settle_from_handle, node, epoch)
            return False
        runtime, job_id = self.live.agents(), node.handle.ref
        if runtime is None and not stop:
            # Nobody can say how the job stands: the script's SDK asks again.
            raise ScriptRuntimeUnavailableError(NO_AGENTS)
        if (
            runtime is not None
            and not stop
            and any(job.job_id == job_id for job in runtime.jobs.awaiting_continuation())
        ):
            # A restart stopped the job, and whether it goes on is not decided: until then it is neither live nor
            # ended, and the call must not attach to a job that may yet end without having done anything.
            raise ScriptRuntimeUnavailableError(_NOT_CONTINUED_YET)
        if runtime is not None and works(runtime, job_id):
            if not stop:
                return True
            await runtime.jobs.request_cancel(job_id)
        try:
            await self.agent_dispatch.settle_agent(run, node, epoch, block=False)
            if not stop:
                await self.live.check_budget(self.live.store.get_run(run.run_id))
        except ScriptRuntimeUnavailableError:
            if not stop:
                # The lock is not held while access resolves: the script's SDK asks again, and a Stop gets in.
                raise
            # A Stop waits for nobody's access and for no job runtime: the turn it may not read may have done
            # anything. A handle without the mark that its job was handed over proves nothing either, since the job
            # starts before that mark is saved.
            state, _, error = indeterminate(node)
            await self.live.settle(node, epoch, state=state, error=error)
        return False

    def _nested_interrupted(self, node: WorkflowNode) -> Outcome | None:
        """Return a non-completed outcome when this generation's nested run ended interrupted."""
        if (node.request.get("toolkit"), node.request.get("function")) != ("workflow", "start_workflow"):
            return None
        for child in self.live.store.child_runs(node.run_id):
            if child.parent_node_key != node.key or child.state is not WorkflowRunState.INTERRUPTED:
                continue
            expected = nested_key((node.run_id, node.key, node.generation), child.source.encode(), child.args)
            if child.owner == expected:
                reason = child.error or f"Nested run {child.run_id} ended interrupted."
                return WorkflowNodeState.CANCELLED, None, {"kind": "interrupted", "message": reason}
        return None
