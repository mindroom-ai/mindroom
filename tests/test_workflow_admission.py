"""Tests for who may start an attempt of a workflow run, and who stops it.

One check admits every attempt (`WorkflowEngine.admit`); while a run's job is unfinished only that job's operation
starts one; and a Stop wins against a launch.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio

from mindroom.event_journal import ToolJobStore
from mindroom.orchestration import tool_job_runtime as coordinator_module
from mindroom.script_runs import manager as manager_module
from mindroom.script_runs.models import ScriptRunState
from mindroom.script_runs.reasons import MAX_RUNTIME_EXCEEDED, RUNTIME_RESTARTED
from mindroom.workflow_runs import engine as engine_module
from mindroom.workflow_runs.engine import WorkflowLimits
from mindroom.workflow_runs.models import WorkflowRunState
from tests.conftest import SETTLE_TIMEOUT_SECONDS
from tests.tool_job_helpers import wait_for_status
from tests.workflow_helpers import WorkflowStack, owner_context, tool_args, workflow_stack

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mindroom.tool_jobs.runtime import BackgroundJob
    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from mindroom.workflow_runs.models import WorkflowRun

SCRIPT = "def main(args):\n    return 'counted'\n"
_A = tool_args("counter", "append", line="a")
_NO_ACCESS = "The requester no longer has access."
_OUT_OF_TIME = "The run used its 72 hours of working time. resume_workflow continues it."


@pytest_asyncio.fixture
async def stack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[WorkflowStack]:
    """The harness with the real job runtime, which keeps the jobs the coordinator continues after a restart."""
    monkeypatch.setattr(manager_module, "background_script_supervision_supported", lambda: True)
    monkeypatch.setattr(engine_module, "_RECHECK_SECONDS", 0.05)
    with workflow_stack(tmp_path, extra_tools=("workflow",)) as stack:
        stack.continues = coordinator_module._continues
        await stack.open_jobs()
        yield stack
        await stack.close()


async def _started(stack: WorkflowStack, **arguments: object) -> dict[str, str]:
    """Start a run as its job and return the handle, once its first attempt runs."""
    handle = json.loads(await stack.call("start_workflow", source=SCRIPT, **arguments))
    stack.run_id = handle["run_id"]
    await stack.until(lambda: stack.store.get_run(stack.run_id).state is WorkflowRunState.RUNNING)
    return handle


async def _ended_job(stack: WorkflowStack, job_id: str) -> BackgroundJob:
    async with asyncio.timeout(SETTLE_TIMEOUT_SECONDS):
        return (await stack.jobs.wait(job_id, owner=stack.context.execution_identity, depth=0)).job


async def _interrupt(stack: WorkflowStack, reason: str = RUNTIME_RESTARTED) -> None:
    """End the attempt as the script runtime does when it stops a process for *reason*, with MindRoom still running."""
    await stack.scripts.end(stack.attempt.run_id, ScriptRunState.INTERRUPTED, reason=reason)


def _elsewhere(stack: WorkflowStack) -> ToolRuntimeContext:
    """Return the owner's context in another thread than the one the stack's runs are started in."""
    context = owner_context(stack.tmp_path, **stack.owner_options, thread_id="$elsewhere:example.test")
    return replace(context, orchestrator=stack.context.orchestrator)


def _run(stack: WorkflowStack) -> WorkflowRun:
    return stack.store.get_run(stack.run_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["start", "resume", "replay"])
@pytest.mark.parametrize(
    ("answer", "ends"),
    [(None, WorkflowRunState.STARTING), ("No attempt today.", WorkflowRunState.INTERRUPTED)],
    ids=["deferred", "refused"],
)
async def test_every_attempt_is_admitted_by_the_one_check_right_before_it_starts(
    stack: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    answer: str | None,
    ends: WorkflowRunState,
) -> None:
    """A start, a person's resume and a replay all ask `admit` under the run's lock: no answer starts no process.

    While it cannot answer, the run waits as it is, with its job; a refusal ends the run with the sentence.
    """
    if path != "start":
        first = await _started(stack)
        await (stack.end_attempt(ScriptRunState.FAILED) if path == "resume" else _interrupt(stack))
    if path == "resume":
        await _ended_job(stack, first["job_id"])
    launched, admit, asked = len(stack.scripts.launched), stack.engine.admit, []

    def answered(run: WorkflowRun, *args: Any) -> object:  # noqa: ANN401
        asked.append(run.state)
        # The check a resume makes before it changes anything passes: what is tested is the one before the launch.
        return admit(run, *args) if run.state is not WorkflowRunState.STARTING else answer

    monkeypatch.setattr(stack.engine, "admit", answered)
    if path == "replay":
        assert await stack.maintain() == []
    else:
        options = {"source": SCRIPT} if path == "start" else {"run_id": stack.run_id}
        stack.run_id = json.loads(await stack.call(f"{path}_workflow", **options))["run_id"]
    await stack.until(lambda: asked.count(WorkflowRunState.STARTING) >= 2 or _run(stack).state is ends)
    await asyncio.sleep(0.1)

    run = _run(stack)
    assert (run.state, run.error, len(stack.scripts.launched)) == (ends, answer, launched)
    job = next(job for job in stack.jobs.retained_jobs() if job.job_id == run.job_id)
    assert job.status == ("running" if answer is None else "interrupted")


@pytest.mark.asyncio
async def test_pass_starts_no_attempt_of_a_run_whose_job_was_stopped_before_its_operation_began(
    stack: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Stop that reaches the reply before its job's operation is scheduled leaves a job that reads `running`.

    Its cancel request is still on its way to the journal. Only the operation starts the run's attempts, and it
    never began, so the pass that runs meanwhile starts none; the Stop then ends the run.
    """
    span = SimpleNamespace(reply_id="reply-1", reply=SimpleNamespace(stop_receipt_order=3), stopped=lambda: True)
    monkeypatch.setattr("mindroom.tool_jobs.execution.current_span", lambda: span)
    save, at_save, saved = ToolJobStore.save, asyncio.Event(), asyncio.Event()

    async def held(store: ToolJobStore, job_id: str, job_json: str, *args: Any, **options: Any) -> None:  # noqa: ANN401
        if '"cancel_requested"' in job_json:
            at_save.set()
            await saved.wait()
        await save(store, job_id, job_json, *args, **options)

    monkeypatch.setattr(ToolJobStore, "save", held)
    call = asyncio.create_task(stack.call("start_workflow", source=SCRIPT))
    await asyncio.wait_for(at_save.wait(), SETTLE_TIMEOUT_SECONDS)
    [intent] = stack.store.list_runs()
    try:
        assert [job.status for job in stack.jobs.retained_jobs()] == ["running"]
        assert await stack.maintain() == []
        waiting = stack.store.get_run(intent.run_id)
    finally:
        saved.set()
    await call
    await wait_for_status(stack.jobs, str(intent.job_id), "cancelled")
    assert await stack.maintain() == []

    assert (waiting.state, waiting.attempt) == (WorkflowRunState.STARTING, 0)
    assert stack.store.get_run(intent.run_id).state is WorkflowRunState.CANCELLED
    assert stack.scripts.launched == []


@pytest.mark.asyncio
async def test_cancel_that_a_launch_overtakes_stops_the_attempt_that_launch_started(
    stack: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancel that found the run waiting for its attempt, and whose write came after that attempt began, ends it too."""
    await stack.start(SCRIPT)
    await _interrupt(stack)
    stack.access = "pending"
    assert await stack.maintain() == []
    end_run, overtaken = stack.engine._end_run, []

    async def overtaken_once(run: WorkflowRun, *args: Any, **changes: Any) -> WorkflowRun:  # noqa: ANN401
        if not overtaken:
            # The cancel read the run and let go of its lock; the launch that waited there takes it first.
            stack.access = None
            overtaken.append(await stack.engine.launch(run.run_id))
        return await end_run(run, *args, **changes)

    monkeypatch.setattr(stack.engine, "_end_run", overtaken_once)

    cancelled = await stack.engine.cancel(stack.run_id, reason="Cancelled by the agent.")

    [launched] = overtaken
    assert (launched.state, launched.attempt, len(stack.scripts.launched)) == (WorkflowRunState.RUNNING, 2, 2)
    assert (cancelled.state, cancelled.error) == (WorkflowRunState.CANCELLED, "Cancelled by the agent.")
    assert stack.script_store.get_run(str(launched.script_run_id)).state is ScriptRunState.CANCELLED
    assert _run(stack) == cancelled


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("resolved", "ends"),
    [(None, (WorkflowRunState.RUNNING, None, 1)), ("revoked", (WorkflowRunState.INTERRUPTED, _NO_ACCESS, 0))],
    ids=["confirmed", "revoked"],
)
async def test_resume_from_another_thread_that_took_the_run_waits_for_access_to_the_runs_own_conversation(
    stack: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
    resolved: str | None,
    ends: tuple[WorkflowRunState, str | None, int],
) -> None:
    """The run was taken and its primary died before the launch: the continued job waits while the access is pending.

    It fails nothing while nobody can say; once the access is confirmed the attempt starts, and proven gone it never does.
    """
    first = await _started(stack)
    await stack.end_attempt(ScriptRunState.FAILED)
    await _ended_job(stack, first["job_id"])
    monkeypatch.setattr(stack.engine, "launch", lambda *_args, **_options: asyncio.Event().wait())
    handle = json.loads(await stack.call("resume_workflow", context=_elsewhere(stack), run_id=stack.run_id))
    await stack.until(lambda: _run(stack).state is WorkflowRunState.STARTING)
    await stack.restart(kill=True)
    stack.access = "pending"
    assert await stack.recover() == []

    await stack.continue_jobs(_elsewhere(stack))
    await asyncio.sleep(0.3)
    waiting = _run(stack)
    [job] = [job for job in stack.jobs.retained_jobs() if job.job_id == handle["job_id"]]
    assert (waiting.state, waiting.job_id, waiting.error, job.status) == (
        WorkflowRunState.STARTING,
        handle["job_id"],
        None,
        "running",
    )
    assert stack.scripts.launched == []
    stack.access = resolved  # type: ignore[assignment]
    await stack.until(lambda: _run(stack).state is not WorkflowRunState.STARTING)

    run = _run(stack)
    assert (run.state, run.error, len(stack.scripts.launched)) == ends
    assert run.attempt == (2 if resolved is None else 1)


@pytest.mark.asyncio
async def test_resume_of_an_ended_run_needs_a_free_slot_of_its_owner(stack: WorkflowStack) -> None:
    """An ended run that is taken is live again, so it is counted like a start, in the write that takes it."""
    stack.limits = None
    first = await _started(stack)
    await stack.result("@0", "tool", _A)
    await stack.finish_attempt(result="the report")
    await _ended_job(stack, first["job_id"])
    ended = stack.engine.status(first["run_id"])
    for _ in range(8):
        await stack.call("start_workflow", source=SCRIPT)
    stack.run_id = first["run_id"]

    handle = json.loads(await stack.call("resume_workflow", run_id=first["run_id"], rerun=["n1"]))
    job = await _ended_job(stack, handle["job_id"])

    limit = "You have 8 workflow runs in progress, the limit. Wait for one to end or cancel one."
    assert (job.status, await stack.collect(job)) == ("failed", limit)
    assert stack.engine.status(first["run_id"]) == ended
    assert len(stack.store.list_runs(include_finished=False)) == 8


@pytest.mark.asyncio
async def test_run_that_used_its_working_time_in_its_last_attempt_gets_no_further_attempt(stack: WorkflowStack) -> None:
    """The time the ended attempt used is charged before the next one is admitted: none left starts no process."""
    await stack.start(SCRIPT, runtime_seconds_left=1.0)
    stack.now += 2
    await _interrupt(stack, MAX_RUNTIME_EXCEEDED)

    assert await stack.maintain() == []

    run = _run(stack)
    assert (run.state, run.error, run.runtime_seconds_left) == (WorkflowRunState.INTERRUPTED, _OUT_OF_TIME, 0.0)
    assert len(stack.scripts.launched) == 1


@pytest.mark.asyncio
async def test_waiting_for_access_after_an_attempt_ended_uses_no_working_time(stack: WorkflowStack) -> None:
    """The run is charged up to the end of its attempt, and not for the hours its owner's access could not be checked."""
    stack.limits = WorkflowLimits(max_runtime_hours=1)
    await stack.start(SCRIPT, runtime_seconds_left=3600.0)
    stack.now += 600
    await _interrupt(stack)
    stack.access = "pending"

    assert await stack.maintain() == []
    stack.now += 5 * 3600
    assert await stack.maintain() == []
    waiting = _run(stack)
    stack.access = None
    assert await stack.maintain() == [stack.run_id]

    assert (waiting.state, waiting.runtime_seconds_left) == (WorkflowRunState.STARTING, 3000.0)
    run = _run(stack)
    assert (run.state, run.attempt, run.runtime_seconds_left) == (WorkflowRunState.RUNNING, 2, 3000.0)
