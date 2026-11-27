"""Tests for workflow runs across a restart of MindRoom: inherited attempts, replay, the run's job, automatic resume.

Level: engine harness (`WorkflowStack`): the real stores, broker, engine and job runtime; the test plays the script.
A killed primary is the harness's `restart(kill=True)`: its tasks stop where they are and only saved state survives.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import signal
import sqlite3
import sys
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
import structlog

from mindroom import authorization
from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.credentials.manager import get_runtime_credentials_manager
from mindroom.delegation.lifecycle import accessed_entities
from mindroom.orchestration import tool_job_runtime as coordinator_module
from mindroom.orchestration import workflow_runtime
from mindroom.orchestration.script_runtime import _LiveScriptRuntimeResolver
from mindroom.orchestration.workflow_runtime import WorkflowRuntimeLifecycle
from mindroom.script_runs import manager as manager_module
from mindroom.script_runs.broker import ScriptToolBroker
from mindroom.script_runs.manager import ScriptRuntimeNotReadyError
from mindroom.script_runs.models import ScriptCallState, ScriptRunState
from mindroom.script_runs.reasons import (
    MAX_RUNTIME_EXCEEDED,
    PLUGIN_TOOLS_CHANGED,
    RUNTIME_RESTARTED,
    RUNTIME_SHUTDOWN,
    WORKFLOW_TOOL_REMOVED,
)
from mindroom.tool_jobs import runtime as job_runtime_module
from mindroom.tool_jobs.runtime import workflow_job_standing
from mindroom.tool_system.worker_routing import (
    parse_tool_execution_identity_payload,
    serialize_tool_execution_identity,
)
from mindroom.workflow_runs import engine as engine_module
from mindroom.workflow_runs import nodes as nodes_module
from mindroom.workflow_runs.engine import SupersededAttemptError
from mindroom.workflow_runs.models import NodeRequest, WorkflowNodeState, WorkflowRunState
from mindroom.workflow_runs.recovery import AUTO_RESUME_REASONS
from mindroom.workflow_runs.store import WorkflowRunNotFoundError, WorkflowRunStoreError
from tests.access_schema_support import with_responder_access
from tests.conftest import SETTLE_TIMEOUT_SECONDS
from tests.tool_job_helpers import wait_for_status
from tests.workflow_helpers import NodeFailed, WorkflowStack, set_run, tool_args, workflow_stack
from tests.workflow_live import kill_after_write

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from mindroom.config.main import Config
    from mindroom.script_runs.models import ScriptCallRecord, ScriptRunRecord
    from mindroom.tool_jobs.runtime import BackgroundJob
    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from mindroom.workflow_runs.limits import WorkflowLimits
    from mindroom.workflow_runs.models import WorkflowRun

_A = tool_args("counter", "append", line="a")
_B = tool_args("counter", "append", line="b")
SCRIPT = "def main(args):\n    return 'counted'\n"


@pytest.fixture(autouse=True)
def _scripts_can_run_here(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let the placement check pass on every platform; the stack starts no process. A job asks again soon."""
    monkeypatch.setattr(manager_module, "background_script_supervision_supported", lambda: True)
    monkeypatch.setattr(engine_module, "RECHECK_SECONDS", 0.05)


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
    handle = json.loads(await stack.call("workflow_write", action="start", source=SCRIPT, **arguments))
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
        await nodes_module.submit_node(stack.engine, old, NodeRequest("@0", "tool", _A, {}))
    async with stack.http(token) as client:
        answers = [
            await client.get("run"),
            await client.post("nodes", json={"key": "@0", "kind": "tool", "args": _A}),
            await client.post("log", json={"text": "still here"}),
            await client.post("finish", json={"result": "late"}),
        ]
    assert [answer.status_code for answer in answers] == [409, 409, 409, 409]
    run, nodes = stack.status(stack.run_id)
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
    await nodes_module.finish(stack.engine, stack.attempt, result="reported before the kill", error=None)

    await stack.restart(kill=True)
    assert await stack.recover() == []
    # What the attempt left is settled at once: only a next attempt would wait for the job.
    settled = stack.store.get_run(stack.run_id).state
    await stack.continue_jobs()

    job = await _ended_job(stack, handle["job_id"])
    run = stack.store.get_run(stack.run_id)
    assert settled is WorkflowRunState.COMPLETED
    assert (run.state, run.attempt, len(stack.script_store.list_runs())) == (WorkflowRunState.COMPLETED, 1, 1)
    assert (job.status, await stack.collect(job)) == ("completed", "reported before the kill")


@pytest.mark.asyncio
async def test_crash_between_admission_and_launch_launches_once(
    stack: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The job was admitted and no attempt started: the intent stays, and the job's continuation launches it once."""
    monkeypatch.setattr(stack.engine, "launch", lambda *_args, **_options: asyncio.Event().wait())
    handle = json.loads(await stack.call("workflow_write", action="start", source=SCRIPT))
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
    handle = json.loads(await stack.call("workflow_write", action="start", source=SCRIPT))
    stack.run_id = handle["run_id"]
    await stack.restart(kill=True)
    stack.scripts.refuse_launch = RuntimeError("No worker is free.")

    await stack.next_primary()

    job = await _ended_job(stack, handle["job_id"])
    run = stack.store.get_run(stack.run_id)
    assert (run.state, run.error, job.status) == (WorkflowRunState.FAILED, "No worker is free.", "failed")


@pytest.mark.asyncio
async def test_first_attempt_of_a_continued_job_waits_for_a_script_runtime_that_is_still_starting(
    stack: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The job coordinator may continue a job before the script runtime takes launches: the job tries until it does."""
    monkeypatch.setattr(stack.engine, "launch", lambda *_args, **_options: asyncio.Event().wait())
    handle = json.loads(await stack.call("workflow_write", action="start", source=SCRIPT))
    stack.run_id = handle["run_id"]
    await stack.restart(kill=True)
    stack.scripts.refuse_launch = ScriptRuntimeNotReadyError("Background script runtime reconciliation is in progress.")
    assert await stack.recover() == []

    await stack.continue_jobs()
    await stack.until(lambda: stack.scripts.refused >= 2)
    # The pass starts nothing for a run whose job is at work on it.
    assert await stack.maintain() == []
    waiting, refused = stack.store.get_run(stack.run_id), stack.scripts.refused
    stack.scripts.refuse_launch = None
    await stack.until(lambda: stack.store.get_run(stack.run_id).attempt == 1)

    run = stack.store.get_run(stack.run_id)
    assert (waiting.state, waiting.attempt, waiting.error) == (WorkflowRunState.STARTING, 0, None)
    assert (run.state, run.idle_resumes, stack.scripts.refused) == (WorkflowRunState.RUNNING, 0, refused)
    await stack.finish_attempt(result="the report")
    assert await stack.collect(await _ended_job(stack, handle["job_id"])) == "the report"


@pytest.mark.asyncio
async def test_resume_that_waits_for_a_starting_script_runtime_counts_as_no_idle_attempt(stack: WorkflowStack) -> None:
    """The attempt a person's resume asked for is that person's, however often its job had to try."""
    first = await _started(stack)
    await stack.end_attempt(ScriptRunState.FAILED)
    await _ended_job(stack, first["job_id"])
    stack.scripts.refuse_launch = ScriptRuntimeNotReadyError("Background script runtime reconciliation is in progress.")

    handle = json.loads(await stack.call("workflow_write", action="resume", run_id=first["run_id"]))
    await stack.until(lambda: stack.scripts.refused >= 2)
    assert await stack.maintain() == []
    waiting = stack.store.get_run(stack.run_id)
    stack.scripts.refuse_launch = None
    await stack.until(lambda: stack.store.get_run(stack.run_id).attempt == 2)

    run = stack.store.get_run(stack.run_id)
    assert (waiting.state, waiting.job_id, waiting.error) == (WorkflowRunState.STARTING, handle["job_id"], None)
    assert (run.state, run.attempt, run.idle_resumes) == (WorkflowRunState.RUNNING, 2, 0)


@pytest.mark.asyncio
async def test_first_attempt_of_a_call_that_waits_itself_fails_on_a_starting_script_runtime(
    stack: WorkflowStack,
) -> None:
    """No pass starts the run of a call that waits for it in this process, so that call is told why nothing started."""
    stack.scripts.refuse_launch = ScriptRuntimeNotReadyError("Background script runtime reconciliation is in progress.")

    run = await stack.start(SCRIPT)

    assert (run.state, run.error) == (
        WorkflowRunState.FAILED,
        "Background script runtime reconciliation is in progress.",
    )


@pytest.mark.asyncio
async def test_crash_between_launch_and_naming_the_attempt_retires_it_and_launches_once(stack: WorkflowStack) -> None:
    """A process the run never named is retired like any inherited attempt, and the run gets one that it names."""
    stack.scripts.hold_launch = asyncio.Event()
    handle = json.loads(await stack.call("workflow_write", action="start", source=SCRIPT))
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
    # A delivery of a call holds its job's admission lock from the reservation until the job is saved.
    async with stack.jobs.admission("job-being-admitted-now"):
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
    status = json.loads(await stack.call("workflow_read", action="get", run_id=stack.run_id))
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
    # The call that reserves a run waits for it, and is counted before it reserves.
    stack.engine._attending["inline:here"] += 1
    fresh = stack.reserve(SCRIPT, origin_job_id="inline:here")
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
        handle = json.loads(await stack.call("workflow_write", action="start", source=SCRIPT))
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


async def _next_attempt(stack: WorkflowStack) -> None:
    """Run the pass on a run whose attempt ended, and wait for its next attempt.

    The pass settles the ended attempt. The next one is started by the operation of the run's job while that job is
    unfinished, and by the pass itself for a run that has no job or whose job has ended.
    """
    run = stack.store.get_run(stack.run_id)
    standing = "ended" if run.job_id is None else workflow_job_standing(stack.context.runtime_paths, run.job_id)
    assert await stack.maintain() == ([stack.run_id] if standing in ("ended", "absent") else [])
    await stack.until(lambda: stack.store.get_run(stack.run_id).attempt == run.attempt + 1)


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

    await _next_attempt(stack)
    await stack.result("@0", "tool", _B)
    await _interrupt(stack, PLUGIN_TOOLS_CHANGED)
    await _next_attempt(stack)

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
    await asyncio.sleep(0.2)
    assert await stack.maintain() == []
    waiting = stack.store.get_run(stack.run_id)
    stack.access = None
    await _next_attempt(stack)

    run = stack.store.get_run(stack.run_id)
    # The ended attempt is settled at once; only the next one waits for the access.
    assert (waiting.state, waiting.attempt, waiting.idle_resumes) == (WorkflowRunState.STARTING, 1, 0)
    assert (run.state, run.attempt, len(stack.scripts.launched)) == (WorkflowRunState.RUNNING, 2, 2)


@pytest.mark.asyncio
async def test_run_of_a_requester_without_access_ends_interrupted(stack: WorkflowStack) -> None:
    """Revoked access starts nothing and says why."""
    await _started(stack)
    await _interrupt(stack, RUNTIME_RESTARTED)
    stack.access = "revoked"

    assert await stack.maintain() == []
    await stack.until(lambda: stack.store.get_run(stack.run_id).state is not WorkflowRunState.STARTING)

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
        await _next_attempt(stack)
    await stack.result("@0", "tool", _A)
    for _ in range(5):
        await _interrupt(stack, RUNTIME_RESTARTED)
        await _next_attempt(stack)
    await _interrupt(stack, RUNTIME_RESTARTED)

    assert await stack.maintain() == []
    await stack.until(lambda: stack.store.get_run(stack.run_id).state is not WorkflowRunState.STARTING)

    run = stack.store.get_run(stack.run_id)
    assert (run.state, run.attempt) == (WorkflowRunState.INTERRUPTED, 8)
    assert (
        run.error
        == 'Stopped resuming after 5 attempts that finished nothing. Fix the script and call workflow_write(action="resume", run_id=...).'
    )


@pytest.mark.asyncio
async def test_refused_launch_of_an_automatic_resume_is_tried_again(stack: WorkflowStack) -> None:
    """A worker that is not there yet fails no run: the run's job tries again."""
    await _started(stack)
    await _interrupt(stack, RUNTIME_RESTARTED)
    stack.scripts.refuse_launch = RuntimeError("Background script runtime reconciliation is in progress.")

    with structlog.testing.capture_logs() as logs:
        assert await stack.maintain() == []
        await stack.until(lambda: stack.scripts.refused >= 3)
    refused = stack.store.get_run(stack.run_id)
    stack.scripts.refuse_launch = None
    await _next_attempt(stack)

    assert (refused.state, refused.error) == (WorkflowRunState.STARTING, None)
    assert stack.store.get_run(stack.run_id).attempt == 2
    # Said once for the attempt the run waits for, not with every try.
    assert [log["event"] for log in logs].count("workflow_launch_refused") == 1


@pytest.mark.asyncio
async def test_refused_launches_count_as_no_idle_attempt(stack: WorkflowStack) -> None:
    """A worker that is away for six tries uses up none of the five attempts: no attempt started."""
    await _started(stack)
    await _interrupt(stack, RUNTIME_RESTARTED)
    stack.scripts.refuse_launch = RuntimeError("Background script runtime reconciliation is in progress.")

    assert await stack.maintain() == []
    await stack.until(lambda: stack.scripts.refused >= 6)
    refused = stack.store.get_run(stack.run_id)
    stack.scripts.refuse_launch = None

    assert (refused.state, refused.idle_resumes) == (WorkflowRunState.STARTING, 0)
    await _next_attempt(stack)
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
    await _next_attempt(stack)
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
    await _next_attempt(stack)
    assert (await stack.submit("@0", *request)).cached
    assert stack.store.get_run(stack.run_id).idle_resumes == 1


@pytest.mark.asyncio
async def test_automatic_resume_keeps_the_used_time(stack: WorkflowStack) -> None:
    """Only a person's resume gives the full working time again."""
    await _started(stack)
    full = stack.store.get_run(stack.run_id).runtime_seconds_left
    stack.now += 600
    await _interrupt(stack, RUNTIME_RESTARTED)

    await _next_attempt(stack)

    # Less the real milliseconds between the script's end and the engine's look at it.
    assert (full, stack.store.get_run(stack.run_id).runtime_seconds_left) == (72 * 3600, pytest.approx(258600, abs=1))


@pytest.mark.asyncio
@pytest.mark.parametrize(("left", "resumed"), [(None, True), (120.0, True), (0.0, False)])
async def test_attempt_at_its_process_limit_is_resumed_only_while_working_time_is_left(
    stack: WorkflowStack,
    left: float | None,
    resumed: bool,
) -> None:
    """The attempt's own 30-day limit ends a process, and the run goes on while it has time or no limit."""
    await _started(stack)
    await _interrupt(stack, MAX_RUNTIME_EXCEEDED)
    set_run(stack.store, stack.run_id, runtime_seconds_left=left)

    assert await stack.maintain() == []
    await stack.until(lambda: stack.store.get_run(stack.run_id).state is not WorkflowRunState.STARTING)

    run = stack.store.get_run(stack.run_id)
    assert (run.state, run.runtime_seconds_left) == (
        WorkflowRunState.RUNNING if resumed else WorkflowRunState.INTERRUPTED,
        left,
    )
    # A run with no time left gets no process that would be stopped at once.
    out_of_time = 'The run used its 72 hours of working time. workflow_write(action="resume", run_id=...) continues it.'
    assert (run.attempt, run.error, len(stack.scripts.launched)) == ((2, None, 2) if resumed else (1, out_of_time, 1))


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
    look = asyncio.create_task(stack.settle())
    await stack.until(reading.is_set)

    await _next_attempt(stack)
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

    await _next_attempt(stack)

    assert (await stack.submit("@0", "tool", _A)).cached
    assert stack.counting_tool.lines == ["a", "a"]


async def _resumed_by_a_person(stack: WorkflowStack, **arguments: object) -> None:
    """End the attempt with a report and, once its job delivered it, resume the run as its owner's call does."""
    await stack.finish_attempt(result="the report")
    await _ended_job(stack, str(stack.store.get_run(stack.run_id).job_id))
    attempt = stack.store.get_run(stack.run_id).attempt
    await stack.call("workflow_write", action="resume", run_id=stack.run_id, **arguments)
    await stack.until(lambda: stack.store.get_run(stack.run_id).attempt == attempt + 1)


async def _interrupted_and_resumed(stack: WorkflowStack) -> None:
    """Lose the attempt to a restart of its process and let the pass start the next one, which remembers nothing."""
    await _interrupt(stack, PLUGIN_TOOLS_CHANGED)
    await _next_attempt(stack)


@pytest.mark.asyncio
async def test_changed_call_in_an_attempt_that_mindroom_started_logs_the_notice(stack: WorkflowStack) -> None:
    """An attempt that follows an interruption runs the script the last one ran, so a changed call is told there too."""
    await _started(stack)
    await stack.result("@0", "tool", _A)
    seq = stack.store.nodes(stack.run_id)[0].seq

    await _interrupted_and_resumed(stack)
    await stack.result("@0", "tool", _B)

    assert stack.store.log(stack.run_id) == [
        f"Call n{seq} changed between attempts without a script edit. It probably contains the time or a random "
        "value; use now() or random() from mindroom.workflow.",
    ]


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
async def test_live_question_after_a_rerun_is_not_reattached_after_an_interruption_between_them(
    stack: WorkflowStack,
) -> None:
    """A crash between a rerun call and an earlier attempt's live question does not reattach that stale question (B4)."""
    question: dict[str, object] = {"question": "Ship?", "options": ["Yes", "No"]}
    stack.show()
    await _started(stack)
    await stack.result("@0", "tool", _A)
    first = await stack.submit("@1", "ask", question)
    assert first.state == "waiting"
    node = stack.store.get_node(stack.run_id, "@0")
    assert node is not None
    await _resumed_by_a_person(stack, rerun=[f"n{node.seq}"])
    await stack.result("@0", "tool", _A)

    await _interrupted_and_resumed(stack)

    assert (await stack.submit("@0", "tool", _A)).cached
    second = await stack.submit("@1", "ask", question)
    saved = stack.store.get_node(stack.run_id, "@1")
    assert saved is not None
    assert (second.state, saved.generation) == ("waiting", 2)


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
async def test_run_whose_job_ended_without_a_stop_is_replayed_under_its_owner(stack: WorkflowStack) -> None:
    """A job the coordinator ended instead of continuing delivers nothing; the run goes on, and its report can be read."""
    handle = await _started(stack)
    await stack.restart(kill=True)
    await stack.jobs.continue_stopped(handle["job_id"], None)

    assert await stack.recover() == [stack.run_id]

    await stack.finish_attempt(result="the report")
    status = json.loads(await stack.call("workflow_read", action="get", run_id=stack.run_id))
    assert (status["status"], status["attempt"], status["result"]) == ("completed", 2, "the report")
    assert (await _ended_job(stack, handle["job_id"])).status == "interrupted"


@pytest.mark.asyncio
async def test_run_whose_job_ended_without_a_stop_ends_when_its_owner_lost_access(stack: WorkflowStack) -> None:
    """The owner's access decides, as for a run with no job."""
    handle = await _started(stack)
    await stack.restart(kill=True)
    await stack.jobs.continue_stopped(handle["job_id"], None)
    stack.access = "revoked"

    assert await stack.recover() == []

    run = stack.store.get_run(stack.run_id)
    assert (run.state, run.error, run.attempt) == (
        WorkflowRunState.INTERRUPTED,
        "The requester no longer has access.",
        1,
    )


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

    # The pass recorded the end of the attempt the kill left and started none; the job started the next one, once.
    assert (waiting.state, waiting.attempt, len(stack.scripts.launched)) == (WorkflowRunState.STARTING, 1, 1)
    await stack.jobs.cancel(handle["job_id"], owner=stack.context.execution_identity, depth=0)
    await wait_for_status(stack.jobs, handle["job_id"], "cancelled")
    assert stack.store.get_run(stack.run_id).state is WorkflowRunState.CANCELLED
    assert stack.attempt.state is ScriptRunState.CANCELLED


@pytest.mark.asyncio
async def test_job_stopped_before_it_was_continued_leaves_no_attempt_running(
    stack: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Stop of the waiting job ends the run with it: no pass starts an attempt nobody can stop, also while it drains."""
    handle = await _started(stack)
    await stack.restart(kill=True)
    assert await stack.recover() == []
    drained, settle = asyncio.Event(), stack.jobs._settle

    async def held(*args: Any) -> None:  # noqa: ANN401
        await drained.wait()
        await settle(*args)

    monkeypatch.setattr(stack.jobs, "_settle", held)
    stopping = asyncio.create_task(stack.jobs.cancel(handle["job_id"], owner=stack.context.execution_identity, depth=0))
    await wait_for_status(stack.jobs, handle["job_id"], "cancel_requested")

    # The job no longer waits to be continued and has not ended: it is being stopped, and nothing may start.
    try:
        started = await stack.maintain()
    finally:
        drained.set()
        await stopping
    assert (started, len(stack.scripts.launched)) == ([], 0)
    await wait_for_status(stack.jobs, handle["job_id"], "cancelled")
    assert await stack.maintain() == []

    run = stack.store.get_run(stack.run_id)
    stopped = "The run's job was stopped before its next attempt started."
    assert (run.state, run.error, len(stack.scripts.launched)) == (WorkflowRunState.CANCELLED, stopped, 0)


@pytest.mark.asyncio
async def test_pass_that_began_before_a_resume_was_admitted_leaves_the_resumed_run(
    stack: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run's job is looked up when the run is judged, so a job admitted after the pass began is not taken for gone."""
    first = await _started(stack)
    await stack.end_attempt(ScriptRunState.FAILED)
    await _ended_job(stack, first["job_id"])
    scanning, scanned, list_runs = threading.Event(), threading.Event(), stack.store.list_runs

    def slow_first_scan(**options: Any) -> list[WorkflowRun]:  # noqa: ANN401
        if not scanning.is_set():
            scanning.set()
            scanned.wait(SETTLE_TIMEOUT_SECONDS)
        return list_runs(**options)

    monkeypatch.setattr(stack.store, "list_runs", slow_first_scan)
    passing = asyncio.create_task(stack.maintain())
    await stack.until(scanning.is_set)
    stack.scripts.hold_launch = asyncio.Event()
    try:
        handle = json.loads(await stack.call("workflow_write", action="resume", run_id=first["run_id"]))
        await stack.until(lambda: len(stack.scripts.launched) == 2)
    finally:
        scanned.set()
    assert await passing == []
    stack.scripts.hold_launch.set()
    await stack.until(lambda: stack.store.get_run(stack.run_id).attempt == 2)

    run = stack.store.get_run(stack.run_id)
    assert (run.state, run.job_id, run.error) == (WorkflowRunState.RUNNING, handle["job_id"], None)


@pytest.mark.asyncio
async def test_pass_without_a_job_runtime_decides_nothing_about_the_run_of_a_job(
    stack: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With background jobs off the saved jobs wait untouched: no run of one is started, ended or deleted.

    Only the end of an attempt whose process is gone is recorded, which needs no word from the job.
    """
    never = "job-the-runtime-may-hold"
    intent = stack.reserve(SCRIPT, origin_job_id=never, job_id=never)
    await _started(stack)
    await stack.restart(kill=True)
    monkeypatch.setattr(job_runtime_module, "get_background_runtime", lambda _paths: None)

    assert await stack.recover() == []

    run = stack.store.get_run(stack.run_id)
    assert (run.state, run.attempt, len(stack.scripts.launched)) == (WorkflowRunState.STARTING, 1, 0)
    assert stack.store.get_run(intent.run_id).state is WorkflowRunState.STARTING


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
@pytest.mark.parametrize("restarted", [True, False])
async def test_run_whose_job_was_stopped_for_the_access_its_owner_lost_ends_interrupted_with_that_reason(
    stack: WorkflowStack,
    restarted: bool,
) -> None:
    """The job runtime stops a job whose requester lost the access, also one a restart left waiting: the run says why."""
    handle = await _started(stack)
    if restarted:
        await stack.restart(kill=True)
        assert await stack.recover() == []
    stack.access = "revoked"
    stack.refuse_admission()

    await stack.jobs.cancel_revoked()
    # The job ends as its run did, where that had an attempt to stop.
    await wait_for_status(stack.jobs, handle["job_id"], "cancelled" if restarted else "interrupted")
    assert await stack.maintain() == []

    run = stack.store.get_run(stack.run_id)
    assert (run.state, run.error) == (WorkflowRunState.INTERRUPTED, "The requester no longer has access.")


@pytest.mark.asyncio
async def test_run_that_waits_for_an_attempt_when_its_job_is_stopped_is_ended_too(stack: WorkflowStack) -> None:
    """A run with no process and no job would wait for ever and count against its owner's limit."""
    handle = await _started(stack)
    await _interrupt(stack, RUNTIME_RESTARTED)
    stack.scripts.refuse_launch = RuntimeError("Background script runtime reconciliation is in progress.")
    assert await stack.maintain() == []
    await stack.restart()
    await stack.jobs.cancel(handle["job_id"], owner=stack.context.execution_identity, depth=0)
    await wait_for_status(stack.jobs, handle["job_id"], "cancelled")

    assert await stack.recover() == []

    run = stack.store.get_run(stack.run_id)
    assert (run.state, run.error) == (
        WorkflowRunState.CANCELLED,
        "The run's job was stopped before its next attempt started.",
    )


def _script_runtime(
    stack: WorkflowStack,
    config: Config | None = None,
    *,
    synced: bool = True,
    rooms: tuple[str, ...] | None = None,
) -> SimpleNamespace:
    """Return what the workflow lifecycle uses of the script runtime: the real resolver over a bot of the stack's owner.

    *config* is the live configuration, the stack's own when not given; the agents it names have a running bot,
    which finished its first sync if *synced* and is in *rooms*, the stack's own room when not given.
    """
    config = config or stack.context.config
    bot = SimpleNamespace(
        running=True,
        first_sync_complete=synced,
        client=SimpleNamespace(rooms=dict.fromkeys((stack.context.room_id,) if rooms is None else rooms)),
        config=config,
        orchestrator=SimpleNamespace(agent_reply_memberships=object()),
        tool_runtime_support=SimpleNamespace(build_context=lambda _target, **_owner: stack.context),
    )
    paths = stack.context.runtime_paths
    resolver = _LiveScriptRuntimeResolver(
        runtime_paths=paths,
        bot_provider=lambda name: bot if name in config.agents else None,  # type: ignore[arg-type, return-value]
        worker_backend_provider=lambda _run: None,
    )
    return SimpleNamespace(
        resolver=resolver,
        runtime_paths=paths,
        config_provider=lambda: config,
        pass_timeout_seconds=30.0,
        retention_seconds=3600.0,
        after_reconcile=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("agents", [{"watcher": ["counter", "shell"]}, {"other": ["workflow"]}], ids=["tool", "agent"])
async def test_run_with_no_job_ends_when_its_agent_or_the_workflow_toolkit_was_removed_while_it_was_stopped(
    stack: WorkflowStack,
    agents: dict[str, list[str]],
) -> None:
    """The run's agent must still be offered `workflow`: what a restart finds removed ends the run, and it does not wait."""
    removed = stack.context.config.model_copy(
        update={"agents": {name: AgentConfig(display_name=name, tools=tools) for name, tools in agents.items()}},
    )
    script_runtime = _script_runtime(stack, removed)
    await stack.start(SCRIPT)
    await WorkflowRuntimeLifecycle(engine=stack.engine, script_runtime=script_runtime).start()  # type: ignore[arg-type]
    await _interrupt(stack, RUNTIME_RESTARTED)

    await script_runtime.after_reconcile()

    run = stack.store.get_run(stack.run_id)
    assert (run.state, run.attempt, run.error) == (
        WorkflowRunState.INTERRUPTED,
        1,
        "The requester no longer has access.",
    )
    assert len(stack.scripts.launched) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("synced", "rooms", "ends"),
    [
        (False, (), (WorkflowRunState.STARTING, None, 1)),
        (True, ("!another:example.test",), (WorkflowRunState.INTERRUPTED, "The requester no longer has access.", 1)),
        (True, None, (WorkflowRunState.RUNNING, None, 2)),
    ],
    ids=["before_sync", "left_the_room", "in_the_room"],
)
async def test_run_goes_on_only_once_its_bot_is_synced_and_still_in_the_runs_room(
    stack: WorkflowStack,
    synced: bool,
    rooms: tuple[str, ...] | None,
    ends: tuple[WorkflowRunState, str | None, int],
) -> None:
    """A requester the agent still answers does not make its bot serve a room it left: there the run gets no attempt.

    Before the bot's first sync nobody knows which rooms it is in, so the run waits.
    """
    script_runtime = _script_runtime(stack, synced=synced, rooms=rooms)
    await stack.start(SCRIPT)
    await WorkflowRuntimeLifecycle(engine=stack.engine, script_runtime=script_runtime).start()  # type: ignore[arg-type]
    await _interrupt(stack, RUNTIME_RESTARTED)

    await script_runtime.after_reconcile()

    run = stack.store.get_run(stack.run_id)
    assert (run.state, run.error, len(stack.scripts.launched)) == ends


def test_access_to_a_team_grants_its_members_and_no_agent_outside_it(stack: WorkflowStack) -> None:
    """Whoever reaches a team reaches its exact members through it; any other pair of bot and agent is asked for both."""
    config, member = stack.context.config.model_copy(deep=True), stack.context.agent_name
    config.teams["crew"] = TeamConfig(display_name="Crew", role="Answers together.", agents=[member])

    assert accessed_entities(config, "crew", member) == {"crew"}
    assert accessed_entities(config, "crew", "outsider") == {"crew", "outsider"}
    assert accessed_entities(config, "outsider", member) == {"outsider", member}


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_run_that_a_teams_member_started_is_answered_for_by_the_team(stack: WorkflowStack) -> None:
    """The requester reaches the member through the team, and only the team's bot is in the room: the run goes on.

    The real resolver answers. A plain script of the same member is its agent's own, as it was.
    """
    context, alice = stack.context, stack.context.requester_id
    config = context.config.model_copy(deep=True)
    config.teams["crew"] = TeamConfig(display_name="Crew", role="Answers together.", agents=[context.agent_name])
    with_responder_access(config, "crew", users=[alice])
    with_responder_access(config, context.agent_name, users=["@somebody-else:example.test"])

    def bot(*rooms: str) -> SimpleNamespace:
        return SimpleNamespace(
            running=True,
            first_sync_complete=True,
            client=SimpleNamespace(rooms=dict.fromkeys(rooms)),
            config=config,
            orchestrator=SimpleNamespace(agent_reply_memberships=AgentReplyMembershipIndex()),
            tool_runtime_support=SimpleNamespace(build_context=lambda _target, **_owner: context),
        )

    bots = {context.agent_name: bot(), "crew": bot(context.room_id)}
    for name, named in bots.items():
        named.agent_name = name
    resolver = _LiveScriptRuntimeResolver(context.runtime_paths, bots.get, lambda _run: None)  # type: ignore[arg-type]
    script_runtime = SimpleNamespace(
        resolver=resolver,
        runtime_paths=context.runtime_paths,
        config_provider=lambda: config,
    )
    lifecycle = WorkflowRuntimeLifecycle(engine=stack.engine, script_runtime=script_runtime)  # type: ignore[arg-type]
    await stack.start(SCRIPT)
    served = serialize_tool_execution_identity(replace(context.execution_identity, transport_agent_name="crew"))
    run = replace(stack.store.get_run(stack.run_id), execution_identity=served)

    access = lifecycle._access(run)

    assert access is context
    # The team's bot posts the run's cards, so the answers and the Resume a person sends to them are its to act on.
    own = stack.store.get_run(stack.run_id)
    assert (lifecycle._served_by(run), lifecycle._served_by(own)) == ("crew", context.agent_name)
    attempt = replace(stack.attempt, execution_identity=served)
    assert resolver.is_authorized(attempt) is True
    assert resolver.is_authorized(replace(attempt, workflow_run_id=None)) is False


@pytest.mark.asyncio
async def test_run_that_a_named_child_started_is_served_by_the_bot_its_turn_ran_through(tmp_path: Path) -> None:
    """Agent A ran agent B as a child and B started the run: the run is B's, and A's bot carries its calls.

    The identity the run saved names A as its transport, so the context rebuilt for each call must too: B's own
    bot would stamp itself, and the broker would refuse every call of the run.
    """
    with workflow_stack(tmp_path, extra_tools=("workflow",), other_agents=("helper",), require_approval=()) as stack:
        # As `child_tool_runtime_context` leaves it: the child is the agent, and the caller's bot the transport.
        caller = replace(stack.context, agent_name="helper", transport_agent_name="watcher")
        built: list[str] = []

        def bot(name: str) -> SimpleNamespace:
            def build(target: object, *, user_id: str, agent_name: str, correlation_id: str) -> ToolRuntimeContext:
                built.append(name)
                moved = {"target": target, "requester_id": user_id, "agent_name": agent_name}
                return replace(caller, transport_agent_name=name, correlation_id=correlation_id, **moved)

            return SimpleNamespace(
                running=True,
                config=caller.config,
                tool_runtime_support=SimpleNamespace(build_context=build),
            )

        bots = {name: bot(name) for name in ("watcher", "helper")}
        resolver = _LiveScriptRuntimeResolver(caller.runtime_paths, bots.get, lambda _run: None)  # type: ignore[arg-type]
        stack.context = caller
        await stack.start()
        try:
            attempt = stack.attempt
            saved = parse_tool_execution_identity_payload(attempt.execution_identity)

            resolved = resolver.resolve(attempt, correlation_id="a-call")

            assert (attempt.agent_name, saved.transport_agent_name, built) == ("helper", "watcher", ["watcher"])
            assert resolved.execution_identity == saved
            # A plain script of the child is its agent's own, as it was.
            resolver.resolve(replace(attempt, workflow_run_id=None), correlation_id="a-call")
            assert built == ["watcher", "helper"]
        finally:
            await stack.engine.close()


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
@pytest.mark.parametrize("lost", ["the requester's access to the child", "the serving bot's place in the room"])
async def test_run_of_a_named_child_answers_to_the_childs_access_and_to_the_room_of_the_bot_that_serves_it(
    tmp_path: Path,
    lost: str,
) -> None:
    """Agent A's bot carries the calls of a run that is agent B's: it grants nothing that B's own access does not.

    One answer serves every call: the gateway's and the broker's (`is_authorized`) and an attempt's or an agent's
    (`_access`). The reproducers of the second full review (Sol R2-B and B2, Astra B4).
    """
    with workflow_stack(tmp_path, extra_tools=("workflow",), other_agents=("helper",)) as stack:
        context, config = stack.context, stack.context.config
        config.agents["helper"].tools = ["counter", "workflow"]
        for agent in ("watcher", "helper"):
            with_responder_access(config, agent, users=[context.requester_id])
        stack.context = replace(context, agent_name="helper", transport_agent_name="watcher")
        rooms = {context.room_id: None}
        bot = SimpleNamespace(
            running=True,
            first_sync_complete=True,
            client=SimpleNamespace(rooms=rooms),
            config=config,
            orchestrator=SimpleNamespace(agent_reply_memberships=AgentReplyMembershipIndex()),
            tool_runtime_support=SimpleNamespace(build_context=lambda _target, **_owner: stack.context),
        )
        resolver = _LiveScriptRuntimeResolver(context.runtime_paths, lambda _name: bot, lambda _run: None)  # type: ignore[arg-type]
        script_runtime = SimpleNamespace(resolver=resolver, config_provider=lambda: config)
        lifecycle = WorkflowRuntimeLifecycle(engine=stack.engine, script_runtime=script_runtime)  # type: ignore[arg-type]
        run = await stack.start()
        try:
            assert (lifecycle._access(run) is stack.context, resolver.is_authorized(stack.attempt)) == (True, True)

            if lost == "the serving bot's place in the room":
                rooms.clear()
            else:
                with_responder_access(config, "helper", users=["@somebody-else:example.test"])

            assert (lifecycle._access(run), resolver.is_authorized(stack.attempt)) == ("revoked", False)
        finally:
            await stack.engine.close()


_NOT_READY = "The run's agent is not ready in the run's room yet. Try again in a moment."
_NO_TOOLKIT = {"watcher": AgentConfig(display_name="watcher", tools=["counter", "shell"])}


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["start", "resume"])
@pytest.mark.parametrize(
    ("agents", "synced", "rooms", "answer"),
    [
        (None, False, None, None),
        (None, True, ("!another:example.test",), "The requester no longer has access."),
        (_NO_TOOLKIT, True, None, "The requester no longer has access."),
    ],
    ids=["before_sync", "left_the_room", "toolkit_removed"],
)
async def test_attempt_asked_for_in_the_runs_own_conversation_needs_the_owners_live_access_too(
    stack: WorkflowStack,
    path: str,
    agents: dict[str, AgentConfig] | None,
    synced: bool,
    rooms: tuple[str, ...] | None,
    answer: str | None,
) -> None:
    """The context of a call says who was allowed when it was made: each launch asks the live runtime, as a replay does.

    A start waits while that cannot answer and ends with its refusal; a resume is refused before it changes the run.
    """
    if path == "resume":
        await stack.start(SCRIPT)
        await stack.end_attempt(ScriptRunState.FAILED)
    before = stack.store.get_run(stack.run_id) if path == "resume" else None
    config = stack.context.config.model_copy(update={"agents": agents}) if agents else None
    script_runtime = _script_runtime(stack, config, synced=synced, rooms=rooms)
    await WorkflowRuntimeLifecycle(engine=stack.engine, script_runtime=script_runtime).start()  # type: ignore[arg-type]

    if path == "start":
        run = await stack.start(SCRIPT)
        ends = WorkflowRunState.INTERRUPTED if answer else WorkflowRunState.STARTING
        assert (run.state, run.error, len(stack.scripts.launched)) == (ends, answer, 0)
    else:
        with pytest.raises(WorkflowRunStoreError, match=re.escape(answer or _NOT_READY)):
            await stack.resume(stack.run_id)
        assert (stack.store.get_run(stack.run_id), len(stack.scripts.launched)) == (before, 1)


@pytest.mark.asyncio
async def test_pass_keeps_an_old_report_until_its_job_has_delivered_it(stack: WorkflowStack) -> None:
    """A report that was stored before a long stop is older than the retention, and still the job's to deliver."""
    handle = await _started(stack)
    await nodes_module.finish(stack.engine, stack.attempt, result="the report", error=None)
    set_run(stack.store, stack.run_id, finished_at="2026-01-01T00:00:00Z")
    await stack.restart(kill=True)
    await stack._retire_attempts(RUNTIME_RESTARTED)
    script_runtime = _script_runtime(stack)
    await WorkflowRuntimeLifecycle(engine=stack.engine, script_runtime=script_runtime).start()  # type: ignore[arg-type]

    # The job waits to be continued: the pass ends the run with its report and keeps it.
    await script_runtime.after_reconcile()
    run = stack.store.get_run(stack.run_id)
    assert (run.state, run.result, run.finished_at) == (
        WorkflowRunState.COMPLETED,
        "the report",
        "2026-01-01T00:00:00Z",
    )
    await stack.continue_jobs()
    job = await _ended_job(stack, handle["job_id"])
    assert (job.status, await stack.collect(job)) == ("completed", "the report")

    # The report is saved as the job's end now, so the run is as old as it is.
    await script_runtime.after_reconcile()
    with pytest.raises(WorkflowRunNotFoundError):
        stack.store.get_run(stack.run_id)


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
    script_runtime = _script_runtime(stack)
    lifecycle = WorkflowRuntimeLifecycle(engine=stack.engine, script_runtime=script_runtime)  # type: ignore[arg-type]
    await stack.start(SCRIPT)
    await lifecycle.start()
    await _interrupt(stack, RUNTIME_RESTARTED)

    await script_runtime.after_reconcile()
    pending = stack.store.get_run(stack.run_id)
    decision[0] = decisions[answer]
    await script_runtime.after_reconcile()

    run = stack.store.get_run(stack.run_id)
    assert (pending.state, pending.attempt, pending.error) == (WorkflowRunState.STARTING, 1, None)
    assert (run.state, run.attempt, run.error) == (
        (WorkflowRunState.RUNNING, 2, None)
        if answer == "ALLOWED"
        else (WorkflowRunState.INTERRUPTED, 1, "The requester no longer has access.")
    )


@pytest.mark.asyncio
async def test_stored_workflow_settings_apply_to_admit_without_reading_on_authorize(
    stack: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dashboard-stored workflow settings apply at admit and auto-resume, while authorize returns the context alone."""
    get_runtime_credentials_manager(stack.context.runtime_paths).save_credentials(
        "workflow",
        {"max_tool_calls_per_minute": 42, "max_runtime_hours": 12},
    )
    reads = 0
    real_limits = workflow_runtime.workflow_limits

    def counted_limits(context: ToolRuntimeContext, agent_name: str) -> WorkflowLimits:
        nonlocal reads
        reads += 1
        return real_limits(context, agent_name)

    monkeypatch.setattr(workflow_runtime, "workflow_limits", counted_limits)
    script_runtime = _script_runtime(stack)
    lifecycle = WorkflowRuntimeLifecycle(engine=stack.engine, script_runtime=script_runtime)  # type: ignore[arg-type]
    await stack.start(SCRIPT)
    await lifecycle.start()
    await _interrupt(stack, RUNTIME_RESTARTED)

    await script_runtime.after_reconcile()

    run = stack.store.get_run(stack.run_id)
    reads_before_authorize = reads
    authorized = stack.engine.authorize(run)
    assert not isinstance(authorized, (tuple, str))
    assert authorized is not None
    assert reads == reads_before_authorize
    admitted = stack.engine.admit(run)
    assert isinstance(admitted, tuple)
    assert (admitted[1].max_tool_calls_per_minute, admitted[1].max_runtime_hours) == (42, 12)
    assert reads == reads_before_authorize + 1
    limits = stack.scripts.limits[-1]
    assert limits is not None
    assert limits.max_tool_calls_per_minute == 42


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
    resumed = json.loads(
        await stack.call("workflow_write", action="resume", run_id=stack.run_id, source=edited, rerun=["n1"]),
    )

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
async def test_accepted_resume_waits_for_the_owners_access_instead_of_failing_its_job(
    stack: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh call is refused while the access cannot be answered; a job that was accepted keeps its request.

    Until the answer comes it takes nothing and marks no call, and then it takes the run as it was accepted.
    """
    first = await _started(stack)
    await stack.result("@0", "tool", _A)
    await stack.finish_attempt(result="the first report")
    await _ended_job(stack, first["job_id"])
    monkeypatch.setattr(stack.engine, "reopen", lambda *_args, **_options: asyncio.Event().wait())
    resumed = json.loads(await stack.call("workflow_write", action="resume", run_id=stack.run_id, rerun=["n1"]))
    await stack.restart(kill=True)
    stack.access = "pending"

    await stack.continue_jobs()
    await asyncio.sleep(0.3)
    waiting = stack.store.get_run(stack.run_id)
    [job] = [job for job in stack.jobs.retained_jobs() if job.job_id == resumed["job_id"]]
    assert (waiting.state, waiting.job_id, job.status) == (WorkflowRunState.COMPLETED, first["job_id"], "running")
    stack.access = None
    await stack.until(lambda: stack.store.get_run(stack.run_id).attempt == 2)

    assert stack.store.get_run(stack.run_id).job_id == resumed["job_id"]
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


def test_live_fault_script_start_tolerates_empty_proc_cmdline_during_exec(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`Backend.start()` tracks its child `Popen` instead of racing Linux's transient empty `/proc/<pid>/cmdline`."""
    slot = tmp_path / "slot-50"
    slot.mkdir()
    state = {"pids": {"backend": 0}, "logs": {"backend": str(slot / "backend.log")}, "apiUrl": "http://127.0.0.1:1"}
    (slot / "state.json").write_text(json.dumps(state))
    deployment = kill_after_write.Deployment(50, {}, slot)
    backend = kill_after_write.Backend(
        deployment,
        0,
        [sys.executable, "-c", "import time; time.sleep(60)", str(slot)],
        slot,
        {},
    )
    real_read_bytes = Path.read_bytes

    def empty_cmdline(self: Path) -> bytes:
        if self.parts[:2] == ("/", "proc") and self.name == "cmdline":
            return b""
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", empty_cmdline)
    monkeypatch.setattr(
        kill_after_write.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: contextlib.nullcontext(SimpleNamespace(status=200)),
    )

    try:
        backend.start()
        assert backend.running
        assert deployment.state["pids"]["backend"] == backend.pid
        backend.stop()
        assert not backend.running
    finally:
        if backend.pid > 0:
            with contextlib.suppress(ProcessLookupError):
                os.kill(backend.pid, signal.SIGKILL)
            with contextlib.suppress(ChildProcessError):
                os.waitpid(backend.pid, 0)


def test_live_fault_script_start_raises_when_backend_actually_exits(tmp_path: Path) -> None:
    """`Backend.start()` still fails fast when the spawned backend genuinely exits before `/api/health` responds."""
    slot = tmp_path / "slot-50"
    slot.mkdir()
    state = {"pids": {"backend": 0}, "logs": {"backend": str(slot / "backend.log")}, "apiUrl": "http://127.0.0.1:1"}
    (slot / "state.json").write_text(json.dumps(state))
    deployment = kill_after_write.Deployment(50, {}, slot)
    backend = kill_after_write.Backend(
        deployment,
        0,
        [sys.executable, "-c", "raise SystemExit(7)", str(slot)],
        slot,
        {},
    )

    with pytest.raises(kill_after_write.LiveError, match=r"the backend exited while starting \(exit 7\)"):
        backend.start()
