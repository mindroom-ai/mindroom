"""Returned outcomes and shutdown cleanup survive cancellation and storage failures."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import pytest

from mindroom.tool_jobs import runtime as runtime_module
from mindroom.tool_jobs.resources import current_execution_resources
from mindroom.tool_jobs.runtime import BackgroundOutcome, JobSpec, ToolJobRuntime
from tests.test_background_subagents import _owner

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
@pytest.mark.parametrize("shutdown", [False, True])
async def test_returned_result_survives_stop_during_resource_cleanup(tmp_path: Path, shutdown: bool) -> None:
    """A completed side effect retains its output, but only after its resource cleanup drains."""
    runtime = ToolJobRuntime(tmp_path)
    cleaning, release, cleaned = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def cleanup() -> None:
        cleaning.set()
        await release.wait()
        cleaned.set()

    async def operation() -> BackgroundOutcome:
        resources = current_execution_resources()
        assert resources is not None
        reference = resources.acquire()
        assert resources.defer(cleanup)
        await reference.release()
        (tmp_path / "effect.txt").write_text("completed once")
        return BackgroundOutcome("completed", "exact result", result_payload={"retained": [1, 2]})

    stopping = None
    try:
        await runtime.start(JobSpec("returned", "tool", 0), owner=_owner(), operation=operation)
        await asyncio.wait_for(cleaning.wait(), 2)
        stopping = asyncio.create_task(
            runtime.shutdown()
            if shutdown
            else runtime.cancel("returned", owner=_owner(), depth=0, await_completion=True),
        )
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(stopping), 0.02)
        assert not cleaned.is_set()
        snapshot = json.loads((tmp_path / "tool_jobs" / "returned.json").read_text())
        assert snapshot["status"] in {"running", "cancel_requested"}
        release.set()
        await asyncio.wait_for(stopping, 2)
        assert cleaned.is_set()
        assert (tmp_path / "effect.txt").read_text() == "completed once"
        snapshot = json.loads((tmp_path / "tool_jobs" / "returned.json").read_text())
        assert snapshot["status"] == "completed"
        assert snapshot["result"] == "exact result"
        assert snapshot["result_payload"] == {"retained": [1, 2]}
    finally:
        release.set()
        if stopping is not None:
            await asyncio.gather(stopping, return_exceptions=True)
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_returned_result_survives_cancel_admission_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Cancellation cannot overwrite output returned while its durable admission owns the lock."""
    runtime = ToolJobRuntime(tmp_path)
    finish, returned, saving, release = (asyncio.Event() for _ in range(4))
    persist = runtime._persist

    async def operation() -> BackgroundOutcome:
        await finish.wait()
        (tmp_path / "effect.txt").write_text("completed once")
        returned.set()
        return BackgroundOutcome("completed", "exact result")

    async def delayed_persist(entry: runtime_module._Entry, *, update_timestamp: bool = True) -> None:
        if entry.job.status == "cancel_requested":
            saving.set()
            await release.wait()
        await persist(entry, update_timestamp=update_timestamp)

    stopping = None
    try:
        await runtime.start(JobSpec("returned", "tool", 0), owner=_owner(), operation=operation)
        monkeypatch.setattr(runtime, "_persist", delayed_persist)
        stopping = asyncio.create_task(runtime.cancel("returned", owner=_owner(), depth=0, await_completion=True))
        await asyncio.wait_for(saving.wait(), 2)
        finish.set()
        await asyncio.wait_for(returned.wait(), 2)
        release.set()
        settled = await asyncio.wait_for(stopping, 2)
        assert settled.status == "completed"
        assert settled.result == "exact result"
        snapshot = json.loads((tmp_path / "tool_jobs" / "returned.json").read_text())
        assert snapshot["status"] == "completed"
        assert snapshot["result"] == "exact result"
    finally:
        finish.set()
        release.set()
        if stopping is not None:
            await asyncio.gather(stopping, return_exceptions=True)
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("first_completed", [False, True])
async def test_shutdown_save_failure_still_drains_every_job_and_releases_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    first_completed: bool,
) -> None:
    """A failed snapshot write remains visible without abandoning other work or blocking restart."""
    runtime = ToolJobRuntime(tmp_path)
    started, stopped = asyncio.Event(), asyncio.Event()
    cleaned: list[str] = []

    async def completed() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "saved")

    async def running() -> BackgroundOutcome:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
        raise AssertionError

    async def cleanup(job: runtime_module.BackgroundJob) -> None:
        cleaned.append(job.job_id)

    await runtime.start(
        JobSpec("first", "tool", 0),
        owner=_owner(),
        operation=completed if first_completed else running,
        cancel=cleanup,
    )
    if first_completed:
        waited = await runtime.wait("first", owner=_owner(), depth=0)
        await runtime.release_wait("first", waited.token)
    await runtime.start(JobSpec("second", "tool", 0), owner=_owner(), operation=running, cancel=cleanup)
    await started.wait()
    writer = runtime_module.write_json_file_durable

    def fail_first_save(path: Path, payload: object) -> None:
        if path.stem == "first":
            msg = "injected shutdown save failure"
            raise OSError(msg)
        writer(path, payload)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(runtime_module, "write_json_file_durable", fail_first_save)
            with pytest.raises((OSError, ExceptionGroup), match="shutdown") as failure:
                await runtime.shutdown()
        assert stopped.is_set()
        assert cleaned == (["second"] if first_completed else ["first", "second"])
        snapshot = json.loads((tmp_path / "tool_jobs" / "second.json").read_text())
        assert snapshot["status"] == "interrupted"
        restored = ToolJobRuntime(tmp_path)
        try:
            await restored.recover()
            assert (await restored.lookup("second", owner=_owner(), depth=0)).status == "interrupted"
        finally:
            await restored.shutdown()
        errors = failure.value.exceptions if isinstance(failure.value, ExceptionGroup) else (failure.value,)
        assert len(errors) == 1
        assert isinstance(errors[0], OSError)
    finally:
        await asyncio.gather(runtime.shutdown(), return_exceptions=True)
