"""Tests for what a fan-out of a workflow script leaves in the run: its error nodes, and the report's first line.

Level: engine harness (`WorkflowStack`); the test plays the script by calling the engine's gateway methods.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

from mindroom.orchestration import tool_job_runtime as coordinator_module
from mindroom.script_runs import manager as manager_module
from mindroom.script_runs.models import ScriptRunState
from mindroom.workflow_runs import engine as engine_module
from mindroom.workflow_runs.models import WorkflowNodeState, WorkflowRunState
from tests.tool_job_helpers import wait_for_status
from tests.workflow_helpers import NodeFailed, WorkflowStack, tool_args, workflow_stack

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mindroom.workflow_runs.models import WorkflowNode

_FOREACH: dict[str, object] = {"op": "foreach"}
_BUG: dict[str, object] = {"type": "KeyError", "message": "'x'"}
_SOURCE = "def main(args):\n    return 'the report'\n"


@pytest_asyncio.fixture
async def stack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[WorkflowStack]:
    """The harness with the real job runtime; a test starts its run directly or as a job."""
    monkeypatch.setattr(manager_module, "background_script_supervision_supported", lambda: True)
    monkeypatch.setattr(engine_module, "_RECHECK_SECONDS", 0.05)
    with workflow_stack(tmp_path, extra_tools=("workflow",)) as stack:
        stack.continues = coordinator_module._continues
        await stack.open_jobs()
        yield stack
        await stack.close()


def _node(stack: WorkflowStack, key: str) -> WorkflowNode:
    node = stack.store.get_node(stack.run_id, key)
    assert node is not None
    return node


@pytest.mark.asyncio
async def test_error_of_the_scripts_own_code_is_a_failed_node_that_ran_nothing(stack: WorkflowStack) -> None:
    """What a lane's own Python raised is saved where it happened, ended, with nothing dispatched for it."""
    await stack.start()
    await stack.submit("@0", "group", _FOREACH, {"members": ["/0:a", "/0:x"]})

    receipt = await stack.submit("/0:x@0", "error", _BUG, {"phase": "Research"})

    node = _node(stack, "/0:x@0")
    assert (receipt.state, receipt.error) == ("failed", {"kind": "error", "message": "KeyError: 'x'"})
    assert (node.state, node.label, node.phase, node.handle) == (WorkflowNodeState.FAILED, "KeyError", "Research", None)
    assert node.ended_at is not None


@pytest.mark.asyncio
async def test_lane_that_failed_in_its_own_code_goes_on_from_there_after_the_fix(stack: WorkflowStack) -> None:
    """The next attempt reuses what the lane had finished and runs the call that now stands where the error was."""
    await stack.start()
    await stack.submit("@0", "group", _FOREACH, {"members": ["/0:x"]})
    assert await stack.result("/0:x@0", "tool", tool_args("counter", "append", line="a")) == "appended"
    await stack.submit("/0:x@1", "error", _BUG)
    await stack.end_attempt(ScriptRunState.FAILED)

    await stack.resume(stack.run_id)
    assert (await stack.submit("@0", "group", _FOREACH, {"members": ["/0:x"]})).cached
    assert (await stack.submit("/0:x@0", "tool", tool_args("counter", "append", line="a"))).cached
    assert await stack.result("/0:x@1", "tool", tool_args("counter", "append", line="b")) == "appended"

    node = _node(stack, "/0:x@1")
    assert (node.kind, node.generation, stack.counting_tool.lines) == ("tool", 2, ["a", "b"])


@pytest.mark.asyncio
async def test_completed_run_with_caught_failures_says_so_in_the_delivery(stack: WorkflowStack) -> None:
    """A run whose script caught failures completes, and its report opens with the calls that failed."""
    handle = json.loads(await stack.call("start_workflow", source=_SOURCE))
    stack.run_id = handle["run_id"]
    await stack.until(lambda: stack.store.get_run(stack.run_id).state is WorkflowRunState.RUNNING)
    await stack.submit("@0", "group", _FOREACH, {"members": ["/0:a", "/0:b", "/0:x"]})
    assert await stack.result("/0:a@0", "tool", tool_args("counter", "append", line="a")) == "appended"
    with pytest.raises(NodeFailed):
        await stack.result("/0:b@0", "tool", tool_args("counter", "fail"))
    await stack.submit("/0:x@0", "error", _BUG)

    await stack.finish_attempt(result="the report")
    await wait_for_status(stack.jobs, handle["job_id"], "completed")

    [job] = [job for job in stack.jobs.retained_jobs() if job.job_id == handle["job_id"]]
    first, _, report = str(await stack.collect(job)).partition("\n\n")
    assert first == '2 calls failed (n3, n4); get_workflow(run_id, nodes="failed") shows them.'
    assert (report, stack.store.get_run(stack.run_id).result) == ("the report", "the report")
    # The run's own record keeps the report as the script returned it.
    assert json.loads(await stack.call("get_workflow", run_id=stack.run_id))["result"] == "the report"


@pytest.mark.asyncio
async def test_completed_run_without_failures_and_a_failed_run_deliver_no_such_line(stack: WorkflowStack) -> None:
    """The line is for failures a run completed in spite of: no failure has none, and a failed run says why it failed."""
    handle = json.loads(await stack.call("start_workflow", source=_SOURCE))
    stack.run_id = handle["run_id"]
    await stack.until(lambda: stack.store.get_run(stack.run_id).state is WorkflowRunState.RUNNING)
    await stack.finish_attempt(result="the report")
    await wait_for_status(stack.jobs, handle["job_id"], "completed")
    [job] = [job for job in stack.jobs.retained_jobs() if job.job_id == handle["job_id"]]
    assert await stack.collect(job) == "the report"

    second = json.loads(await stack.call("start_workflow", source=_SOURCE))
    stack.run_id = second["run_id"]
    await stack.until(lambda: stack.store.get_run(stack.run_id).state is WorkflowRunState.RUNNING)
    with pytest.raises(NodeFailed):
        await stack.result("@0", "tool", tool_args("counter", "fail"))
    await stack.finish_attempt(error="Traceback: the script raised")
    await wait_for_status(stack.jobs, second["job_id"], "failed")
    [failed] = [job for job in stack.jobs.retained_jobs() if job.job_id == second["job_id"]]
    assert await stack.collect(failed) == "Traceback: the script raised"
