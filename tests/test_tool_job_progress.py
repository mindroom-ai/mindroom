"""Live progress that a running job's tool reports, read by the agent through the job's summary."""

from __future__ import annotations

import asyncio
import contextvars
import json
from typing import TYPE_CHECKING

import pytest

from mindroom.tool_jobs.runtime import BackgroundOutcome, job_summary
from mindroom.tool_system.progress import report_tool_progress
from tests.tool_job_helpers import job_owner, tool_job_runtime, wait_for_status

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.tool_jobs.runtime import ToolJobRuntime
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


async def _until_progress(runtime: ToolJobRuntime, owner: ToolExecutionIdentity, expected: str) -> dict[str, object]:
    for _ in range(200):
        jobs = await runtime.list_jobs(owner=owner, depth=0)
        summary = job_summary(jobs[0])
        if summary.get("progress") == expected:
            return summary
        await asyncio.sleep(0.01)
    raise AssertionError(expected)


@pytest.mark.asyncio
async def test_a_running_job_shows_the_latest_progress_its_tool_reported(tmp_path: Path) -> None:
    """Progress reported on the loop or from a worker thread replaces the last one while the job runs."""
    runtime = await tool_job_runtime(tmp_path)
    owner = job_owner()
    finish = asyncio.Event()

    async def operation() -> BackgroundOutcome:
        report_tool_progress("compiling 1/2")
        await asyncio.to_thread(report_tool_progress, "compiling 2/2")
        await finish.wait()
        return BackgroundOutcome("completed", "built")

    try:
        await runtime.start("job-1", tool_name="make", depth=0, adapter={}, owner=owner, operation=operation)
        summary = await _until_progress(runtime, owner, "compiling 2/2")
        assert summary["status"] == "running"

        finish.set()
        await wait_for_status(runtime, "job-1", "completed")
        ended = job_summary((await runtime.list_jobs(owner=owner, depth=0))[0])
        assert "progress" not in ended
        assert ended["summary"] == "built"
    finally:
        finish.set()
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_a_wait_that_times_out_returns_the_running_jobs_progress(tmp_path: Path) -> None:
    """The handle a timed-out wait returns carries the latest progress, so the agent can check on the job."""
    runtime = await tool_job_runtime(tmp_path)
    owner = job_owner()
    finish = asyncio.Event()

    async def operation() -> BackgroundOutcome:
        report_tool_progress("step 3 of 9")
        await finish.wait()
        return BackgroundOutcome("completed", "done")

    try:
        await runtime.start("job-1", tool_name="make", depth=0, adapter={}, owner=owner, operation=operation)
        await _until_progress(runtime, owner, "step 3 of 9")
        waited = await runtime.wait("job-1", owner=owner, depth=0, timeout=0.01)

        assert waited.claim is None
        assert job_summary(waited.job)["progress"] == "step 3 of 9"
    finally:
        finish.set()
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_progress_keeps_only_its_latest_characters(tmp_path: Path) -> None:
    """Progress is capped to its last characters wherever it comes from."""
    runtime = await tool_job_runtime(tmp_path)
    owner = job_owner()
    finish = asyncio.Event()
    text = "x" * 2000 + "tail"

    async def operation() -> BackgroundOutcome:
        report_tool_progress(text)
        await finish.wait()
        return BackgroundOutcome("completed", "done")

    try:
        await runtime.start("job-1", tool_name="make", depth=0, adapter={}, owner=owner, operation=operation)
        summary = await _until_progress(runtime, owner, text[-2000:])
        assert str(summary["progress"]).endswith("tail")
    finally:
        finish.set()
        await runtime.shutdown()


def test_progress_outside_a_job_is_ignored() -> None:
    """A tool that reports progress while nothing listens keeps running."""
    report_tool_progress("nobody listens")


@pytest.mark.asyncio
async def test_a_report_after_the_tool_returned_does_not_change_the_job(tmp_path: Path) -> None:
    """A thread that outlives its tool call cannot attach progress to the settled job or reorder it."""
    runtime = await tool_job_runtime(tmp_path)
    owner = job_owner()
    tool_context: list[contextvars.Context] = []

    async def operation() -> BackgroundOutcome:
        tool_context.append(contextvars.copy_context())
        return BackgroundOutcome("completed", "done")

    try:
        await runtime.start("job-1", tool_name="make", depth=0, adapter={}, owner=owner, operation=operation)
        await wait_for_status(runtime, "job-1", "completed")
        settled = (await runtime.list_jobs(owner=owner, depth=0))[0]

        await asyncio.to_thread(tool_context[0].run, report_tool_progress, "late")
        await asyncio.sleep(0)

        job = (await runtime.list_jobs(owner=owner, depth=0))[0]
        assert job == settled
        assert job.progress is None
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_progress_is_never_saved_with_the_job(tmp_path: Path) -> None:
    """Progress is live state: a saved snapshot of a running job carries none."""
    runtime = await tool_job_runtime(tmp_path)
    owner = job_owner()
    finish = asyncio.Event()

    async def operation() -> BackgroundOutcome:
        report_tool_progress("step 1")
        await finish.wait()
        return BackgroundOutcome("completed", "done")

    try:
        await runtime.start("job-1", tool_name="make", depth=0, adapter={}, owner=owner, operation=operation)
        await _until_progress(runtime, owner, "step 1")
        await runtime.set_awaiting_approval("job-1", awaiting=True)

        [saved] = await runtime._store.load_all()
        assert json.loads(saved.job_json)["status"] == "awaiting_approval"
        assert json.loads(saved.job_json).get("progress") is None
        assert job_summary((await runtime.list_jobs(owner=owner, depth=0))[0])["progress"] == "step 1"
    finally:
        finish.set()
        await runtime.shutdown()
