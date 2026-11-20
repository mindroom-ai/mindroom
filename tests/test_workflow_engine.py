"""Tests for the workflow engine: attempts, tool nodes and their dispatch handles, run states.

Level: engine harness (`WorkflowStack`): the real stores, broker and engine; the test plays the script.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio

from mindroom.approval.tool_approval import ToolApprovalDecision
from mindroom.config.agent import AgentConfig
from mindroom.orchestration.workflow_runtime import WorkflowRuntimeLifecycle
from mindroom.script_runs.broker import ScriptBrokerAuthenticationError, ScriptToolCallRequest
from mindroom.script_runs.launch_contract import digest_arguments
from mindroom.script_runs.manager import ScriptRunLimits
from mindroom.script_runs.models import ScriptCallState, ScriptRunState, ScriptToolGrant
from mindroom.script_runs.reasons import RUNTIME_RESTARTED
from mindroom.workflow_runs.engine import SupersededAttemptError
from mindroom.workflow_runs.models import (
    DispatchHandle,
    NodeRequest,
    WorkflowNode,
    WorkflowNodeState,
    WorkflowRunState,
)
from mindroom.workflow_runs.replay import node_digest
from mindroom.workflow_runs.store import WorkflowRunStoreError
from tests.conftest import SETTLE_TIMEOUT_SECONDS
from tests.workflow_helpers import NodeFailed, WorkflowStack, tool_args, workflow_stack

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mindroom.script_runs.models import ScriptCallRecord, ScriptRunRecord
    from mindroom.tool_system.runtime_context import ToolRuntimeContext

_APPEND = tool_args("counter", "append", line="a")
_CALL = "call-1"


@pytest_asyncio.fixture
async def stack(tmp_path: Path) -> AsyncIterator[WorkflowStack]:
    """The engine harness with one started run."""
    with workflow_stack(tmp_path) as stack:
        await stack.start()
        yield stack
        await stack.engine.close()


def _broker_call(stack: WorkflowStack, key: str = "@0") -> ScriptCallRecord:
    """Return the broker receipt that the handle of the node at *key* names."""
    node = stack.store.get_node(stack.run_id, key)
    assert node is not None
    assert node.handle is not None
    script_run_id, _, call_id = node.handle.ref.partition(":")
    return stack.script_store.get_call(script_run_id, call_id)


def _dispatched_node(stack: WorkflowStack, *, script_run_id: str | None = None) -> WorkflowNode:
    """Write a `tool` node as the engine does before it dispatches: running, with a handle to one broker call."""
    return stack.store.put_node(
        WorkflowNode(
            run_id=stack.run_id,
            key="@0",
            kind="tool",
            lane="",
            idx=0,
            digest=node_digest("tool", _APPEND),
            request=_APPEND,
            state=WorkflowNodeState.RUNNING,
            handle=DispatchHandle("broker", f"{script_run_id or stack.attempt.run_id}:{_CALL}", dispatched=False),
            attempt=1,
        ),
    )


def _claim(stack: WorkflowStack) -> None:
    """Claim the dispatched node's call in the broker's store, with no task that owns it."""
    stack.script_store.claim_call(
        run_id=stack.attempt.run_id,
        call_id=_CALL,
        grant=ScriptToolGrant("counter", "append"),
        arguments_digest=digest_arguments({"line": "a"}),
    )


def _settled(stack: WorkflowStack, node: WorkflowNode) -> tuple[WorkflowNodeState, object, str | None]:
    """Settle *node* from its handle; return the state, the saved result and the saved error kind."""
    state = stack.engine.settle_from_handle(node)
    saved = stack.store.get_node(stack.run_id, node.key)
    assert saved is not None
    assert saved.state is state
    return state, saved.result, None if saved.error is None else str(saved.error["kind"])


@pytest.mark.asyncio
async def test_launch_starts_the_first_attempt_with_the_workflow_limits(stack: WorkflowStack) -> None:
    """The run names its attempt and runs; the attempt gets the workflow's own admission and rate limits."""
    run = stack.store.get_run(stack.run_id)

    assert (run.state, run.attempt, run.script_run_id) == (WorkflowRunState.RUNNING, 1, stack.attempt.run_id)
    assert stack.attempt.workflow_run_id == run.run_id
    assert stack.scripts.limits == [ScriptRunLimits(max_concurrent_runs=8, max_tool_calls_per_minute=600)]


@pytest.mark.asyncio
async def test_request_that_arrives_before_the_run_names_its_attempt_is_served(tmp_path: Path) -> None:
    """A fast script asks for its arguments while the launch is still recording the attempt; it must not be refused."""
    with workflow_stack(tmp_path) as stack:
        early: list[asyncio.Task[dict[str, object]]] = []
        launch = stack.scripts.run

        async def launch_then_ask(*args: object, **options: object) -> ScriptRunRecord:
            attempt = await launch(*args, **options)  # type: ignore[arg-type]
            early.append(asyncio.create_task(stack.engine.run_info(attempt)))
            await asyncio.sleep(0)
            return attempt

        stack.scripts.run = launch_then_ask  # type: ignore[method-assign]
        run = await stack.start(args={"n": 1})

        assert await early[0] == {"run_id": run.run_id, "attempt": 1, "args": {"n": 1}}


@pytest.mark.asyncio
async def test_tool_node_saves_its_handle_before_dispatch_and_its_result_after(stack: WorkflowStack) -> None:
    """The node row names the broker call before the tool runs, so a crash in between can read the outcome."""
    seen: list[WorkflowNode | None] = []
    stack.counting_tool.before_effect = lambda: seen.append(stack.store.get_node(stack.run_id, "@0"))

    assert await stack.result("@0", "tool", _APPEND) == "appended"

    assert seen[0] is not None
    assert seen[0].handle is not None
    assert (seen[0].handle.kind, seen[0].state) == ("broker", WorkflowNodeState.RUNNING)
    assert seen[0].handle.ref.startswith(f"{stack.attempt.run_id}:")
    assert stack.counting_tool.lines == ["a"]
    saved = stack.store.get_node(stack.run_id, "@0")
    assert saved is not None
    assert (saved.state, saved.result, saved.label) == (WorkflowNodeState.COMPLETED, "appended", "counter.append")


@pytest.mark.asyncio
async def test_redelivered_request_in_the_same_attempt_returns_the_same_node_and_runs_nothing(
    stack: WorkflowStack,
) -> None:
    """A lost response never repeats work, not even failed work."""
    first = await stack.submit("@0", "tool", tool_args("counter", "fail"))
    failed = await stack.engine.node_receipt(stack.attempt, first.id, wait=SETTLE_TIMEOUT_SECONDS)
    again = await stack.submit("@0", "tool", tool_args("counter", "fail"))

    assert (failed.state, failed.error) == ("failed", {"kind": "failed", "message": "The counter failed."})
    assert (again.id, again.state, stack.counting_tool.calls) == (first.id, "failed", 1)


@pytest.mark.asyncio
async def test_large_tool_result_is_returned_in_full(stack: WorkflowStack) -> None:
    """A 300 KiB result round-trips whole: neither the workspace auto-save nor the 64 KiB receipt cap bounds it."""
    size = 300 * 1024

    assert await stack.result("@0", "tool", tool_args("counter", "text", size=size)) == "x" * size
    assert _broker_call(stack).result == "x" * size


@pytest.mark.asyncio
async def test_large_result_survives_a_crash_between_its_receipt_and_its_node(
    stack: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A committed receipt holds the whole result, so the node recovers it and the tool is not called again."""
    size = 300 * 1024

    def crash(*_args: object, **_fields: object) -> bool:
        msg = "The primary stopped before the node was written."
        raise sqlite3.OperationalError(msg)

    monkeypatch.setattr(stack.store, "settle_node", crash)
    await stack.submit("@0", "tool", tool_args("counter", "text", size=size))
    await stack.until(lambda: stack.counting_tool.calls == 1)
    await stack.until(lambda: _broker_call(stack).state is not ScriptCallState.PENDING)

    stack = await stack.restart()
    await stack.end_attempt(ScriptRunState.INTERRUPTED)

    node = stack.store.get_node(stack.run_id, "@0")
    assert node is not None
    assert (node.state, node.result, stack.counting_tool.calls) == (WorkflowNodeState.COMPLETED, "x" * size, 1)


@pytest.mark.asyncio
async def test_shell_output_over_the_auto_save_size_is_never_swapped_for_a_workspace_file(stack: WorkflowStack) -> None:
    """A node's result lives in the run store, so all the shell tool keeps of the output, 50 KiB, arrives as text."""
    command = "head -c 51200 /dev/zero | tr '\\0' Z"

    result = await stack.result("@0", "tool", tool_args("shell", "run_shell_command", args=command, timeout=30))

    assert isinstance(result, dict)
    output = str(result["output"])
    assert (result["exit_code"], output.count("Z"), "mindroom_tool_output" in output) == (0, 51200, False)
    assert len(output) > 51200
    assert not list(stack.context.runtime_paths.storage_root.rglob("mindroom_tool_outputs/*"))


@pytest.mark.asyncio
async def test_result_over_eight_mebibytes_returns_a_reference_and_the_results_route_streams_all_of_it(
    stack: WorkflowStack,
) -> None:
    """The receipt of a result too large to inline names the route that returns all of it, also after a restart."""
    size = 8 * 1024 * 1024 + 1
    receipt = await stack.submit("@0", "tool", tool_args("counter", "text", size=size))
    while receipt.state == "running":
        receipt = await stack.engine.node_receipt(stack.attempt, receipt.id, wait=SETTLE_TIMEOUT_SECONDS)

    assert (receipt.state, receipt.result, receipt.result_ref) == ("completed", None, f"results/{receipt.id}")
    async with stack.http() as client:
        assert (await client.get(f"results/{receipt.id}")).json() == "x" * size

    stack = await stack.restart()
    async with stack.http() as client:
        again = await client.get(f"nodes/{receipt.id}")
        assert (again.json()["state"], again.json()["result_ref"]) == ("completed", f"results/{receipt.id}")
        assert (await client.get(f"results/{receipt.id}")).json() == "x" * size


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_code", [0, 3])
async def test_shell_node_result_carries_the_real_exit_status(stack: WorkflowStack, exit_code: int) -> None:
    """The status comes from the finished process, so no text the command prints can stand in for it."""
    command = f"echo 'Exit code 7'; exit {exit_code}"

    result = await stack.result("@0", "tool", tool_args("shell", "run_shell_command", args=command, timeout=30))

    assert isinstance(result, dict)
    assert result["exit_code"] == exit_code
    assert "Exit code 7" in str(result["output"])


@pytest.mark.asyncio
async def test_effect_start_is_recorded_after_approval_and_before_the_tool_runs(stack: WorkflowStack) -> None:
    """A call still waiting for approval is known not to have run; one the tool was invoked for is not."""
    stack.resolver.hold_approvals = asyncio.Event()
    seen: list[ScriptCallRecord] = []
    stack.counting_tool.before_effect = lambda: seen.append(_broker_call(stack))

    await stack.submit("@0", "tool", _APPEND)
    await stack.until(lambda: stack.resolver.asks == ["counter.append"])
    waiting = _broker_call(stack)
    stack.resolver.hold_approvals.set()
    await stack.until(lambda: stack.counting_tool.lines == ["a"])

    assert (waiting.state, waiting.effect_started_at) == (ScriptCallState.PENDING, None)
    assert seen[0].state is ScriptCallState.PENDING
    assert seen[0].effect_started_at is not None


@pytest.mark.asyncio
async def test_denied_approval_fails_the_node_as_denied_and_runs_nothing(stack: WorkflowStack) -> None:
    """The operator's approval rules stay applied to a workflow's calls."""
    stack.resolver.decision = ToolApprovalDecision(approved=False, reason="Not this line.")

    with pytest.raises(NodeFailed) as denied:
        await stack.result("@0", "tool", _APPEND)

    assert (denied.value.kind, str(denied.value)) == ("denied", "Not this line.")
    assert stack.counting_tool.lines == []


@pytest.mark.asyncio
async def test_tool_the_owner_no_longer_has_is_refused_at_the_call(stack: WorkflowStack) -> None:
    """A call answers to the owner's live tools: the launch snapshot is empty, and a removed tool is refused."""
    assert stack.attempt.grants == ()
    assert await stack.result("@0", "tool", _APPEND) == "appended"

    without_counter = stack.context.config.model_copy(
        update={"agents": {"watcher": AgentConfig(display_name="Watcher", tools=["shell"])}},
    )
    stack.resolver.context = replace(stack.context, config_provider=lambda: without_counter)
    with pytest.raises(NodeFailed) as refused:
        await stack.result("@1", "tool", tool_args("counter", "append", line="b"))

    assert refused.value.kind == "failed"
    assert "no longer available" in str(refused.value)
    assert stack.counting_tool.lines == ["a"]


@pytest.mark.asyncio
async def test_workflow_call_reaches_a_toolkit_plain_scripts_may_not_use(tmp_path: Path) -> None:
    """The broker resolves a workflow's call on the workflow tool surface, which includes the owner's `memory`."""
    with workflow_stack(tmp_path, extra_tools=("memory",)) as stack:
        await stack.start()

        listed = await stack.result("@0", "tool", tool_args("memory", "list_memories"))
        await stack.engine.close()

    assert isinstance(listed, str)


@pytest.mark.asyncio
async def test_attempt_the_run_no_longer_names_is_refused(stack: WorkflowStack) -> None:
    """Only the run's current attempt is served, so a process the run moved on from cannot act."""
    old = stack.attempt
    stack.store.update_run(stack.run_id, script_run_id="script-" + "0" * 32)

    with pytest.raises(SupersededAttemptError):
        await stack.engine.submit_node(old, NodeRequest("@0", "tool", _APPEND, {}))
    with pytest.raises(SupersededAttemptError):
        await stack.engine.finish(old, result="late", error=None)

    assert stack.counting_tool.calls == 0


@pytest.mark.asyncio
async def test_node_kind_this_release_cannot_run_is_refused_by_name(stack: WorkflowStack) -> None:
    """A script written for a later SDK gets a refusal it can read, and no node is saved."""
    with pytest.raises(WorkflowRunStoreError, match="cannot run a 'agent' call"):
        await stack.submit("@0", "agent", {"prompt": "Count the tides."})

    assert stack.engine.status(stack.run_id)[1] == []


@pytest.mark.asyncio
async def test_settle_from_handle_copies_a_completed_receipt(stack: WorkflowStack) -> None:
    """The work finished and only the node was not updated: its result is copied, never produced again."""
    node = _dispatched_node(stack)
    _claim(stack)
    stack.script_store.publish_call_result(
        run_id=stack.attempt.run_id,
        call_id=_CALL,
        state=ScriptCallState.COMPLETED,
        result="appended",
    )

    assert _settled(stack, node) == (WorkflowNodeState.COMPLETED, "appended", None)
    assert stack.counting_tool.calls == 0


@pytest.mark.asyncio
async def test_settle_from_handle_pending_receipt_whose_effect_started_is_indeterminate(stack: WorkflowStack) -> None:
    """The tool was invoked and no end was recorded, so the call may have run."""
    node = _dispatched_node(stack)
    _claim(stack)
    stack.script_store.mark_call_effect_started(stack.attempt.run_id, _CALL)

    assert _settled(stack, node) == (WorkflowNodeState.INDETERMINATE, None, "indeterminate")
    saved = stack.store.get_node(stack.run_id, "@0")
    assert saved is not None
    assert saved.error is not None
    assert saved.error["message"] == (
        "This call may already have run (attempt 1). Check its effect; "
        f'to run it again call resume_workflow(run_id, rerun=["n{saved.seq}"]).'
    )


@pytest.mark.asyncio
async def test_settle_from_handle_approval_wait_before_the_effect_is_safe_to_run_again(stack: WorkflowStack) -> None:
    """A call claimed and still waiting for hooks or approval never reached its tool."""
    node = _dispatched_node(stack)
    _claim(stack)

    assert _settled(stack, node) == (WorkflowNodeState.FAILED, None, "interrupted")


@pytest.mark.asyncio
async def test_settle_from_handle_with_pruned_call_records_is_indeterminate(stack: WorkflowStack) -> None:
    """With the attempt's records gone, a missing claim proves nothing."""
    node = _dispatched_node(stack, script_run_id="script-" + "0" * 32)

    assert _settled(stack, node) == (WorkflowNodeState.INDETERMINATE, None, "indeterminate")


@pytest.mark.asyncio
async def test_settle_from_handle_unclaimed_call_is_safe_to_run_again(stack: WorkflowStack) -> None:
    """The attempt's records exist and hold no claim for the call, so it never started."""
    node = _dispatched_node(stack)

    assert _settled(stack, node) == (WorkflowNodeState.FAILED, None, "interrupted")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("effect_started", "call_state", "error_kind", "node_state", "node_error_kind"),
    [
        (False, ScriptCallState.FAILED, "capability_revoked", WorkflowNodeState.FAILED, "failed"),
        (False, ScriptCallState.FAILED, "approval_denied", WorkflowNodeState.FAILED, "denied"),
        (False, ScriptCallState.INDETERMINATE, "indeterminate", WorkflowNodeState.FAILED, "interrupted"),
        (True, ScriptCallState.INDETERMINATE, "indeterminate", WorkflowNodeState.INDETERMINATE, "indeterminate"),
        (True, ScriptCallState.FAILED, "result_too_large", WorkflowNodeState.INDETERMINATE, "indeterminate"),
        (True, ScriptCallState.FAILED, "invalid_tool_result", WorkflowNodeState.INDETERMINATE, "indeterminate"),
        (True, ScriptCallState.FAILED, "tool_failure", WorkflowNodeState.FAILED, "failed"),
    ],
)
async def test_settle_from_handle_reads_an_ended_receipt_by_whether_the_tool_was_invoked(
    stack: WorkflowStack,
    effect_started: bool,
    call_state: ScriptCallState,
    error_kind: str,
    node_state: WorkflowNodeState,
    node_error_kind: str,
) -> None:
    """A refusal before the tool is a failure; an end the receipt cannot state after the tool ran is indeterminate."""
    node = _dispatched_node(stack)
    _claim(stack)
    if effect_started:
        stack.script_store.mark_call_effect_started(stack.attempt.run_id, _CALL)
    stack.script_store.publish_call_result(
        run_id=stack.attempt.run_id,
        call_id=_CALL,
        state=call_state,
        error={"kind": error_kind, "message": "Why the call ended.", "retryable": False},
    )

    assert _settled(stack, node) == (node_state, None, node_error_kind)


@pytest.mark.asyncio
async def test_receipt_that_could_not_be_stored_after_the_effect_is_never_safe_to_run_again(
    stack: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What the broker answers for a receipt it could not store proves nothing: the tool ran, so the call may have."""

    def refuse(**_fields: object) -> ScriptCallRecord:
        msg = "disk I/O error"
        raise sqlite3.OperationalError(msg)

    monkeypatch.setattr(stack.script_store, "publish_call_result", refuse)

    with pytest.raises(NodeFailed) as lost:
        await stack.result("@0", "tool", _APPEND)

    assert (lost.value.kind, stack.counting_tool.lines) == ("indeterminate", ["a"])
    assert _broker_call(stack).state is ScriptCallState.PENDING


@pytest.mark.asyncio
async def test_shell_answer_without_an_exit_status_may_have_run(stack: WorkflowStack) -> None:
    """No status says the command ended: its answer was lost on the way, so nothing proves it did not run."""
    node = _dispatched_node(stack)
    stack.script_store.claim_call(
        run_id=stack.attempt.run_id,
        call_id=_CALL,
        grant=ScriptToolGrant("shell", "run_shell_command"),
        arguments_digest=digest_arguments({"args": "make deploy"}),
    )
    stack.script_store.mark_call_effect_started(stack.attempt.run_id, _CALL)
    stack.script_store.publish_call_result(
        run_id=stack.attempt.run_id,
        call_id=_CALL,
        state=ScriptCallState.COMPLETED,
        result="Error: Shell supervisor did not respond in time.",
    )

    assert _settled(stack, node) == (WorkflowNodeState.INDETERMINATE, None, "indeterminate")


@pytest.mark.asyncio
async def test_workflow_attempt_cannot_use_the_plain_call_routes(stack: WorkflowStack) -> None:
    """A workflow call enters through its node and nowhere else, so no plain claim can stand in for a dispatch."""
    authorization = f"Bearer {stack.token}"
    assert await stack.result("@0", "tool", _APPEND) == "appended"
    plain = ScriptToolCallRequest(
        stack.attempt.run_id,
        "call-plain",
        ScriptToolGrant("counter", "append"),
        {"line": "b"},
    )

    with pytest.raises(ScriptBrokerAuthenticationError):
        await stack.broker.accept_authenticated(plain, authorization)
    with pytest.raises(ScriptBrokerAuthenticationError):
        await stack.broker.get_authenticated(stack.attempt.run_id, _broker_call(stack).call_id, authorization)

    assert stack.counting_tool.lines == ["a"]
    assert stack.script_store.pending_calls(stack.attempt.run_id) == []
    assert stack.broker.authenticate(stack.attempt.run_id, authorization).run_id == stack.attempt.run_id


@pytest.mark.asyncio
async def test_effect_cannot_start_for_a_call_that_already_ended(stack: WorkflowStack) -> None:
    """A call settled while it waited is never invoked afterwards, so its missing stamp stays true."""
    _claim(stack)
    stack.script_store.settle_orphaned_call(run_id=stack.attempt.run_id, call_id=_CALL, error={"kind": "indeterminate"})

    with pytest.raises(ValueError, match="revoked"):
        stack.script_store.mark_call_effect_started(stack.attempt.run_id, _CALL)


@pytest.mark.asyncio
async def test_finish_then_exit_completes_run_and_wakes_waiters(stack: WorkflowStack) -> None:
    """The report is stored when the script posts it, and the run completes when its process has exited."""
    waiter = asyncio.create_task(stack.engine.wait(stack.run_id))

    await stack.engine.finish(stack.attempt, result={"n": 2}, error=None)
    await asyncio.sleep(0)
    assert not waiter.done()
    assert stack.store.get_run(stack.run_id).state is WorkflowRunState.RUNNING
    await stack.end_attempt(ScriptRunState.EXITED)
    done = await asyncio.wait_for(waiter, SETTLE_TIMEOUT_SECONDS)

    assert (done.state, done.result, done.error) == (WorkflowRunState.COMPLETED, {"n": 2}, None)
    assert done.finished_at is not None


@pytest.mark.asyncio
async def test_exit_without_finish_fails_and_says_why(stack: WorkflowStack) -> None:
    """A script that ends without reporting, as one that calls `sys.exit(0)` does, fails with the reason."""
    await stack.end_attempt(ScriptRunState.EXITED)

    run = await stack.engine.wait(stack.run_id)
    assert (run.state, run.error) == (WorkflowRunState.FAILED, "The script ended without returning from main(args).")


@pytest.mark.asyncio
async def test_failed_exit_keeps_the_reported_error_or_the_end_of_the_output(tmp_path: Path) -> None:
    """A traceback the script posted is the run's error; a process that died without one leaves its last output."""
    with workflow_stack(tmp_path) as stack:
        await stack.start()
        await stack.engine.finish(stack.attempt, result=None, error="Traceback: the tide table was empty.")
        await stack.end_attempt(ScriptRunState.FAILED)
        reported = await stack.engine.wait(stack.run_id)

        await stack.start()
        await stack.end_attempt(ScriptRunState.FAILED, output="x" * 5000 + "\nKilled")
        died = await stack.engine.wait(stack.run_id)
        await stack.engine.close()

    assert (reported.state, reported.error) == (WorkflowRunState.FAILED, "Traceback: the tide table was empty.")
    assert died.state is WorkflowRunState.FAILED
    assert died.error is not None
    assert died.error.endswith("\nKilled")
    assert len(died.error) == 4096


@pytest.mark.asyncio
async def test_interrupted_attempt_interrupts_the_run_with_its_reason(stack: WorkflowStack) -> None:
    """An attempt the runtime stopped maps to itself, and its unsettled nodes are read from their handles."""
    assert await stack.result("@0", "tool", _APPEND) == "appended"
    stack.resolver.hold_approvals = asyncio.Event()
    await stack.submit("@1", "tool", tool_args("counter", "append", line="b"))
    await stack.until(lambda: len(stack.resolver.asks) == 2)

    await stack.end_attempt(ScriptRunState.INTERRUPTED, RUNTIME_RESTARTED)

    run, nodes = stack.engine.status(stack.run_id)
    assert (run.state, run.error) == (WorkflowRunState.INTERRUPTED, RUNTIME_RESTARTED)
    assert [(node.key, node.state) for node in nodes] == [
        ("@0", WorkflowNodeState.COMPLETED),
        ("@1", WorkflowNodeState.FAILED),
    ]
    assert nodes[1].error is not None
    assert nodes[1].error["kind"] == "interrupted"


@pytest.mark.asyncio
async def test_launch_is_idempotent_and_never_starts_an_ended_run(stack: WorkflowStack) -> None:
    """A reserved run gets one attempt from one launch: a repeated launch, or one after a cancel, starts no process."""
    again = await asyncio.gather(*(stack.engine.launch(stack.run_id, stack.context) for _ in range(2)))

    assert [(run.attempt, run.script_run_id) for run in again] == [(1, stack.attempt.run_id)] * 2
    await stack.engine.cancel(stack.run_id, reason="Stopped by the user.")
    late = await stack.engine.launch(stack.run_id, stack.context)

    assert (late.state, late.attempt, len(stack.scripts.launched)) == (WorkflowRunState.CANCELLED, 1, 1)


@pytest.mark.asyncio
async def test_launch_that_finds_its_run_ended_stops_the_attempt_it_started(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancel accepted while the process starts wins on the run row: the run never names the new attempt."""
    with workflow_stack(tmp_path) as stack:
        start = stack.scripts.run

        async def cancelled_meanwhile(context: ToolRuntimeContext, **options: Any) -> ScriptRunRecord:  # noqa: ANN401
            attempt = await start(context, **options)
            stack.store.update_run(options["workflow_run_id"], state=WorkflowRunState.CANCELLED, error="Stopped.")
            return attempt

        monkeypatch.setattr(stack.scripts, "run", cancelled_meanwhile)

        run = await stack.start()

        started = stack.script_store.get_run(stack.scripts.launched[0].run_id)
        await stack.engine.close()
    assert (run.state, run.error, run.script_run_id) == (WorkflowRunState.CANCELLED, "Stopped.", None)
    assert started.state is ScriptRunState.CANCELLED


@pytest.mark.asyncio
@pytest.mark.parametrize("maintenance_first", [False, True])
async def test_accepted_cancel_wins_over_a_stored_report(stack: WorkflowStack, maintenance_first: bool) -> None:
    """Cancel and the maintenance pass use one rule, so a report stored just before the cancel cannot complete the run."""
    await stack.engine.finish(stack.attempt, result="done", error=None)
    if maintenance_first:
        await stack.scripts.revoke(stack.attempt.run_id, reason="Stopped by the user.")
        await stack.scripts.reconcile_durable(run_id=stack.attempt.run_id)
        await stack.engine.reconcile()

    cancelled, _ = await asyncio.gather(
        stack.engine.cancel(stack.run_id, reason="Stopped by the user."),
        stack.engine.reconcile(),
    )

    run = await stack.engine.wait(stack.run_id)
    assert (run.state, run.error) == (WorkflowRunState.CANCELLED, "Stopped by the user.")
    assert cancelled == run


@pytest.mark.asyncio
async def test_cancel_after_the_script_ended_keeps_what_the_script_reported(stack: WorkflowStack) -> None:
    """A cancel that arrives after the process ended was never accepted, so the run ends as its report says."""
    await stack.engine.finish(stack.attempt, result="done", error=None)
    await stack.scripts.end(stack.attempt.run_id, ScriptRunState.EXITED)

    run = await stack.engine.cancel(stack.run_id, reason="Stopped by the user.")

    assert (run.state, run.result, run.error) == (WorkflowRunState.COMPLETED, "done", None)


@pytest.mark.asyncio
async def test_failed_exit_after_a_success_report_fails_the_run(stack: WorkflowStack) -> None:
    """A process that died after reporting success did not end well, and the run says how it died."""
    await stack.engine.finish(stack.attempt, result="done", error=None)

    await stack.end_attempt(ScriptRunState.FAILED, output="Segmentation fault")

    run = await stack.engine.wait(stack.run_id)
    assert (run.state, run.error) == (WorkflowRunState.FAILED, "Segmentation fault")


@pytest.mark.asyncio
async def test_report_after_the_run_ended_is_not_stored(stack: WorkflowStack) -> None:
    """A report reaches the run only while it runs, so a leftover process cannot rewrite an ended run."""
    attempt = stack.attempt
    cancelled = await stack.engine.cancel(stack.run_id, reason="Stopped by the user.")

    await stack.engine.finish(attempt, result="done", error=None)

    assert stack.store.get_run(stack.run_id) == cancelled


@pytest.mark.asyncio
async def test_maintenance_pass_ends_every_run_it_can_and_prunes_expired_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run whose attempt record is gone ends interrupted, and neither it nor a failing run stops the pass."""
    with workflow_stack(tmp_path) as stack:
        script_runtime = SimpleNamespace(after_reconcile=None, retention_seconds=3600.0)
        lifecycle = WorkflowRuntimeLifecycle(engine=stack.engine, script_runtime=script_runtime)  # type: ignore[arg-type]
        await lifecycle.start()
        orphan = await stack.start()
        unsettled = _dispatched_node(stack)
        await stack.scripts.end(stack.attempt.run_id, ScriptRunState.EXITED)
        assert stack.script_store.prune_terminal_run(stack.attempt.run_id, finished_before="9999-01-01T00:00:00Z")
        broken = await stack.start()
        read_attempt = stack.script_store.get_run

        def locked(run_id: str) -> ScriptRunRecord:
            if run_id == broken.script_run_id:
                msg = "database is locked"
                raise sqlite3.OperationalError(msg)
            return read_attempt(run_id)

        monkeypatch.setattr(stack.script_store, "get_run", locked)
        reported = await stack.start()
        await stack.engine.finish(stack.attempt, result="done", error=None)
        await stack.scripts.end(stack.attempt.run_id, ScriptRunState.EXITED)

        await script_runtime.after_reconcile()

        states = {run.run_id: (run.state, run.error) for run in stack.store.list_runs()}
        node = stack.store.get_node(orphan.run_id, unsettled.key)
        script_runtime.retention_seconds = 0.0
        await script_runtime.after_reconcile()
        left = [run.run_id for run in stack.store.list_runs()]
        await lifecycle.shutdown()
    assert states[orphan.run_id] == (WorkflowRunState.INTERRUPTED, "The record of the run's attempt is gone.")
    assert states[reported.run_id] == (WorkflowRunState.COMPLETED, None)
    assert states[broken.run_id] == (WorkflowRunState.RUNNING, None)
    assert node is not None
    assert node.state is WorkflowNodeState.INDETERMINATE
    assert left == [broken.run_id]


@pytest.mark.asyncio
async def test_store_writes_wait_for_the_database_lock_off_the_event_loop(stack: WorkflowStack) -> None:
    """Another writer holding the database delays a log line and a node, and never the loop that serves every run."""
    other_writer = sqlite3.connect(stack.store.database_path, isolation_level=None, check_same_thread=False)
    other_writer.execute("BEGIN IMMEDIATE")
    release = threading.Timer(0.5, other_writer.execute, ("COMMIT",))
    release.start()
    ticks = 0

    async def tick() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    ticker = asyncio.create_task(tick())
    try:
        await asyncio.gather(
            stack.engine.append_log(stack.attempt, "The tide is in."),
            *(stack.submit("@0", "tool", _APPEND) for _ in range(2)),
        )
        ticks_while_waiting = ticks
        assert await stack.result("@0", "tool", _APPEND) == "appended"
    finally:
        ticker.cancel()
        release.join()
        other_writer.close()

    assert ticks_while_waiting >= 10
    assert (stack.store.log(stack.run_id), stack.counting_tool.lines) == (["The tide is in."], ["a"])


@pytest.mark.asyncio
async def test_cancel_revokes_attempt_and_keeps_completed_nodes(stack: WorkflowStack) -> None:
    """Cancelling ends the attempt and the run, runs nothing that was still waiting, and keeps what finished."""
    assert await stack.result("@0", "tool", _APPEND) == "appended"
    stack.resolver.hold_approvals = asyncio.Event()
    await stack.submit("@1", "tool", tool_args("counter", "append", line="b"))
    await stack.until(lambda: len(stack.resolver.asks) == 2)
    token = stack.token

    run = await stack.engine.cancel(stack.run_id, reason="Stopped by the user.")

    attempt = stack.attempt
    assert (run.state, run.error) == (WorkflowRunState.CANCELLED, "Stopped by the user.")
    assert (attempt.state, attempt.cancellation_reason) == (ScriptRunState.CANCELLED, "Stopped by the user.")
    with pytest.raises(ScriptBrokerAuthenticationError):
        stack.broker.authenticate(attempt.run_id, f"Bearer {token}")
    _, nodes = stack.engine.status(stack.run_id)
    assert [(node.key, node.state, node.result) for node in nodes] == [
        ("@0", WorkflowNodeState.COMPLETED, "appended"),
        ("@1", WorkflowNodeState.FAILED, None),
    ]
    stack.resolver.hold_approvals.set()
    await asyncio.sleep(0)
    assert stack.counting_tool.lines == ["a"]
    assert await stack.engine.cancel(stack.run_id, reason="Again.") == run
