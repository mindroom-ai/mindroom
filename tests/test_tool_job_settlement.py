"""Returned outcomes and shutdown cleanup survive cancellation and storage failures."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import pytest
from structlog.testing import capture_logs

from mindroom.config.main import Config
from mindroom.orchestrator import _MultiAgentOrchestrator
from mindroom.tool_jobs import runtime as runtime_module
from mindroom.tool_jobs.resources import current_execution_resources
from mindroom.tool_jobs.runtime import BackgroundOutcome, JobSpec, ToolJobRuntime
from tests.bot_helpers import _runtime_bound_config
from tests.conftest import runtime_paths_for
from tests.test_background_subagents import _owner

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
async def test_shutdown_save_failure_does_not_abandon_orchestrator_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A job write failure must remain visible while later services and the shared journal close."""
    config = _runtime_bound_config(Config(), tmp_path)
    orchestrator = _MultiAgentOrchestrator(runtime_paths=runtime_paths_for(config))
    orchestrator.config = config
    orchestrator._shared_journal_store()
    runtime = ToolJobRuntime(orchestrator.storage_path)
    orchestrator._tool_job_runtime._runtime = runtime

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "retained result")

    def fail_save(_path: Path, _payload: dict[str, object]) -> None:
        msg = "job storage unavailable"
        raise OSError(msg)

    try:
        await runtime.start(JobSpec("shutdown", "tool", 0), owner=_owner(), operation=operation)
        await runtime.wait("shutdown", owner=_owner(), depth=0)
        with monkeypatch.context() as patch, capture_logs() as logs:
            patch.setattr(runtime_module, "write_json_file_durable", fail_save)
            await orchestrator.stop()
        assert orchestrator._open_journal is None
        assert any(entry["event"] == "Background tool job runtime shutdown failed" for entry in logs)
        assert any(entry["event"] == "All agent bots stopped" for entry in logs)
    finally:
        await orchestrator.stop()


@pytest.mark.asyncio
async def test_outcome_write_failure_preserves_returned_value_until_storage_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed snapshot write cannot replace a completed side effect's output with the storage error."""
    runtime = ToolJobRuntime(tmp_path)
    release = asyncio.Event()
    writer = runtime_module.write_json_file_durable

    async def operation() -> BackgroundOutcome:
        await release.wait()
        (tmp_path / "effect.txt").write_text("once")
        return BackgroundOutcome("completed", "retained output", result_payload={"artifact": [1, 2]})

    def fail_outcome(path: Path, payload: dict[str, object]) -> None:
        if payload["status"] == "completed":
            msg = "outcome storage unavailable"
            raise OSError(msg)
        writer(path, payload)

    try:
        await runtime.start(JobSpec("write-failure", "tool", 0), owner=_owner(), operation=operation)
        with monkeypatch.context() as patch:
            patch.setattr(runtime_module, "write_json_file_durable", fail_outcome)
            release.set()
            waited = await asyncio.wait_for(runtime.wait("write-failure", owner=_owner(), depth=0), 2)
            assert waited.job.status == "completed"
            assert waited.job.result == "retained output"
            assert waited.job.result_payload == {"artifact": [1, 2]}
            assert (tmp_path / "effect.txt").read_text() == "once"
        await runtime.acknowledge_wait("write-failure", waited.token)
        await runtime.shutdown()
        restored = ToolJobRuntime(tmp_path)
        try:
            await restored.recover()
            saved = await restored.lookup("write-failure", owner=_owner(), depth=0)
            assert saved.status == "completed"
            assert saved.result == "retained output"
            assert saved.result_payload == {"artifact": [1, 2]}
            assert saved.wait_acknowledged
        finally:
            await restored.shutdown()
    finally:
        release.set()
        await runtime.shutdown()


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
