"""Tests for workflow runs across a restart of MindRoom: inherited attempts, replay, the run's job, automatic resume.

Level: engine harness (`WorkflowStack`): the real stores, broker, engine and job runtime; the test plays the script.
A killed primary is the harness's `restart(kill=True)`: its tasks stop where they are and only saved state survives.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sqlite3
import threading
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio

from mindroom import authorization
from mindroom.orchestration import tool_job_runtime as coordinator_module
from mindroom.orchestration.script_runtime import _LiveScriptRuntimeResolver
from mindroom.orchestration.workflow_runtime import WorkflowRuntimeLifecycle
from mindroom.script_runs import manager as manager_module
from mindroom.script_runs.broker import ScriptToolBroker
from mindroom.script_runs.models import ScriptCallState, ScriptRunState
from mindroom.script_runs.reasons import (
    MAX_RUNTIME_EXCEEDED,
    PLUGIN_TOOLS_CHANGED,
    RUNTIME_RESTARTED,
    RUNTIME_SHUTDOWN,
    WORKFLOW_TOOL_REMOVED,
)
from mindroom.workflow_runs.engine import SupersededAttemptError
from mindroom.workflow_runs.models import NodeRequest, WorkflowNodeState, WorkflowRunState
from mindroom.workflow_runs.recovery import AUTO_RESUME_REASONS
from mindroom.workflow_runs.store import WorkflowRunNotFoundError
from tests.conftest import SETTLE_TIMEOUT_SECONDS
from tests.tool_job_helpers import wait_for_status
from tests.workflow_helpers import NodeFailed, WorkflowStack, tool_args, workflow_stack
from tests.workflow_live import kill_after_write

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mindroom.script_runs.models import ScriptCallRecord, ScriptRunRecord
    from mindroom.tool_jobs.runtime import BackgroundJob

_A = tool_args("counter", "append", line="a")
_B = tool_args("counter", "append", line="b")
SCRIPT = "def main(args):\n    return 'counted'\n"


@pytest.fixture(autouse=True)
def _scripts_can_run_here(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let the placement check pass on every platform; the stack starts no process."""
    monkeypatch.setattr(manager_module, "background_script_supervision_supported", lambda: True)


@pytest_asyncio.fixture
async def stack(tmp_path: Path) -> AsyncIterator[WorkflowStack]:
    """The harness with the real job runtime, which keeps the jobs the coordinator continues after a restart."""
    with workflow_stack(tmp_path, extra_tools=("workflow",)) as stack:
        stack.continues = coordinator_module._continues
        await stack.open_jobs()
        yield stack
        await stack.close()


@pytest.fixture
def barrier(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Hold every fault barrier, as a crash test does; the directory shows which ones a call reached."""
    faults = tmp_path / "faults"
    faults.mkdir()
    monkeypatch.setenv("MINDROOM_WORKFLOW_FAULT_DIR", str(faults))
    return faults


async def _started(stack: WorkflowStack, **arguments: object) -> dict[str, str]:
    """Start a run as its job and return the handle, once its first attempt runs."""
    handle = json.loads(await stack.call("start_workflow", source=SCRIPT, **arguments))
    stack.run_id = handle["run_id"]
    await stack.until(lambda: stack.store.get_run(stack.run_id).state is WorkflowRunState.RUNNING)
    return handle


async def _ended_job(stack: WorkflowStack, job_id: str) -> BackgroundJob:
    owner = stack.context.execution_identity
    async with asyncio.timeout(SETTLE_TIMEOUT_SECONDS):
        return (await stack.jobs.wait(job_id, owner=owner, depth=0)).job


async def _killed_at(stack: WorkflowStack, barrier: Path, name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Submit the write, kill the primary once the call stands at the barrier *name*, and start a new primary."""
    if name == "before_settle":
        (barrier / "before_receipt.release").touch()
    await stack.submit("@0", "tool", _A)
    await stack.until((barrier / f"{name}.reached").exists)
    assert stack.counting_tool.lines == ["a"]
    await stack.restart(kill=True)
    monkeypatch.delenv("MINDROOM_WORKFLOW_FAULT_DIR")


async def _delivery(stack: WorkflowStack, job_id: str) -> tuple[list[str], str, object]:
    """Return what reaches the conversation of an ended job nobody collected: the follow-ups, its status, its payload."""
    follow_ups = await stack.follow_ups()
    [job] = [job for job in stack.jobs.retained_jobs() if job.job_id == job_id]
    return follow_ups, job.status, await stack.collect(job)


@pytest.mark.asyncio
async def test_crash_after_the_write_and_its_receipt_copies_the_result_and_writes_once(
    stack: WorkflowStack,
    barrier: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The acceptance test, receipt saved: one write, the result read back in attempt 2, one successful report.

    The node was never settled, and the replay reads the receipt instead. The report is delivered once, also when
    the primary is killed again after the delivery and before the conversation acknowledged it.
    """
    handle = await _started(stack)
    await _killed_at(stack, barrier, "before_settle", monkeypatch)
    node = stack.store.get_node(stack.run_id, "@0")
    assert node is not None
    assert node.state is WorkflowNodeState.RUNNING

    await stack.next_primary()
    receipt = await stack.submit("@0", "tool", _A)

    assert (receipt.state, receipt.result, receipt.cached) == ("completed", "appended", True)
    await stack.finish_attempt(result="report")
    await wait_for_status(stack.jobs, handle["job_id"], "completed")
    run = stack.store.get_run(stack.run_id)
    assert (run.state, run.attempt, run.result, run.job_id) == (
        WorkflowRunState.COMPLETED,
        2,
        "report",
        handle["job_id"],
    )
    [follow_up], status, payload = await _delivery(stack, handle["job_id"])
    assert (status, payload) == ("completed", "report")

    # Delivered and not acknowledged yet: a second kill here delivers the same follow-up, and starts nothing.
    await stack.restart(kill=True)
    assert await stack.recover() == []
    await stack.continue_jobs()
    assert await _delivery(stack, handle["job_id"]) == ([follow_up], "completed", "report")
    claimed = await stack.jobs.wait(handle["job_id"], owner=stack.context.execution_identity, depth=0)
    await stack.jobs.acknowledge_wait(handle["job_id"], claimed.claim)
    assert await stack.follow_ups() == []

    assert [job.job_id for job in stack.jobs.retained_jobs()] == [handle["job_id"]]
    assert (stack.store.get_run(stack.run_id).attempt, len(stack.script_store.list_runs())) == (2, 2)
    assert stack.counting_tool.lines == ["a"]


@pytest.mark.asyncio
async def test_crash_after_the_write_without_a_receipt_is_indeterminate_and_writes_once(
    stack: WorkflowStack,
    barrier: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The acceptance test, no receipt: one write, one delivery of the failure in attempt 2, no successful report.

    The write happened and nothing says so, so the call is never issued again without `rerun`.
    """
    handle = await _started(stack)
    await _killed_at(stack, barrier, "before_receipt", monkeypatch)
    assert not (barrier / "before_settle.reached").exists()

    await stack.next_primary()
    receipt = await stack.submit("@0", "tool", _A)

    assert receipt.error is not None
    assert (receipt.state, receipt.error["kind"], stack.counting_tool.lines) == ("failed", "indeterminate", ["a"])
    failure = f"ToolError: {receipt.error['message']}"
    await stack.finish_attempt(error=failure)
    await wait_for_status(stack.jobs, handle["job_id"], "failed")
    run = stack.store.get_run(stack.run_id)
    node = stack.store.get_node(stack.run_id, "@0")
    assert node is not None
    assert (run.state, run.attempt, run.result, node.state) == (
        WorkflowRunState.FAILED,
        2,
        None,
        WorkflowNodeState.INDETERMINATE,
    )
    [follow_up], status, payload = await _delivery(stack, handle["job_id"])
    assert (status, payload) == ("failed", failure)
    assert await stack.follow_ups() == [follow_up]
    assert [job.status for job in stack.jobs.retained_jobs()] == ["failed"]
    assert (len(stack.script_store.list_runs()), stack.counting_tool.lines) == (2, ["a"])


@pytest.mark.asyncio
async def test_kill_after_the_node_was_settled_and_before_the_script_reported_reuses_it_and_reports_once(
    stack: WorkflowStack,
) -> None:
    """The node holds its result and the run has no report yet: attempt 2 reads the node back and reports once."""
    handle = await _started(stack)
    assert await stack.result("@0", "tool", _A) == "appended"
    settled = stack.store.get_node(stack.run_id, "@0")
    assert settled is not None

    await stack.restart(kill=True)
    await stack.next_primary()

    receipt = await stack.submit("@0", "tool", _A)
    node = stack.store.get_node(stack.run_id, "@0")
    assert node is not None
    assert (settled.state, receipt.cached, node.generation, node.attempt) == (WorkflowNodeState.COMPLETED, True, 1, 2)
    await stack.finish_attempt(result="report")
    await wait_for_status(stack.jobs, handle["job_id"], "completed")
    [_], status, payload = await _delivery(stack, handle["job_id"])
    assert (status, payload, stack.store.get_run(stack.run_id).attempt) == ("completed", "report", 2)
    assert stack.counting_tool.lines == ["a"]


@pytest.mark.asyncio
async def test_inherited_attempt_is_refused_before_anything_recovers(stack: WorkflowStack) -> None:
    """A new primary serves no attempt it did not start: a leftover process gets 409 on every route and adds nothing."""
    await _started(stack)
    old, token = stack.attempt, stack.token

    await stack.restart(kill=True)

    with pytest.raises(SupersededAttemptError):
        await stack.engine.submit_node(old, NodeRequest("@0", "tool", _A, {}))
    async with stack.http(token) as client:
        answers = [
            await client.get("run"),
            await client.post("nodes", json={"key": "@0", "kind": "tool", "args": _A}),
            await client.post("log", json={"text": "still here"}),
            await client.post("finish", json={"result": "late"}),
        ]
    assert [answer.status_code for answer in answers] == [409, 409, 409, 409]
    run, nodes = stack.engine.status(stack.run_id)
    assert (nodes, stack.store.log(stack.run_id), run.finished_at, stack.counting_tool.calls) == ([], [], None, 0)

    await stack.recover()
    assert (
        stack.script_store.get_run(old.run_id).state,
        stack.script_store.get_run(old.run_id).cancellation_reason,
    ) == (
        ScriptRunState.INTERRUPTED,
        RUNTIME_RESTARTED,
    )
    # Its capability is revoked now, and a process that is still there is still told to exit, not that it is unknown.
    async with stack.http(token) as client:
        late = [await client.get("run"), await client.post("nodes", json={"key": "@0", "kind": "tool", "args": _A})]
    async with stack.http("another-capability") as client:
        unknown = await client.get("run")
    assert ([answer.status_code for answer in late], unknown.status_code) == ([409, 409], 404)
    assert late[0].json() == {"detail": "superseded"}


@pytest.mark.asyncio
async def test_restart_keeps_the_job_running_and_delivers_one_report(stack: WorkflowStack) -> None:
    """An orderly restart: the run gets its next attempt, reuses what finished, and its one job delivers the report."""
    handle = await _started(stack)
    assert await stack.result("@0", "tool", _A) == "appended"
    first = stack.attempt.run_id

    await stack.restart()
    saved = stack.jobs.awaiting_continuation()
    await stack.next_primary()

    assert [(job.job_id, job.status) for job in saved] == [(handle["job_id"], "running")]
    assert stack.script_store.get_run(first).cancellation_reason == RUNTIME_SHUTDOWN
    run = stack.store.get_run(stack.run_id)
    assert (run.state, run.attempt, run.idle_resumes) == (WorkflowRunState.RUNNING, 2, 1)
    assert (await stack.submit("@0", "tool", _A)).cached
    assert await stack.result("@1", "tool", _B) == "appended"
    assert stack.store.get_run(stack.run_id).idle_resumes == 0
    await stack.finish_attempt(result="the report")
    job = await _ended_job(stack, handle["job_id"])
    assert (job.status, await stack.collect(job), stack.counting_tool.lines) == ("completed", "the report", ["a", "b"])
    assert [job.job_id for job in stack.jobs.retained_jobs()] == [handle["job_id"]]


@pytest.mark.asyncio
async def test_restart_after_finish_was_stored_delivers_it_without_a_new_attempt(stack: WorkflowStack) -> None:
    """The script had reported: the run ends with that report, and its job delivers it once."""
    handle = await _started(stack)
    await stack.engine.finish(stack.attempt, result="reported before the kill", error=None)

    await stack.restart(kill=True)
    assert await stack.recover() == []
    await stack.continue_jobs()

    job = await _ended_job(stack, handle["job_id"])
    run = stack.store.get_run(stack.run_id)
    assert (run.state, run.attempt, len(stack.script_store.list_runs())) == (WorkflowRunState.COMPLETED, 1, 1)
    assert (job.status, await stack.collect(job)) == ("completed", "reported before the kill")


@pytest.mark.asyncio
async def test_crash_between_admission_and_launch_launches_once(
    stack: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The job was admitted and no attempt started: the intent stays, and the job's continuation launches it once."""
    monkeypatch.setattr(stack.engine, "launch", lambda *_args, **_options: asyncio.Event().wait())
    handle = json.loads(await stack.call("start_workflow", source=SCRIPT))
    stack.run_id = handle["run_id"]
    await stack.until(lambda: stack.store.get_run(stack.run_id).job_id == handle["job_id"])

    await stack.restart(kill=True)
    assert await stack.recover() == []
    assert stack.store.get_run(stack.run_id).state is WorkflowRunState.STARTING
    await stack.continue_jobs()
    await stack.until(lambda: stack.store.get_run(stack.run_id).state is WorkflowRunState.RUNNING)

    assert (stack.store.get_run(stack.run_id).attempt, len(stack.script_store.list_runs())) == (1, 1)
    await stack.finish_attempt(result="the report")
    assert await stack.collect(await _ended_job(stack, handle["job_id"])) == "the report"


@pytest.mark.asyncio
async def test_first_attempt_that_a_continued_job_cannot_start_fails_the_run_with_the_reason(
    stack: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A first attempt is the start the owner asked for, so its refusal is told, as before the restart."""
    monkeypatch.setattr(stack.engine, "launch", lambda *_args, **_options: asyncio.Event().wait())
    handle = json.loads(await stack.call("start_workflow", source=SCRIPT))
    stack.run_id = handle["run_id"]
    await stack.restart(kill=True)
    stack.scripts.refuse_launch = RuntimeError("No worker is free.")

    await stack.next_primary()

    job = await _ended_job(stack, handle["job_id"])
    run = stack.store.get_run(stack.run_id)
    assert (run.state, run.error, job.status) == (WorkflowRunState.FAILED, "No worker is free.", "failed")


@pytest.mark.asyncio
async def test_crash_between_launch_and_naming_the_attempt_retires_it_and_launches_once(stack: WorkflowStack) -> None:
    """A process the run never named is retired like any inherited attempt, and the run gets one that it names."""
    stack.scripts.hold_launch = asyncio.Event()
    handle = json.loads(await stack.call("start_workflow", source=SCRIPT))
    stack.run_id = handle["run_id"]
    await stack.until(lambda: len(stack.scripts.launched) == 1)
    unnamed = stack.scripts.launched[0].run_id

    await stack.restart(kill=True)
    assert await stack.recover() == []
    await stack.continue_jobs()
    await stack.until(lambda: stack.store.get_run(stack.run_id).state is WorkflowRunState.RUNNING)

    run = stack.store.get_run(stack.run_id)
    assert stack.script_store.get_run(unnamed).state is ScriptRunState.INTERRUPTED
    assert (run.attempt, run.script_run_id != unnamed, len(stack.script_store.list_runs())) == (1, True, 2)


@pytest.mark.asyncio
async def test_intent_without_a_job_is_deleted(stack: WorkflowStack) -> None:
    """An intent a dead primary left counts toward the run limit for nobody: it goes, and a fresh one stays."""
    never = "job-the-runtime-never-admitted"
    left = stack.reserve(SCRIPT, origin_job_id=never, job_id=never)

    await stack.restart(kill=True)
    fresh = stack.reserve(SCRIPT, origin_job_id="job-being-admitted-now", job_id="job-being-admitted-now")
    assert await stack.recover() == []

    with pytest.raises(WorkflowRunNotFoundError):
        stack.store.get_run(left.run_id)
    assert stack.store.get_run(fresh.run_id).state is WorkflowRunState.STARTING


@pytest.mark.asyncio
async def test_run_with_no_job_is_replayed_under_its_owner_and_keeps_its_report(stack: WorkflowStack) -> None:
    """A run that a call started and waited for itself goes on too; with no caller left, its report can be read."""
    await stack.start(SCRIPT)
    await stack.result("@0", "tool", _A)

    await stack.restart(kill=True)
    assert await stack.recover() == [stack.run_id]

    assert (await stack.submit("@0", "tool", _A)).cached
    await stack.finish_attempt(result="the report")
    status = json.loads(await stack.call("get_workflow", run_id=stack.run_id))
    assert (status["status"], status["attempt"], status["result"]) == ("completed", 2, "the report")
    assert stack.counting_tool.lines == ["a"]


@pytest.mark.asyncio
async def test_run_with_no_job_that_no_longer_has_access_ends_interrupted(stack: WorkflowStack) -> None:
    """The saved owner is checked as a job's requester is."""
    await stack.start(SCRIPT)
    await stack.restart(kill=True)
    stack.access = "revoked"

    assert await stack.recover() == []

    run = stack.store.get_run(stack.run_id)
    assert (run.state, run.error, run.attempt) == (
        WorkflowRunState.INTERRUPTED,
        "The requester no longer has access.",
        1,
    )


@pytest.mark.asyncio
async def test_run_with_no_job_that_never_got_an_attempt_is_launched_once(stack: WorkflowStack) -> None:
    """A dead primary's reservation is an admitted run; one this primary just made is its own call's to launch."""
    left = stack.reserve(SCRIPT)

    await stack.restart(kill=True)
    fresh = stack.reserve(SCRIPT)
    assert await stack.recover() == [left.run_id]
    assert await stack.maintain() == []

    assert [stack.store.get_run(run.run_id).attempt for run in (left, fresh)] == [1, 0]
    assert len(stack.scripts.launched) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("managed", [True, False])
async def test_graceful_stop_while_the_first_attempt_starts_leaves_the_run_to_the_next_primary(
    stack: WorkflowStack,
    managed: bool,
) -> None:
    """A shutdown in the middle of a launch ends no admitted run: the next primary starts it, once."""
    stack.scripts.hold_launch = asyncio.Event()
    if managed:
        handle = json.loads(await stack.call("start_workflow", source=SCRIPT))
        stack.run_id = handle["run_id"]
    else:
        launching = asyncio.create_task(stack.start(SCRIPT))
    await stack.until(lambda: len(stack.scripts.launched) == 1)

    await stack.restart()
    stopped = stack.store.get_run(stack.run_id)
    resumed = await stack.recover()
    if managed:
        await stack.continue_jobs()
    else:
        await launching
    await stack.until(lambda: stack.store.get_run(stack.run_id).state is WorkflowRunState.RUNNING)

    assert (stopped.state, stopped.error, stopped.finished_at) == (WorkflowRunState.STARTING, None, None)
    assert resumed == ([] if managed else [stack.run_id])
    assert (stack.store.get_run(stack.run_id).attempt, len(stack.scripts.launched)) == (1, 1)
    await stack.finish_attempt(result="the report")
    if managed:
        assert await stack.collect(await _ended_job(stack, handle["job_id"])) == "the report"


async def _interrupt(stack: WorkflowStack, reason: str) -> None:
    """End the attempt as the script runtime does when it stops a process for *reason*, with MindRoom still running."""
    await stack.scripts.end(stack.attempt.run_id, ScriptRunState.INTERRUPTED, reason=reason)


def test_automatic_resume_follows_infrastructure_interruptions_only() -> None:
    """A removed tool, a removed agent and revoked access end a run; what MindRoom itself interrupted goes on."""
    assert PLUGIN_TOOLS_CHANGED in AUTO_RESUME_REASONS
    assert {RUNTIME_RESTARTED, RUNTIME_SHUTDOWN, MAX_RUNTIME_EXCEEDED} <= AUTO_RESUME_REASONS
    assert WORKFLOW_TOOL_REMOVED not in AUTO_RESUME_REASONS


@pytest.mark.asyncio
async def test_attempt_interrupted_while_mindroom_runs_is_resumed_by_the_next_pass(stack: WorkflowStack) -> None:
    """A plugin reload stops the process, not the run: the pass starts the next attempt, and the same job goes on."""
    handle = await _started(stack)
    await stack.result("@0", "tool", _A)
    await _interrupt(stack, PLUGIN_TOOLS_CHANGED)

    assert await stack.maintain() == [stack.run_id]
    await stack.result("@0", "tool", _B)
    await _interrupt(stack, PLUGIN_TOOLS_CHANGED)
    assert await stack.maintain() == [stack.run_id]

    # What ran again in the second attempt dirties nothing in the third.
    assert (await stack.submit("@0", "tool", _B)).cached
    await stack.finish_attempt(result="the report")
    job = await _ended_job(stack, handle["job_id"])
    assert (job.status, await stack.collect(job), stack.store.get_run(stack.run_id).attempt) == (
        "completed",
        "the report",
        3,
    )


@pytest.mark.asyncio
async def test_removed_tool_ends_the_run_interrupted(stack: WorkflowStack) -> None:
    """An interruption that is no infrastructure fault is final, as before."""
    handle = await _started(stack)
    await _interrupt(stack, WORKFLOW_TOOL_REMOVED)

    assert await stack.maintain() == []

    run = stack.store.get_run(stack.run_id)
    assert (run.state, run.error) == (WorkflowRunState.INTERRUPTED, WORKFLOW_TOOL_REMOVED)
    assert (await _ended_job(stack, handle["job_id"])).status == "interrupted"


@pytest.mark.asyncio
async def test_restart_before_matrix_is_ready_waits_and_resumes_later(stack: WorkflowStack) -> None:
    """Access that cannot be checked yet is tried again at the next pass, and counts as no attempt."""
    await _started(stack)
    await _interrupt(stack, RUNTIME_RESTARTED)
    stack.access = "pending"

    assert await stack.maintain() == []
    assert await stack.maintain() == []
    waiting = stack.store.get_run(stack.run_id)
    stack.access = None
    assert await stack.maintain() == [stack.run_id]

    run = stack.store.get_run(stack.run_id)
    assert (waiting.state, waiting.attempt, waiting.idle_resumes) == (WorkflowRunState.RUNNING, 1, 0)
    assert (run.state, run.attempt, len(stack.scripts.launched)) == (WorkflowRunState.RUNNING, 2, 2)


@pytest.mark.asyncio
async def test_run_of_a_requester_without_access_ends_interrupted(stack: WorkflowStack) -> None:
    """Revoked access starts nothing and says why."""
    await _started(stack)
    await _interrupt(stack, RUNTIME_RESTARTED)
    stack.access = "revoked"

    assert await stack.maintain() == []

    run = stack.store.get_run(stack.run_id)
    assert (run.state, run.error) == (WorkflowRunState.INTERRUPTED, "The requester no longer has access.")
    assert len(stack.scripts.launched) == 1


@pytest.mark.asyncio
async def test_cancelled_run_is_not_resumed(stack: WorkflowStack) -> None:
    """A cancel is the owner's word: no restart starts the run again."""
    await _started(stack)
    await stack.engine.cancel(stack.run_id, reason="Cancelled by the agent.")

    await stack.restart()
    assert await stack.recover() == []

    assert stack.store.get_run(stack.run_id).state is WorkflowRunState.CANCELLED
    assert stack.scripts.launched == []


@pytest.mark.asyncio
async def test_automatic_resume_stops_after_five_idle_attempts(stack: WorkflowStack) -> None:
    """A script that takes its attempt down every time is not started for ever; a finished call starts the count anew."""
    await _started(stack)
    for _ in range(2):
        await _interrupt(stack, RUNTIME_RESTARTED)
        assert await stack.maintain() == [stack.run_id]
    await stack.result("@0", "tool", _A)
    for _ in range(5):
        await _interrupt(stack, RUNTIME_RESTARTED)
        assert await stack.maintain() == [stack.run_id]
    await _interrupt(stack, RUNTIME_RESTARTED)

    assert await stack.maintain() == []

    run = stack.store.get_run(stack.run_id)
    assert (run.state, run.attempt) == (WorkflowRunState.INTERRUPTED, 8)
    assert (
        run.error == "Stopped resuming after 5 attempts that finished nothing. Fix the script and call resume_workflow."
    )


@pytest.mark.asyncio
async def test_refused_launch_of_an_automatic_resume_is_tried_again(stack: WorkflowStack) -> None:
    """A worker that is not there yet fails no run: the next pass tries again."""
    await _started(stack)
    await _interrupt(stack, RUNTIME_RESTARTED)
    stack.scripts.refuse_launch = RuntimeError("Background script runtime reconciliation is in progress.")

    assert await stack.maintain() == []
    refused = stack.store.get_run(stack.run_id)
    stack.scripts.refuse_launch = None
    assert await stack.maintain() == [stack.run_id]

    assert (refused.state, refused.error) == (WorkflowRunState.STARTING, None)
    assert stack.store.get_run(stack.run_id).attempt == 2


@pytest.mark.asyncio
async def test_refused_launches_count_as_no_idle_attempt(stack: WorkflowStack) -> None:
    """A worker that is away for six passes uses up none of the five attempts: no attempt started."""
    await _started(stack)
    await _interrupt(stack, RUNTIME_RESTARTED)
    stack.scripts.refuse_launch = RuntimeError("Background script runtime reconciliation is in progress.")

    for _ in range(6):
        assert await stack.maintain() == []
    refused = stack.store.get_run(stack.run_id)
    stack.scripts.refuse_launch = None

    assert (refused.state, refused.idle_resumes) == (WorkflowRunState.STARTING, 0)
    assert await stack.maintain() == [stack.run_id]
    assert stack.store.get_run(stack.run_id).idle_resumes == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["value", "group", "receipt"])
async def test_durable_progress_of_any_kind_starts_the_idle_count_again(
    stack: WorkflowStack,
    kind: str,
    barrier: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A value, a group, and a receipt that is copied into its node after the restart are each a finished call."""
    await _started(stack)
    await _interrupt(stack, RUNTIME_RESTARTED)
    assert await stack.maintain() == [stack.run_id]
    assert stack.store.get_run(stack.run_id).idle_resumes == 1

    if kind == "receipt":
        await _killed_at(stack, barrier, "before_settle", monkeypatch)
        await stack.next_primary()
        # The new attempt is the first one since the receipt was copied into its node.
        assert stack.store.get_run(stack.run_id).idle_resumes == 1
        return
    monkeypatch.delenv("MINDROOM_WORKFLOW_FAULT_DIR")
    request = ("value", {"op": "now"}, {}) if kind == "value" else ("group", {}, {"members": ["/0:0"]})
    await stack.submit("@0", *request)
    assert stack.store.get_run(stack.run_id).idle_resumes == 0
    # A call that is only read back is no progress.
    await _interrupt(stack, RUNTIME_RESTARTED)
    assert await stack.maintain() == [stack.run_id]
    assert (await stack.submit("@0", *request)).cached
    assert stack.store.get_run(stack.run_id).idle_resumes == 1


@pytest.mark.asyncio
async def test_automatic_resume_keeps_the_used_time(stack: WorkflowStack) -> None:
    """Only a person's resume gives the full working time again."""
    await _started(stack)
    full = stack.store.get_run(stack.run_id).runtime_seconds_left
    stack.now += 600
    await _interrupt(stack, RUNTIME_RESTARTED)

    assert await stack.maintain() == [stack.run_id]

    assert (full, stack.store.get_run(stack.run_id).runtime_seconds_left) == (72 * 3600, 72 * 3600 - 600)


@pytest.mark.asyncio
@pytest.mark.parametrize(("left", "resumed"), [(None, True), (120.0, True), (0.0, False)])
async def test_attempt_at_its_process_limit_is_resumed_only_while_working_time_is_left(
    stack: WorkflowStack,
    left: float | None,
    resumed: bool,
) -> None:
    """The attempt's own 30-day limit ends a process, and the run goes on while it has time or no limit."""
    await _started(stack)
    stack.store.update_run(stack.run_id, runtime_seconds_left=left)
    await _interrupt(stack, MAX_RUNTIME_EXCEEDED)

    assert await stack.maintain() == ([stack.run_id] if resumed else [])

    run = stack.store.get_run(stack.run_id)
    assert (run.state, run.runtime_seconds_left) == (
        WorkflowRunState.RUNNING if resumed else WorkflowRunState.INTERRUPTED,
        left,
    )
    # A run with no time left gets no process that would be stopped at once.
    assert (run.attempt, run.error) == ((2, None) if resumed else (1, MAX_RUNTIME_EXCEEDED))


@pytest.mark.asyncio
async def test_look_at_an_attempt_that_was_replaced_meanwhile_leaves_the_new_one(
    stack: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The engine's own look at an ended attempt, finished after a pass resumed the run, ends nothing."""
    await _started(stack)
    await _interrupt(stack, PLUGIN_TOOLS_CHANGED)
    reading, resumed = threading.Event(), threading.Event()
    get_run = stack.script_store.get_run

    def slow_first_read(run_id: str) -> ScriptRunRecord:
        if not reading.is_set():
            reading.set()
            resumed.wait(SETTLE_TIMEOUT_SECONDS)
        return get_run(run_id)

    monkeypatch.setattr(stack.script_store, "get_run", slow_first_read)
    look = asyncio.create_task(stack.engine.reconcile())
    await stack.until(reading.is_set)

    assert await stack.maintain() == [stack.run_id]
    resumed.set()
    await look

    run = stack.store.get_run(stack.run_id)
    assert (run.state, run.attempt, run.error) == (WorkflowRunState.RUNNING, 2, None)


@pytest.mark.asyncio
async def test_rerun_names_a_call_for_one_attempt_only(stack: WorkflowStack) -> None:
    """The call a person asked to run again ran in the resumed attempt; the automatic attempt after it reads it back."""
    await _started(stack)
    await stack.result("@0", "tool", _A)
    node = stack.store.get_node(stack.run_id, "@0")
    assert node is not None
    await _resumed_by_a_person(stack, rerun=[f"n{node.seq}"])
    await stack.result("@0", "tool", _A)
    await _interrupt(stack, PLUGIN_TOOLS_CHANGED)

    assert await stack.maintain() == [stack.run_id]

    assert (await stack.submit("@0", "tool", _A)).cached
    assert stack.counting_tool.lines == ["a", "a"]


async def _resumed_by_a_person(stack: WorkflowStack, **arguments: object) -> None:
    """End the attempt with a report and, once its job delivered it, resume the run as its owner's call does."""
    await stack.finish_attempt(result="the report")
    await _ended_job(stack, str(stack.store.get_run(stack.run_id).job_id))
    attempt = stack.store.get_run(stack.run_id).attempt
    await stack.call("resume_workflow", run_id=stack.run_id, **arguments)
    await stack.until(lambda: stack.store.get_run(stack.run_id).attempt == attempt + 1)


async def _interrupted_and_resumed(stack: WorkflowStack) -> None:
    """Lose the attempt to a restart of its process and let the pass start the next one, which remembers nothing."""
    await _interrupt(stack, PLUGIN_TOOLS_CHANGED)
    assert await stack.maintain() == [stack.run_id]


@pytest.mark.asyncio
async def test_rerun_of_an_ambiguous_call_survives_an_interruption_before_the_call(
    stack: WorkflowStack,
    barrier: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The resume that named the call was accepted, so a later attempt runs the call too, once."""
    await _started(stack)
    await _killed_at(stack, barrier, "before_receipt", monkeypatch)
    await stack.next_primary()
    with pytest.raises(NodeFailed):
        await stack.result("@0", "tool", _A)
    node = stack.store.get_node(stack.run_id, "@0")
    assert node is not None
    await _resumed_by_a_person(stack, rerun=[f"n{node.seq}"])

    await _interrupted_and_resumed(stack)

    assert await stack.result("@0", "tool", _A) == "appended"
    await _interrupted_and_resumed(stack)
    assert (await stack.submit("@0", "tool", _A)).cached
    assert stack.counting_tool.lines == ["a", "a"]


@pytest.mark.asyncio
async def test_call_after_a_rerun_runs_again_after_an_interruption_between_them(stack: WorkflowStack) -> None:
    """The call that follows a rerun may see something else, also when the attempt that reran died before it."""
    await _started(stack)
    await stack.result("@0", "tool", _A)
    await stack.result("@1", "tool", _B)
    node = stack.store.get_node(stack.run_id, "@0")
    assert node is not None
    await _resumed_by_a_person(stack, rerun=[f"n{node.seq}"])
    await stack.result("@0", "tool", _A)

    await _interrupted_and_resumed(stack)

    assert (await stack.submit("@0", "tool", _A)).cached
    assert await stack.result("@1", "tool", _B) == "appended"
    assert stack.counting_tool.lines == ["a", "b", "a", "b"]


@pytest.mark.asyncio
async def test_call_after_a_changed_result_runs_again_when_the_attempt_died_before_it_compared(
    stack: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The result a `cached=False` call had before is saved with its dispatch, so the receipt alone tells it changed."""
    answer, every_time = tool_args("counter", "answer"), {"cached": False}
    stack.counting_tool.answers = ["old", "new", "new"]
    await _started(stack)
    await stack.result("@0", "tool", answer, every_time)
    await stack.result("@1", "tool", _A)
    await _interrupted_and_resumed(stack)
    execute, received = ScriptToolBroker.execute_for_workflow, asyncio.Event()

    async def stopped_after_the_receipt(*args: Any, **options: Any) -> ScriptCallRecord:  # noqa: ANN401
        await execute(*args, **options)
        received.set()
        return await asyncio.Event().wait()  # type: ignore[return-value]

    monkeypatch.setattr(ScriptToolBroker, "execute_for_workflow", stopped_after_the_receipt)
    await stack.submit("@0", "tool", answer, every_time)
    await received.wait()
    monkeypatch.setattr(ScriptToolBroker, "execute_for_workflow", execute)

    await _interrupted_and_resumed(stack)

    assert stack.store.get_node(stack.run_id, "@0").result == "new"  # type: ignore[union-attr]
    assert await stack.result("@0", "tool", answer, every_time) == "new"
    assert await stack.result("@1", "tool", _A) == "appended"
    assert stack.counting_tool.lines == ["a", "a"]


@pytest.mark.asyncio
async def test_call_after_a_changed_fan_out_runs_again_after_an_interruption_between_them(
    stack: WorkflowStack,
) -> None:
    """A fan-out whose items changed dirties what follows its join, and the next attempt still knows."""
    fan_out: dict[str, object] = {"op": "foreach"}
    await _started(stack)
    await stack.submit("@0", "group", fan_out, {"members": ["/0:0"]})
    await stack.result("@1", "tool", _A)
    await _resumed_by_a_person(stack)
    await stack.submit("@0", "group", fan_out, {"members": ["/0:0", "/0:1"]})

    await _interrupted_and_resumed(stack)

    assert (await stack.submit("@0", "group", fan_out, {"members": ["/0:0", "/0:1"]})).cached
    assert await stack.result("@1", "tool", _A) == "appended"
    assert stack.counting_tool.lines == ["a", "a"]


@pytest.mark.asyncio
async def test_call_that_may_have_run_is_not_run_because_a_call_before_it_ran_again(
    stack: WorkflowStack,
    barrier: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only a resume that names it runs an ambiguous call again; a rerun before it does not, in any later attempt."""
    monkeypatch.delenv("MINDROOM_WORKFLOW_FAULT_DIR")
    await _started(stack)
    await stack.result("@0", "tool", _B)
    monkeypatch.setenv("MINDROOM_WORKFLOW_FAULT_DIR", str(barrier))
    await stack.submit("@1", "tool", _A)
    await stack.until((barrier / "before_receipt.reached").exists)
    await stack.restart(kill=True)
    monkeypatch.delenv("MINDROOM_WORKFLOW_FAULT_DIR")
    await stack.next_primary()
    first = stack.store.get_node(stack.run_id, "@0")
    assert first is not None
    await _resumed_by_a_person(stack, rerun=[f"n{first.seq}"])
    await stack.result("@0", "tool", _B)

    await _interrupted_and_resumed(stack)

    with pytest.raises(NodeFailed) as error:
        await stack.result("@1", "tool", _A)
    assert (error.value.kind, stack.counting_tool.lines) == ("indeterminate", ["b", "a", "b"])


@pytest.mark.asyncio
async def test_run_whose_job_ended_is_not_resumed(stack: WorkflowStack) -> None:
    """A job the coordinator ended instead of continuing receives nothing, so its run gets no further attempt."""
    handle = await _started(stack)
    await stack.restart(kill=True)
    await stack.jobs.continue_stopped(handle["job_id"], None)

    assert await stack.recover() == []

    run = stack.store.get_run(stack.run_id)
    assert (run.state, run.error, run.attempt) == (WorkflowRunState.INTERRUPTED, RUNTIME_RESTARTED, 1)


@pytest.mark.asyncio
async def test_a_continued_job_that_is_cancelled_cancels_the_run(stack: WorkflowStack) -> None:
    """The job a restart continued still stops its run."""
    handle = await _started(stack)
    await stack.restart()
    await stack.next_primary()

    await stack.jobs.cancel(handle["job_id"], owner=stack.context.execution_identity, depth=0)
    await wait_for_status(stack.jobs, handle["job_id"], "cancelled")

    assert stack.store.get_run(stack.run_id).state is WorkflowRunState.CANCELLED
    assert stack.attempt.state is ScriptRunState.CANCELLED


@pytest.mark.asyncio
async def test_a_job_whose_run_is_gone_ends_instead_of_continuing(stack: WorkflowStack) -> None:
    """Nothing is left to wait for, so the job ends interrupted."""
    handle = await _started(stack)
    await stack.restart()
    assert stack.store.prune("9999-01-01T00:00:00Z") == 0
    await stack.engine.cancel(stack.run_id, reason="Cancelled by the agent.")
    assert stack.store.prune("9999-01-01T00:00:00Z") == 1

    await stack.continue_jobs()

    assert (await _ended_job(stack, handle["job_id"])).status == "interrupted"


@pytest.mark.asyncio
async def test_indeterminate_call_of_a_killed_primary_fails_the_script_that_does_not_catch_it(
    stack: WorkflowStack,
    barrier: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The broker's receipt of the cut call is closed as indeterminate, and the call's node says how to run it again."""
    await _started(stack)
    await _killed_at(stack, barrier, "before_receipt", monkeypatch)
    await stack.next_primary()

    with pytest.raises(NodeFailed) as error:
        await stack.result("@0", "tool", _A)

    node = stack.store.get_node(stack.run_id, "@0")
    assert node is not None
    assert node.handle is not None
    script_run_id, _, call_id = node.handle.ref.partition(":")
    assert stack.script_store.get_call(script_run_id, call_id).state is ScriptCallState.INDETERMINATE
    assert (node.state, error.value.kind) == (WorkflowNodeState.INDETERMINATE, "indeterminate")
    assert f'rerun=["n{node.seq}"]' in str(error.value)


@pytest.mark.asyncio
async def test_pass_leaves_the_next_attempt_to_a_job_that_is_not_continued_yet(stack: WorkflowStack) -> None:
    """Until the job runtime granted the job again and can cancel the run, nothing starts; then the job starts it."""
    handle = await _started(stack)
    await stack.restart(kill=True)

    assert await stack.recover() == []
    assert await stack.maintain() == []
    waiting = stack.store.get_run(stack.run_id)
    await stack.continue_jobs()
    await stack.until(lambda: stack.store.get_run(stack.run_id).attempt == 2)

    # Left as the kill left it, and then started once, by the job.
    assert (waiting.state, waiting.attempt, len(stack.scripts.launched)) == (WorkflowRunState.RUNNING, 1, 1)
    await stack.jobs.cancel(handle["job_id"], owner=stack.context.execution_identity, depth=0)
    await wait_for_status(stack.jobs, handle["job_id"], "cancelled")
    assert stack.store.get_run(stack.run_id).state is WorkflowRunState.CANCELLED
    assert stack.attempt.state is ScriptRunState.CANCELLED


@pytest.mark.asyncio
async def test_job_stopped_before_it_was_continued_leaves_no_attempt_running(stack: WorkflowStack) -> None:
    """A Stop or a denial that ends the waiting job ends the run with it: no pass starts an attempt nobody can stop."""
    handle = await _started(stack)
    await stack.restart(kill=True)
    assert await stack.recover() == []

    await stack.jobs.continue_stopped(handle["job_id"], None)
    assert await stack.maintain() == []

    run = stack.store.get_run(stack.run_id)
    assert (run.state, run.error, len(stack.scripts.launched)) == (WorkflowRunState.INTERRUPTED, RUNTIME_RESTARTED, 0)


@pytest.mark.asyncio
async def test_cancel_of_a_run_that_waits_for_its_next_attempt_cancels_it(stack: WorkflowStack) -> None:
    """The run is judged as it is now, not by how its last process ended, and no later pass starts it."""
    handle = await _started(stack)
    await _interrupt(stack, RUNTIME_RESTARTED)
    stack.scripts.refuse_launch = RuntimeError("Background script runtime reconciliation is in progress.")
    assert await stack.maintain() == []
    assert stack.store.get_run(stack.run_id).state is WorkflowRunState.STARTING

    await stack.jobs.cancel(handle["job_id"], owner=stack.context.execution_identity, depth=0)
    await wait_for_status(stack.jobs, handle["job_id"], "cancelled")
    stack.scripts.refuse_launch = None
    assert await stack.maintain() == []

    run = stack.store.get_run(stack.run_id)
    assert (run.state, run.error, run.attempt) == (WorkflowRunState.CANCELLED, "The run's job was cancelled.", 1)


@pytest.mark.asyncio
async def test_run_that_waits_for_an_attempt_when_its_job_ends_is_ended_too(stack: WorkflowStack) -> None:
    """A run with no process and no job would wait for ever and count against its owner's limit."""
    handle = await _started(stack)
    await _interrupt(stack, RUNTIME_RESTARTED)
    stack.scripts.refuse_launch = RuntimeError("Background script runtime reconciliation is in progress.")
    assert await stack.maintain() == []
    await stack.restart()
    await stack.jobs.continue_stopped(handle["job_id"], None)

    assert await stack.recover() == []

    run = stack.store.get_run(stack.run_id)
    assert (run.state, run.error) == (
        WorkflowRunState.CANCELLED,
        "The run's job ended before its next attempt started.",
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
@pytest.mark.parametrize("answer", ["ALLOWED", "DENIED"])
async def test_lifecycle_waits_for_room_membership_before_it_judges_the_owners_access(
    stack: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
    answer: str,
) -> None:
    """Membership that is still loading after a start is no denial: the run waits, and the loaded answer decides."""
    decisions = authorization._ReplyAuthorizationDecision
    decision = [decisions.PENDING]
    monkeypatch.setattr(authorization, "_responder_reply_authorization", lambda *_args, **_options: decision[0])
    bot = SimpleNamespace(
        running=True,
        config=stack.context.config,
        orchestrator=SimpleNamespace(agent_reply_memberships=object()),
        tool_runtime_support=SimpleNamespace(build_context=lambda _target, **_owner: stack.context),
    )
    paths = stack.context.runtime_paths
    resolver = _LiveScriptRuntimeResolver(
        runtime_paths=paths,
        bot_provider=lambda _name: bot,  # type: ignore[arg-type, return-value]
        worker_backend_provider=lambda _run: None,
    )
    script_runtime = SimpleNamespace(
        resolver=resolver,
        runtime_paths=paths,
        pass_timeout_seconds=30.0,
        retention_seconds=3600.0,
        after_reconcile=None,
    )
    lifecycle = WorkflowRuntimeLifecycle(engine=stack.engine, script_runtime=script_runtime)  # type: ignore[arg-type]
    await lifecycle.start()
    await stack.start(SCRIPT)
    await _interrupt(stack, RUNTIME_RESTARTED)

    await script_runtime.after_reconcile()
    pending = stack.store.get_run(stack.run_id)
    decision[0] = decisions[answer]
    await script_runtime.after_reconcile()

    run = stack.store.get_run(stack.run_id)
    assert (pending.state, pending.attempt, pending.error) == (WorkflowRunState.RUNNING, 1, None)
    assert (run.state, run.attempt, run.error) == (
        (WorkflowRunState.RUNNING, 2, None)
        if answer == "ALLOWED"
        else (WorkflowRunState.INTERRUPTED, 1, "The requester no longer has access.")
    )


@pytest.mark.asyncio
async def test_resume_that_was_admitted_and_killed_before_it_took_the_run_takes_it_as_accepted(
    stack: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The accepted request is saved with its job: the edited script and the named call survive the kill."""
    edited = "def main(args):\n    return 'counted again'\n"
    first = await _started(stack)
    await stack.result("@0", "tool", _A)
    await stack.finish_attempt(result="the first report")
    await _ended_job(stack, first["job_id"])
    monkeypatch.setattr(stack.engine, "reopen", lambda *_args, **_options: asyncio.Event().wait())
    resumed = json.loads(await stack.call("resume_workflow", run_id=stack.run_id, source=edited, rerun=["n1"]))

    await stack.restart(kill=True)
    assert await stack.recover() == []
    untouched = stack.store.get_run(stack.run_id)
    await stack.continue_jobs()
    await stack.until(lambda: stack.store.get_run(stack.run_id).attempt == 2)

    run = stack.store.get_run(stack.run_id)
    assert (untouched.state, untouched.attempt, untouched.job_id) == (WorkflowRunState.COMPLETED, 1, first["job_id"])
    assert (run.source, run.job_id) == (edited, resumed["job_id"])
    assert await stack.result("@0", "tool", _A) == "appended"
    await stack.finish_attempt(result="the second report")
    job = await _ended_job(stack, resumed["job_id"])
    assert (await stack.collect(job), stack.counting_tool.lines) == ("the second report", ["a", "a"])


@pytest.mark.asyncio
async def test_live_fault_script_counts_the_follow_ups_of_one_job(stack: WorkflowStack, tmp_path: Path) -> None:
    """`tests/workflow_live` reads deliveries from the journal by the ID the runtime gives a job's follow-up."""
    handle = await _started(stack)
    await stack.finish_attempt(result="report")
    await wait_for_status(stack.jobs, handle["job_id"], "completed")
    [first] = await stack.follow_ups()
    slot = tmp_path / "slot"
    (slot / "storage" / "tracking").mkdir(parents=True)
    with contextlib.closing(sqlite3.connect(slot / "storage" / "tracking" / "event_journal.db")) as journal:
        journal.execute("CREATE TABLE journal_events (principal_id TEXT, event_id TEXT)")
        other = "job-follow-up:0123456789abcdef:0123456789abcdef:1"
        journal.executemany("INSERT INTO journal_events VALUES ('helper', ?)", [(first,), (other,)])
        journal.commit()
    deployment = kill_after_write.Deployment(50, {}, slot)

    assert first.startswith("job-follow-up:")
    assert (deployment.deliveries(handle["job_id"]), deployment.deliveries("another-job")) == (1, 0)


def test_live_fault_script_finds_a_slot_that_only_the_agent_stack_started(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Docker-worker stack has no `just live` state: the stack's own `state.json` names the same account."""
    state = {
        "homeserver": "http://127.0.0.1:28141",
        "tester": {"userId": "@tester:matrix.localhost", "username": "tester", "accessToken": "placeholder"},
        "rooms": {"lobby": "!lobby:matrix.localhost", "space": "!space:matrix.localhost"},
    }
    (tmp_path / "slot-41").mkdir()
    (tmp_path / "slot-41" / "state.json").write_text(json.dumps(state))
    monkeypatch.setattr(kill_after_write, "_SLOTS", tmp_path / "no-live-slots")
    monkeypatch.setattr(kill_after_write, "_STACKS", tmp_path)

    deployment = kill_after_write.Deployment.find(41)

    assert (deployment.directory, deployment.live["lobby"]["roomId"]) == (
        tmp_path / "slot-41",
        "!lobby:matrix.localhost",
    )
    assert (deployment.live["homeserver"], deployment.live["userId"]) == (
        state["homeserver"],
        "@tester:matrix.localhost",
    )
    with pytest.raises(kill_after_write.LiveError, match="no ready live deployment on slot 42"):
        kill_after_write.Deployment.find(42)
