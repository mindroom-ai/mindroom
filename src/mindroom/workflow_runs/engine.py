"""The workflow engine: a run's attempts, its tool nodes and their dispatch handles, and the run's state."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, cast
from uuid import NAMESPACE_OID, uuid5
from weakref import WeakValueDictionary

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
from mindroom.script_runs.store import ScriptCallNotFoundError, ScriptRunNotFoundError, ScriptRunStoreError
from mindroom.workflow_runs.models import (
    TERMINAL_RUN_STATES,
    DispatchHandle,
    NodeReceipt,
    WorkflowNode,
    WorkflowNodeState,
    WorkflowRun,
    WorkflowRunState,
)
from mindroom.workflow_runs.replay import node_digest, split_key
from mindroom.workflow_runs.store import WorkflowRunStoreError

if TYPE_CHECKING:
    from collections.abc import Coroutine

    from mindroom.script_runs.broker import ScriptToolBroker
    from mindroom.script_runs.manager import ScriptResourceProfileName, ScriptRunManager
    from mindroom.script_runs.models import ScriptRunRecord
    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from mindroom.workflow_runs.models import NodeRequest
    from mindroom.workflow_runs.store import WorkflowRunStore

logger = get_logger(__name__)

_UNSETTLED_NODE_STATES = frozenset({WorkflowNodeState.QUEUED, WorkflowNodeState.RUNNING})
_LIVE_RUN_STATES = frozenset(WorkflowRunState) - TERMINAL_RUN_STATES
_SHELL_COMMAND = ScriptToolGrant("shell", "run_shell_command")
_MAX_LABEL_CHARS = 80
_ERROR_TAIL_BYTES = 4096
_NO_RETURN = "The script ended without returning from main(args)."
_INTERRUPTED: dict[str, object] = {"kind": "interrupted", "message": "The call was interrupted before its tool ran."}
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


class SupersededAttemptError(RuntimeError):
    """Raised for a request of an attempt its run no longer names, so a leftover process cannot act."""


class WorkflowEngine:
    """Run workflow attempts on the script runtime and their nodes through its broker."""

    def __init__(self, store: WorkflowRunStore, scripts: ScriptRunManager, broker: ScriptToolBroker) -> None:
        self.store = store
        self.scripts = scripts
        self.broker = broker
        # One per run with a waiter or a launch in progress: polls and `wait` sleep on it, a launch holds its lock.
        self._changed: WeakValueDictionary[str, asyncio.Condition] = WeakValueDictionary()
        self._tasks: dict[str, set[asyncio.Task[None]]] = {}

    async def launch(
        self,
        run_id: str,
        context: ToolRuntimeContext,
        limits: WorkflowLimits | None = None,
    ) -> WorkflowRun:
        """Start the next attempt of a reserved run."""
        limits = limits or WorkflowLimits()
        # Held until the run names the attempt, so the new process's first request waits instead of being refused.
        async with self._condition(run_id):
            run = self.store.get_run(run_id)
            if run.state is not WorkflowRunState.STARTING:
                # A reserved run gets one attempt from one launch; a run that was launched or has ended gets none.
                return run
            attempt = await self.scripts.run(
                context,
                source=run.source,
                workflow_run_id=run_id,
                resource_profile=cast("ScriptResourceProfileName | None", run.resource_profile),
                limits=ScriptRunLimits(
                    max_concurrent_runs=limits.max_concurrent_runs,
                    max_tool_calls_per_minute=limits.max_tool_calls_per_minute,
                ),
            )
            launched = self.store.update_run(
                run_id,
                when={WorkflowRunState.STARTING},
                state=WorkflowRunState.RUNNING,
                attempt=run.attempt + 1,
                script_run_id=attempt.run_id,
            )
            if launched.script_run_id != attempt.run_id:
                # The run ended while its process started, so nothing names this attempt: stop it.
                await self.scripts.revoke(attempt.run_id, reason=launched.error or launched.state.value)
                await self.scripts.reconcile_durable(run_id=attempt.run_id)
            return launched

    async def cancel(self, run_id: str, *, reason: str) -> WorkflowRun:
        """Revoke the run's attempt, end its process and end the run cancelled; completed nodes stay saved."""
        async with self._condition(run_id):
            run = self.store.get_run(run_id)
        if run.state in TERMINAL_RUN_STATES:
            return run
        outcome: tuple[WorkflowRunState, str | None] = (WorkflowRunState.CANCELLED, reason)
        if run.script_run_id is not None:
            await self.scripts.revoke(run.script_run_id, reason=reason)
            attempt = await self.scripts.reconcile_durable(run_id=run.script_run_id)
            if attempt.state in TERMINAL_SCRIPT_RUN_STATES:
                # The same rule as the maintenance pass: an attempt that ended before the cancel ends as it did.
                outcome = _run_outcome(self.store.get_run(run_id), attempt)
        return await self._end_run(run, *outcome)

    async def wait(self, run_id: str) -> WorkflowRun:
        """Return the run once it has ended."""
        condition = self._condition(run_id)
        async with condition:
            while (run := self.store.get_run(run_id)).state not in TERMINAL_RUN_STATES:
                await condition.wait()
        return run

    def status(self, run_id: str) -> tuple[WorkflowRun, list[WorkflowNode]]:
        """Return the run and its current nodes."""
        return self.store.get_run(run_id), self.store.nodes(run_id)

    async def reconcile(self) -> None:
        """End each run whose attempt has ended, with the state that attempt and the script's report give it."""
        for run in self.store.list_runs(include_finished=False):
            if run.script_run_id is None:
                continue
            attempt = await asyncio.to_thread(self.scripts.store.get_run, run.script_run_id)
            if attempt.state in TERMINAL_SCRIPT_RUN_STATES:
                await self._end_run(run, *_run_outcome(self.store.get_run(run.run_id), attempt))

    async def close(self) -> None:
        """Stop the engine's own tasks; unsettled nodes keep their handles for the next start to read."""
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
        saved = self.store.get_node(run.run_id, request.key)
        if saved is not None:
            # A request the script sent twice never repeats work, not even failed work.
            return _receipt(saved)
        if request.kind != "tool":
            msg = f"This MindRoom cannot run a '{request.kind}' call in a workflow yet."
            raise WorkflowRunStoreError(msg)
        arguments = request.args.get("arguments")
        if not isinstance(arguments, dict) or not all(
            isinstance(request.args.get(name), str) for name in ("toolkit", "function")
        ):
            msg = "A tool call needs a toolkit, a function and its arguments."
            raise WorkflowRunStoreError(msg)
        grant = ScriptToolGrant(str(request.args["toolkit"]), str(request.args["function"]))
        lane, index = split_key(request.key)
        call_id = uuid5(NAMESPACE_OID, f"{run.run_id}:{request.key}:1").hex
        meta = request.meta
        node = self.store.put_node(
            WorkflowNode(
                run_id=run.run_id,
                key=request.key,
                kind=request.kind,
                lane=lane,
                idx=index,
                digest=node_digest(request.kind, request.args),
                request=request.args,
                state=WorkflowNodeState.RUNNING,
                # Saved before the dispatch, so a crash after the tool ran reads the broker's receipt for this call.
                handle=DispatchHandle("broker", f"{attempt.run_id}:{call_id}", dispatched=False),
                attempt=run.attempt,
                label=str(meta.get("label") or f"{grant.toolkit_name}.{grant.function_name}")[:_MAX_LABEL_CHARS],
                name=cast("str | None", meta.get("name")),
                phase=cast("str | None", meta.get("phase")),
                cached=meta.get("cached") is not False,
                started_at=utc_timestamp(),
            ),
        )
        call = self._run_tool_node(attempt, node, call_id, grant, cast("dict[str, object]", arguments))
        self._spawn(run.run_id, call)
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
                    await asyncio.wait_for(condition.wait(), remaining)

    async def node(self, attempt: ScriptRunRecord, node_id: str) -> WorkflowNode:
        """Return one node of the attempt's run, for its whole result."""
        return self._node((await self._current_run(attempt)).run_id, node_id)

    async def append_log(self, attempt: ScriptRunRecord, text: str) -> None:
        """Add one progress line to the attempt's run."""
        self.store.append_log((await self._current_run(attempt)).run_id, text)

    async def finish(self, attempt: ScriptRunRecord, *, result: object, error: str | None) -> None:
        """Store what the script's `main` returned or raised; the run ends when the attempt's process has."""
        run = await self._current_run(attempt)
        self.store.update_run(
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

    def _spawn(self, run_id: str, work: Coroutine[None, None, None]) -> None:
        tasks = self._tasks.setdefault(run_id, set())
        task = asyncio.create_task(work, name=f"workflow:{run_id}")
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    async def _current_run(self, attempt: ScriptRunRecord) -> WorkflowRun:
        """Return the run *attempt* is the current attempt of."""
        if attempt.workflow_run_id is None:
            msg = f"{attempt.run_id} is not a workflow attempt."
            raise LookupError(msg)
        async with self._condition(attempt.workflow_run_id):
            run = self.store.get_run(attempt.workflow_run_id)
        if run.script_run_id != attempt.run_id:
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
        attempt: ScriptRunRecord,
        node: WorkflowNode,
        call_id: str,
        grant: ScriptToolGrant,
        arguments: dict[str, object],
    ) -> None:
        try:
            call = await self.broker.execute_for_workflow(attempt, call_id=call_id, grant=grant, arguments=arguments)
            state, result, error = _call_outcome(node, call)
        except (ScriptRunStoreError, ScriptRuntimeUnavailableError) as exc:
            # Refused before any claim: the rate limit, a revoked attempt, a broker that takes no calls now.
            state, result, error = WorkflowNodeState.FAILED, None, {"kind": "failed", "message": str(exc)}
        settle = partial(self.store.settle_node, node.run_id, node.key, node.generation)
        await asyncio.to_thread(settle, state=state, result=result, error=error)
        await self._notify(node.run_id)

    async def _observe_exit(self, script_run_id: str) -> None:
        """End the run soon after its script reported: look for the exit instead of waiting for the next pass."""
        for delay in _EXIT_POLL_SECONDS:
            await asyncio.sleep(delay)
            if (await self.scripts.reconcile_durable(run_id=script_run_id)).state in TERMINAL_SCRIPT_RUN_STATES:
                await self.reconcile()
                break

    async def _end_run(self, run: WorkflowRun, state: WorkflowRunState, error: str | None) -> WorkflowRun:
        """End *run*: stop its node tasks, read each unsettled node from its handle, store the state, wake waiters."""
        tasks = self._tasks.pop(run.run_id, set()) - {asyncio.current_task()}
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        current = self.store.get_run(run.run_id)
        if current.state in TERMINAL_RUN_STATES:
            return current
        for node in self.store.nodes(run.run_id, include_stale=True):
            if node.state in _UNSETTLED_NODE_STATES and node.handle is not None:
                await asyncio.to_thread(self.settle_from_handle, node)
        ended = self.store.update_run(
            run.run_id,
            when=_LIVE_RUN_STATES,
            state=state,
            error=error,
            finished_at=current.finished_at or utc_timestamp(),
        )
        await self._notify(run.run_id)
        return ended


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


def _call_outcome(node: WorkflowNode, call: ScriptCallRecord) -> _Outcome:
    """Map a broker receipt to how its node ends; the effect stamp says what is safe to run again."""
    if call.state is ScriptCallState.COMPLETED:
        exit_code = (call.metadata or {}).get("exit_code")
        if call.grant != _SHELL_COMMAND:
            return WorkflowNodeState.COMPLETED, call.result, None
        if exit_code is None:
            # No status says the command ended: its answer was lost on the way, so it may have run.
            return _indeterminate(node)
        return WorkflowNodeState.COMPLETED, {"output": call.result, "exit_code": exit_code}, None
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


def _run_outcome(run: WorkflowRun, attempt: ScriptRunRecord) -> tuple[WorkflowRunState, str | None]:
    """Return how a run whose attempt ended ends: an accepted cancel first, then a failed exit, then the report."""
    if attempt.state is ScriptRunState.CANCELLED:
        return WorkflowRunState.CANCELLED, attempt.cancellation_reason
    if attempt.state is ScriptRunState.FAILED:
        tail = (attempt.error or attempt.output).encode()[-_ERROR_TAIL_BYTES:]
        return WorkflowRunState.FAILED, run.error or tail.decode(errors="ignore")
    if run.finished_at is not None:
        # The script reported and its process then ended cleanly or with the primary, so the report decides.
        return (WorkflowRunState.COMPLETED if run.error is None else WorkflowRunState.FAILED), run.error
    if attempt.state is ScriptRunState.EXITED:
        return WorkflowRunState.FAILED, _NO_RETURN
    return WorkflowRunState.INTERRUPTED, attempt.cancellation_reason
