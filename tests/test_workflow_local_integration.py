"""Tests for a workflow attempt as a real child process that talks to the real gateway routes.

Level: local integration (`LocalWorkflowStack`): the real script manager in local mode, the shell supervisor, the
shim and the SDK in the child, the broker and the engine, served over a real listener.
"""

from __future__ import annotations

import socket
import sys
from contextlib import closing
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

from mindroom.api.script_gateway import serve_listener
from mindroom.script_runs.manager import ScriptRunManager
from mindroom.script_runs.worker_client import ScriptWorkerClient
from mindroom.workflow_runs.engine import WorkflowEngine
from mindroom.workflow_runs.models import WorkflowNodeState, WorkflowRunState
from tests.conftest import bound_worker_launch_config
from tests.workflow_helpers import WorkflowStack, workflow_stack

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

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

    def _open(self) -> None:
        super()._open()
        self.scripts = ScriptRunManager(  # type: ignore[assignment]
            store=self.script_store,
            broker=self.broker,
            worker_client=ScriptWorkerClient(),
            worker_backend=None,
            gateway_url="",
            cancellation_grace_seconds=0.2,
            cancellation_poll_interval_seconds=0.02,
        )
        self.engine = WorkflowEngine(self.store, self.scripts, self.broker)


@pytest_asyncio.fixture
async def local_stack(tmp_path: Path) -> AsyncIterator[LocalWorkflowStack]:
    """A local stack whose gateway listens on a real port, as the child process needs."""
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    with (
        workflow_stack(tmp_path, stack_type=LocalWorkflowStack) as stack,
        bound_worker_launch_config(stack.context.config),
    ):
        assert isinstance(stack, LocalWorkflowStack)
        stack.scripts.gateway_url = f"http://127.0.0.1:{port}/api/script-gateway"
        async with serve_listener(
            stack.app(),
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
            await stack.engine.close()


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
