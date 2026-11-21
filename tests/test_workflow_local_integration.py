"""Tests for a workflow attempt as a real child process that talks to the real gateway routes.

Level: local integration (`LocalWorkflowStack`): the real script manager in local mode, the shell supervisor, the
shim and the SDK in the child, the broker and the engine, served over a real listener.
"""

from __future__ import annotations

import asyncio
import json
import socket
import sys
from contextlib import closing
from functools import partial
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

from mindroom.api.script_gateway import serve_listener
from mindroom.api.workflow_gateway import bind_workflow_engine
from mindroom.orchestration import tool_job_runtime as coordinator_module
from mindroom.script_runs import manager as manager_module
from mindroom.script_runs.manager import ScriptRunManager
from mindroom.script_runs.models import ScriptRunState
from mindroom.script_runs.reasons import MAX_RUNTIME_EXCEEDED, RUNTIME_RESTARTED
from mindroom.script_runs.worker_client import ScriptWorkerClient
from mindroom.workflow_runs.engine import WorkflowEngine
from mindroom.workflow_runs.models import WorkflowNodeState, WorkflowRunState
from tests.conftest import SETTLE_TIMEOUT_SECONDS, bound_worker_launch_config
from tests.tool_job_helpers import wait_for_status
from tests.workflow_helpers import WorkflowStack, set_run, workflow_stack

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from fastapi import FastAPI

    from mindroom.tool_jobs.runtime import BackgroundJob

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Local workflow runs are Linux only.")

_SCRIPT = """\
from mindroom.workflow import log, shell, tool


def main(args):
    log("hi")
    appended = tool("counter", "append", line=args["line"])
    return {"n": args["n"] + 1, "code": shell("exit 3").exit_code, "appended": appended}
"""


class LocalWorkflowStack(WorkflowStack):
    """The engine harness with the real script manager: an attempt is a real child process of this test."""

    gateway_url = ""
    served: FastAPI | None = None
    """The app the listener serves; a restarted primary answers on the same port with its own engine."""

    def _open(self) -> None:
        super()._open()
        self.scripts = ScriptRunManager(  # type: ignore[assignment]
            store=self.script_store,
            broker=self.broker,
            worker_client=ScriptWorkerClient(),
            worker_backend=None,
            gateway_url=self.gateway_url,
            cancellation_grace_seconds=0.2,
            cancellation_poll_interval_seconds=0.02,
        )
        self.engine = WorkflowEngine(self.store, self.scripts, self.broker)
        self.context.orchestrator.workflow_engine = self.engine  # type: ignore[union-attr]
        if self.served is not None:
            bind_workflow_engine(self.served, self.engine)


@pytest_asyncio.fixture
async def local_stack(tmp_path: Path) -> AsyncIterator[LocalWorkflowStack]:
    """A local stack whose gateway listens on a real port, as the child process needs."""
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    with (
        workflow_stack(tmp_path, stack_type=LocalWorkflowStack, extra_tools=("workflow",)) as stack,
        bound_worker_launch_config(stack.context.config),
    ):
        assert isinstance(stack, LocalWorkflowStack)
        stack.gateway_url = stack.scripts.gateway_url = f"http://127.0.0.1:{port}/api/script-gateway"
        stack.served = stack.app()
        stack.continues = coordinator_module._continues
        await stack.open_jobs()
        async with serve_listener(
            stack.served,
            name="workflow_test_gateway",
            host="127.0.0.1",
            port=port,
            log_level="WARNING",
            ws="none",
            access_log=False,
        ):
            yield stack
            for run in stack.store.list_runs(include_finished=False):
                await stack.engine.cancel(run.run_id, reason="The test ended.")
            await stack.close()


@pytest.mark.asyncio
async def test_real_local_script_returns_its_report(local_stack: LocalWorkflowStack) -> None:
    """A real script reads its arguments, logs, calls a tool and the shell, and its return value is the run's result."""
    run = await local_stack.start(_SCRIPT, args={"n": 1, "line": "a"})

    done = await local_stack.engine.wait(run.run_id)

    assert done.error is None
    assert (done.state, done.result) == (WorkflowRunState.COMPLETED, {"n": 2, "code": 3, "appended": "appended"})
    assert local_stack.counting_tool.lines == ["a"]
    assert local_stack.store.log(run.run_id) == ["hi"]
    _, nodes = local_stack.engine.status(run.run_id)
    assert [(node.label, node.state) for node in nodes] == [
        ("counter.append", WorkflowNodeState.COMPLETED),
        ("shell.run_shell_command", WorkflowNodeState.COMPLETED),
    ]


@pytest.mark.asyncio
async def test_real_local_script_that_raises_fails_the_run_with_its_traceback(local_stack: LocalWorkflowStack) -> None:
    """The traceback the SDK posts is the run's error, with the user's own line in it."""
    run = await local_stack.start("def main(args):\n    raise ValueError('no tide table')\n")

    done = await local_stack.engine.wait(run.run_id)

    assert done.state is WorkflowRunState.FAILED
    assert done.error is not None
    assert "ValueError: no tide table" in done.error
    assert "line 2, in main" in done.error


_WRITE_ONCE = """\
from mindroom.workflow import tool


def main(args):
    return {"appended": tool("counter", "append", line="a")}
"""


async def _start_job(stack: LocalWorkflowStack, source: str, **arguments: object) -> dict[str, str]:
    """Start a run as its job, as an agent's `start_workflow` call does, and return the handle."""
    handle = json.loads(await stack.call("start_workflow", source=source, **arguments))
    stack.run_id = handle["run_id"]
    return handle


async def _ended_job(stack: LocalWorkflowStack, job_id: str) -> BackgroundJob:
    async with asyncio.timeout(SETTLE_TIMEOUT_SECONDS):
        return (await stack.jobs.wait(job_id, owner=stack.context.execution_identity, depth=0)).job


async def _kill_at(
    stack: LocalWorkflowStack,
    tmp_path: Path,
    barrier: str,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict[str, str], str]:
    """Run the one-write script, kill the primary while the write stands at *barrier*, and start a new primary.

    Returns the job's handle and the first attempt. The new primary runs without barriers, as
    `tests/workflow_live/kill_after_write.py` restarts the backend.
    """
    faults = tmp_path / "faults"
    faults.mkdir()
    if barrier == "before_settle":
        (faults / "before_receipt.release").touch()
    monkeypatch.setenv("MINDROOM_WORKFLOW_FAULT_DIR", str(faults))
    handle = await _start_job(stack, _WRITE_ONCE)
    # The barrier's own mark, never the counter file: the write is certain to have happened when it shows.
    await stack.until((faults / f"{barrier}.reached").exists)
    first = stack.attempt.run_id
    await stack.restart(kill=True)
    monkeypatch.delenv("MINDROOM_WORKFLOW_FAULT_DIR")
    return handle, first


@pytest.mark.asyncio
async def test_real_run_killed_after_the_receipt_resumes_writes_once_and_reports_once(
    local_stack: LocalWorkflowStack,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The acceptance test with a real script process, receipt saved: one write, the node reused, one report."""
    handle, first = await _kill_at(local_stack, tmp_path, "before_settle", monkeypatch)

    await local_stack.next_primary()
    await wait_for_status(local_stack.jobs, handle["job_id"], "completed")
    [job] = local_stack.jobs.retained_jobs()

    run, [node] = local_stack.engine.status(local_stack.run_id)
    attempt = local_stack.script_store.get_run(first)
    assert (attempt.state, attempt.cancellation_reason) == (ScriptRunState.INTERRUPTED, RUNTIME_RESTARTED)
    assert local_stack.counting_tool.lines == ["a"]
    assert (node.state, node.generation, node.attempt) == (WorkflowNodeState.COMPLETED, 1, 2)
    assert (run.state, run.result, run.attempt) == (WorkflowRunState.COMPLETED, {"appended": "appended"}, 2)
    assert (job.status, json.loads(str(await local_stack.collect(job)))) == ("completed", {"appended": "appended"})
    # One job, and one follow-up that delivers it, however often the pass that admits follow-ups runs.
    [follow_up] = await local_stack.follow_ups()
    assert (job.job_id, await local_stack.follow_ups()) == (handle["job_id"], [follow_up])
    assert len(local_stack.script_store.list_runs()) == 2


@pytest.mark.asyncio
async def test_real_run_killed_before_the_receipt_is_indeterminate_and_writes_once(
    local_stack: LocalWorkflowStack,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The acceptance test with a real script process, no receipt: the write is not made again and no report comes.

    The script does not catch the error, so the run fails with it, and a resume that names the call runs it again.
    """
    handle, _ = await _kill_at(local_stack, tmp_path, "before_receipt", monkeypatch)

    await local_stack.next_primary()
    await wait_for_status(local_stack.jobs, handle["job_id"], "failed")
    [job] = local_stack.jobs.retained_jobs()

    run, [node] = local_stack.engine.status(local_stack.run_id)
    assert local_stack.counting_tool.lines == ["a"]
    assert (node.state, node.generation) == (WorkflowNodeState.INDETERMINATE, 1)
    # The second attempt ran and failed at the call: the failure is delivered once, and no report is.
    assert (run.state, run.result, run.attempt, job.status) == (WorkflowRunState.FAILED, None, 2, "failed")
    assert len(await local_stack.follow_ups()) == 1
    assert await local_stack.collect(job) == run.error
    assert run.error is not None
    assert (
        f'ToolError: This call may already have run (attempt 1). Check its effect; to run it again call resume_workflow(run_id, rerun=["n{node.seq}"]).'
        in run.error
    )

    resumed = json.loads(await local_stack.call("resume_workflow", run_id=run.run_id, rerun=[f"n{node.seq}"]))
    again = await _ended_job(local_stack, resumed["job_id"])

    assert (again.status, local_stack.counting_tool.lines) == ("completed", ["a", "a"])


_SLEEP_ONCE = """\
import pathlib
import time

from mindroom.workflow import tool


def main(args):
    tool("counter", "append", line="a")
    marker = pathlib.Path(args["marker"])
    if not marker.exists():
        marker.write_text("slept")
        time.sleep(600)
    return "done"
"""


@pytest.mark.asyncio
async def test_attempt_that_hits_its_process_limit_is_resumed_while_working_time_is_left(
    local_stack: LocalWorkflowStack,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The process limit of an attempt ends that process only: the run goes on, with the working time it had left."""
    month = manager_module._WORKFLOW_ATTEMPT_SECONDS
    monkeypatch.setattr(manager_module, "_WORKFLOW_ATTEMPT_SECONDS", 1)
    marker = tmp_path / "slept"
    handle = await _start_job(local_stack, _SLEEP_ONCE, args={"marker": str(marker)})
    await local_stack.until(marker.exists)
    first = local_stack.attempt.run_id
    await asyncio.to_thread(partial(set_run, local_stack.store, local_stack.run_id, runtime_seconds_left=1000.0))
    await asyncio.sleep(1)
    monkeypatch.setattr(manager_module, "_WORKFLOW_ATTEMPT_SECONDS", month)

    ended = await local_stack.scripts.reconcile_durable(run_id=first)
    assert (ended.state, ended.cancellation_reason) == (ScriptRunState.INTERRUPTED, MAX_RUNTIME_EXCEEDED)
    assert await local_stack.maintain() == [local_stack.run_id]
    job = await _ended_job(local_stack, handle["job_id"])

    run = local_stack.store.get_run(local_stack.run_id)
    assert (job.status, run.state, run.result, run.attempt) == ("completed", WorkflowRunState.COMPLETED, "done", 2)
    assert local_stack.counting_tool.lines == ["a"]
    assert run.runtime_seconds_left is not None
    assert 0 < run.runtime_seconds_left <= 1000
