"""Tests for the invocation that owns a workflow run: a job, or the call that waits for the run itself.

The owner is saved with the run's claim. Only it launches the run, cancels it or receives its report; the pass
deletes, launches and prunes nothing that an owner it cannot rule out still needs.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio

from mindroom.event_journal import ToolJobStore
from mindroom.orchestration import tool_job_runtime as coordinator_module
from mindroom.script_runs import manager as manager_module
from mindroom.script_runs.models import ScriptRunState
from mindroom.script_runs.reasons import RUNTIME_RESTARTED
from mindroom.workflow_runs import engine as engine_module
from mindroom.workflow_runs.engine import WorkflowLimits
from mindroom.workflow_runs.models import WorkflowRunState
from tests.conftest import SETTLE_TIMEOUT_SECONDS
from tests.tool_job_helpers import wait_for_status
from tests.workflow_helpers import CallFailed, WorkflowStack, set_run, tool_args, workflow_stack

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mindroom.tool_jobs.runtime import BackgroundJob
    from mindroom.workflow_runs.models import WorkflowRun

SCRIPT = "def main(args):\n    return 'counted'\n"
_A = tool_args("counter", "append", line="a")
_TAKEN = "Run {} was resumed by another call."


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
    await stack.until(lambda: _run(stack).state is WorkflowRunState.RUNNING)
    return handle


async def _ended_job(stack: WorkflowStack, job_id: str) -> BackgroundJob:
    async with asyncio.timeout(SETTLE_TIMEOUT_SECONDS):
        return (await stack.jobs.wait(job_id, owner=stack.context.execution_identity, depth=0)).job


def _run(stack: WorkflowStack) -> WorkflowRun:
    return stack.store.get_run(stack.run_id)


@pytest.mark.asyncio
async def test_resume_call_delivered_again_after_its_run_ended_returns_that_report_and_runs_nothing(
    stack: WorkflowStack,
) -> None:
    """A call that waits for its run itself owns the run it took: delivered again, it finds its own run, as it ended.

    Its `rerun` was applied once. Another call, with another ID, is a new resume.
    """
    first = await _started(stack)
    await stack.result("@0", "tool", _A)
    await stack.finish_attempt(result="the first report")
    await _ended_job(stack, first["job_id"])
    resume = {"depth": 1, "call_id": "one-resume", "run_id": stack.run_id, "rerun": ["n1"]}
    call = asyncio.create_task(stack.call("resume_workflow", **resume))
    await stack.until(lambda: _run(stack).attempt == 2)
    assert await stack.result("@0", "tool", _A) == "appended"
    await stack.finish_attempt(result="the second report")
    assert await call == "the second report"
    ended = stack.engine.status(stack.run_id)

    assert await stack.call("resume_workflow", **resume) == "the second report"

    assert stack.engine.status(stack.run_id) == ended
    assert (stack.counting_tool.lines, len(stack.scripts.launched)) == (["a", "a"], 2)


@pytest.mark.asyncio
async def test_start_call_delivered_again_after_a_job_took_its_run_neither_stops_nor_receives_that_run(
    stack: WorkflowStack,
) -> None:
    """The call that started a run owns it no more once a resume took it: it is told so, and its leaving ends nothing."""
    start = {"depth": 1, "call_id": "one-start", "source": SCRIPT}
    call = asyncio.create_task(stack.call("start_workflow", **start))
    await stack.until(lambda: [run.state for run in stack.store.list_runs()] == [WorkflowRunState.RUNNING])
    stack.run_id = stack.store.list_runs()[0].run_id
    await stack.finish_attempt(result="the first report")
    assert await call == "the first report"
    handle = json.loads(await stack.call("resume_workflow", run_id=stack.run_id))
    await stack.until(lambda: _run(stack).attempt == 2)

    with pytest.raises(CallFailed, match=_TAKEN.format(stack.run_id)):
        await stack.call("start_workflow", **start)

    run = _run(stack)
    assert (run.state, run.job_id, stack.attempt.state) == (
        WorkflowRunState.RUNNING,
        handle["job_id"],
        ScriptRunState.RUNNING,
    )
    await stack.finish_attempt(result="the second report")
    assert await stack.collect(await _ended_job(stack, handle["job_id"])) == "the second report"


@pytest.mark.asyncio
async def test_pass_deletes_no_intent_whose_start_is_being_admitted_again(
    stack: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An intent that an earlier primary left is its job's again once the start is delivered again.

    The pass deletes an intent only under its job's admission lock, which the delivery holds until the job is saved.
    """
    accept, at_accept, accepted = ToolJobStore.accept, asyncio.Event(), asyncio.Event()

    async def held(store: ToolJobStore, job_id: str, job_json: str) -> None:
        at_accept.set()
        await accepted.wait()
        await accept(store, job_id, job_json)

    start = {"call_id": "one-start", "source": SCRIPT}
    identity = {"owner": asdict(stack.context.execution_identity), "run_id": "run", "tool_call_id": "one-start"}
    job_id = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    # As a kill between the two writes left it: the intent is saved, and no job with its ID is.
    intent = stack.reserve(SCRIPT, origin_job_id=job_id, job_id=job_id)
    await stack.restart(kill=True)
    monkeypatch.setattr(ToolJobStore, "accept", held)

    again = asyncio.create_task(stack.call("start_workflow", **start))
    await asyncio.wait_for(at_accept.wait(), SETTLE_TIMEOUT_SECONDS)
    assert await stack.maintain() == []
    accepted.set()

    assert json.loads(await again)["run_id"] == intent.run_id
    await stack.until(lambda: stack.store.get_run(intent.run_id).state is WorkflowRunState.RUNNING)
    assert len(stack.scripts.launched) == 1


@pytest.mark.asyncio
async def test_pass_that_judged_a_run_before_a_job_took_it_starts_no_attempt_for_that_job(
    stack: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pass launches for the owner and the epoch it judged: a run that a resume took meanwhile is that job's."""
    await stack.start(SCRIPT)
    await stack.scripts.end(stack.attempt.run_id, ScriptRunState.FAILED)
    settle, wait = stack.engine.settle, stack.engine.wait
    judged, resumed, operating = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def slow_settle(run: WorkflowRun, **options: Any) -> WorkflowRun:  # noqa: ANN401
        judged.set()
        await resumed.wait()
        return await settle(run, **options)

    async def held_operation(run_id: str, *waiter: Any) -> WorkflowRun:  # noqa: ANN401
        if waiter:
            await operating.wait()
        return await wait(run_id, *waiter)

    monkeypatch.setattr(stack.engine, "settle", slow_settle)
    monkeypatch.setattr(stack.engine, "wait", held_operation)
    passing = asyncio.create_task(stack.maintain())
    await asyncio.wait_for(judged.wait(), SETTLE_TIMEOUT_SECONDS)
    # Another look ends the run, and a resume's job takes it; its operation has not launched yet.
    await settle(_run(stack))
    handle = json.loads(await stack.call("resume_workflow", run_id=stack.run_id))
    await stack.until(lambda: _run(stack).state is WorkflowRunState.STARTING)
    resumed.set()

    assert await passing == []
    run = _run(stack)
    assert (run.state, run.job_id, run.attempt, len(stack.scripts.launched)) == (
        WorkflowRunState.STARTING,
        handle["job_id"],
        1,
        1,
    )
    operating.set()
    await stack.until(lambda: _run(stack).attempt == 2)


@pytest.mark.asyncio
async def test_stop_that_a_restart_found_saved_delivers_the_report_of_a_run_that_had_completed(
    stack: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The run completed, its job was stopped before it saved the report, and MindRoom died with only the Stop saved.

    The next start settles the job as the stopped job's own handler would have: with the report of its own run.
    """
    wait, save = stack.engine.wait, ToolJobStore.save

    async def never_returns(*waiter: Any) -> WorkflowRun:  # noqa: ANN401
        await wait(*waiter)
        return await asyncio.Event().wait()  # type: ignore[return-value]

    dying: list[str] = []

    async def only_the_stop(store: ToolJobStore, job_id: str, job_json: str, *args: Any, **options: Any) -> None:  # noqa: ANN401
        # The primary that dies saves the Stop and nothing after it; the next one saves as usual.
        dying.append(store._runtime_generation)
        if store._runtime_generation == dying[0] and '"cancel_requested"' not in job_json:
            msg = "MindRoom is gone."
            raise OSError(msg)
        await save(store, job_id, job_json, *args, **options)

    monkeypatch.setattr(stack.engine, "wait", never_returns)
    handle = await _started(stack)
    await stack.finish_attempt(result="the report")
    monkeypatch.setattr(ToolJobStore, "save", only_the_stop)
    await stack.jobs.cancel(handle["job_id"], owner=stack.context.execution_identity, depth=0)
    await stack.until(lambda: [job.status for job in stack.jobs.retained_jobs()] != ["cancel_requested"])

    await stack.restart(kill=True)
    await stack.until(lambda: stack.jobs.retained_jobs()[0].status in ("completed", "cancelled"))

    [job] = stack.jobs.retained_jobs()
    assert (job.status, await stack.collect(job)) == ("completed", "the report")
    assert (_run(stack).state, _run(stack).result) == (WorkflowRunState.COMPLETED, "the report")


@pytest.mark.asyncio
async def test_launch_for_an_owner_or_an_epoch_the_run_no_longer_has_starts_nothing(stack: WorkflowStack) -> None:
    """Whoever launches says for whom and for which state of the run: a run that moved on is left as it is."""
    run = stack.reserve(SCRIPT, owner="inline:the-call-that-waits")

    for stale in ({"owner": "a-job-that-had-it-before"}, {"owner": run.owner, "epoch": run.epoch + 1}):
        left = await stack.engine.launch(run.run_id, stack.context, **stale)
        assert (left.state, left.attempt, stack.scripts.launched) == (WorkflowRunState.STARTING, 0, [])

    launched = await stack.engine.launch(run.run_id, stack.context, owner=run.owner, epoch=run.epoch)
    assert (launched.state, launched.attempt) == (WorkflowRunState.RUNNING, 1)


@pytest.mark.asyncio
async def test_pass_whose_launch_waited_while_another_owner_took_the_run_starts_nothing(stack: WorkflowStack) -> None:
    """The pass names the owner and the epoch it judged, so its launch is no launch for whoever has the run by then."""
    await stack.start(SCRIPT)
    await stack.scripts.end(stack.attempt.run_id, ScriptRunState.INTERRUPTED, reason=RUNTIME_RESTARTED)
    lock = stack.engine._condition(stack.run_id)
    settle, settled = stack.engine.settle, asyncio.Event()

    async def seen(*run: Any, **options: Any) -> WorkflowRun:  # noqa: ANN401
        try:
            return await settle(*run, **options)
        finally:
            await lock.acquire()
            settled.set()

    stack.engine.settle = seen  # type: ignore[method-assign]
    passing = asyncio.create_task(stack.maintain())
    await asyncio.wait_for(settled.wait(), SETTLE_TIMEOUT_SECONDS)
    await asyncio.sleep(0.05)
    # A cancel and a resume by a job, while the pass's launch waits for the run's lock.
    set_run(stack.store, stack.run_id, state=WorkflowRunState.CANCELLED)
    set_run(stack.store, stack.run_id, state=WorkflowRunState.STARTING, owner="the-job-of-a-resume")
    lock.release()

    assert await passing == []
    assert (_run(stack).state, _run(stack).attempt, len(stack.scripts.launched)) == (WorkflowRunState.STARTING, 1, 1)


@pytest.mark.asyncio
async def test_stop_of_a_resume_that_never_took_its_run_is_no_report_of_that_run(
    stack: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The run is still its last owner's, report included: the stopped job ends cancelled, and the run stays as it ended."""
    first = await _started(stack)
    await stack.finish_attempt(result="the first report")
    await _ended_job(stack, first["job_id"])
    monkeypatch.setattr(stack.engine, "reopen", lambda *_args, **_options: asyncio.Event().wait())
    handle = json.loads(await stack.call("resume_workflow", run_id=stack.run_id))

    await stack.jobs.cancel(handle["job_id"], owner=stack.context.execution_identity, depth=0)
    await wait_for_status(stack.jobs, handle["job_id"], "cancelled")

    run = _run(stack)
    assert (run.state, run.result, run.job_id) == (WorkflowRunState.COMPLETED, "the first report", first["job_id"])


@pytest.mark.asyncio
async def test_resume_call_delivered_again_while_its_run_still_runs_waits_for_that_run(stack: WorkflowStack) -> None:
    """The second delivery of a call that waits for its run is no second resume: it is not told to cancel the run first."""
    first = await _started(stack)
    await stack.finish_attempt(result="the first report")
    await _ended_job(stack, first["job_id"])
    resume = {"depth": 1, "call_id": "one-resume", "run_id": stack.run_id}
    calls = [asyncio.create_task(stack.call("resume_workflow", **resume))]
    await stack.until(lambda: _run(stack).attempt == 2)

    calls.append(asyncio.create_task(stack.call("resume_workflow", **resume)))
    await asyncio.sleep(0.1)
    await stack.finish_attempt(result="the second report")

    assert await asyncio.gather(*calls) == ["the second report", "the second report"]
    assert len(stack.scripts.launched) == 2


@pytest.mark.asyncio
async def test_attempt_after_an_interruption_starts_with_the_limits_the_agent_has_now(stack: WorkflowStack) -> None:
    """The job's own context and limits are those of its call: a replay starts with what the live runtime answers."""
    await _started(stack)
    stack.limits = WorkflowLimits(max_tool_calls_per_minute=7)

    await stack.scripts.end(stack.attempt.run_id, ScriptRunState.INTERRUPTED, reason=RUNTIME_RESTARTED)
    assert await stack.maintain() == []
    await stack.until(lambda: _run(stack).attempt == 2)

    limits = stack.scripts.limits[-1]
    assert limits is not None
    assert limits.max_tool_calls_per_minute == 7
