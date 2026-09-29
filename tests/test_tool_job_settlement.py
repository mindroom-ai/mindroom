"""Returned outcomes and shutdown cleanup survive cancellation and storage failures."""

from __future__ import annotations

import asyncio
import json
from contextlib import nullcontext
from typing import TYPE_CHECKING

import pytest
from structlog.testing import capture_logs

from mindroom.config.main import Config
from mindroom.orchestrator import _MultiAgentOrchestrator
from mindroom.tool_jobs import runtime as runtime_module
from mindroom.tool_jobs.resources import current_execution_resources
from mindroom.tool_jobs.runtime import BackgroundOutcome, JobRecoveryBlockedError
from tests.bot_helpers import _runtime_bound_config
from tests.conftest import runtime_paths_for
from tests.tool_job_helpers import job_owner, start_job, tool_job_runtime

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
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
    runtime = tool_job_runtime(orchestrator.storage_path)
    orchestrator._tool_job_runtime._runtime = runtime
    started = asyncio.Event()

    async def operation() -> BackgroundOutcome:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError

    def fail_save(_path: Path, _payload: dict[str, object], *, strict_atomic_replace: bool = False) -> None:
        assert strict_atomic_replace
        msg = "job storage unavailable"
        raise OSError(msg)

    try:
        await start_job(
            runtime,
            "shutdown",
            tool_name="tool",
            depth=0,
            adapter={},
            owner=job_owner(),
            operation=operation,
        )
        await asyncio.wait_for(started.wait(), 10)
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
    runtime = tool_job_runtime(tmp_path)
    release = asyncio.Event()
    writer = runtime_module.write_json_file_durable

    async def operation() -> BackgroundOutcome:
        await release.wait()
        with (tmp_path / "effect.txt").open("a") as effect:
            effect.write("once")
        return BackgroundOutcome("completed", "retained output", result_payload={"artifact": [1, 2]})

    def fail_outcome(path: Path, payload: dict[str, object], *, strict_atomic_replace: bool = False) -> None:
        # Both the payload file and the metadata that references it fail to save.
        if path.name.endswith(".result.json") or payload["status"] == "completed":
            msg = "outcome storage unavailable"
            raise OSError(msg)
        writer(path, payload, strict_atomic_replace=strict_atomic_replace)

    try:
        await start_job(
            runtime,
            "write-failure",
            tool_name="tool",
            depth=0,
            adapter={},
            owner=job_owner(),
            operation=operation,
        )
        with monkeypatch.context() as patch:
            patch.setattr(runtime_module, "write_json_file_durable", fail_outcome)
            release.set()
            waited = await asyncio.wait_for(runtime.wait("write-failure", owner=job_owner(), depth=0), 2)
            assert waited.job.status == "completed"
            assert waited.job.result == "retained output"
            assert await runtime.read_payload(waited.job) == {"artifact": [1, 2]}
            assert not (tmp_path / "tool_jobs" / "write-failure.g0.result.json").exists()
            assert (tmp_path / "effect.txt").read_text() == "once"
        await runtime.acknowledge_wait("write-failure", waited.claim)
        await runtime.shutdown()
        restored = tool_job_runtime(tmp_path)
        try:
            await restored.recover()
            saved = await restored.lookup("write-failure", owner=job_owner(), depth=0)
            assert saved.status == "completed"
            assert saved.result == "retained output"
            assert await restored.read_payload(saved) == {"artifact": [1, 2]}
            assert saved.consumed
        finally:
            await restored.shutdown()
    finally:
        release.set()
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("shutdown", [False, True])
async def test_returned_result_survives_stop_during_resource_cleanup(tmp_path: Path, shutdown: bool) -> None:
    """A completed side effect retains its output, but only after its resource cleanup drains."""
    runtime = tool_job_runtime(tmp_path)
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
        with (tmp_path / "effect.txt").open("a") as effect:
            effect.write("completed once")
        return BackgroundOutcome("completed", "exact result", result_payload={"retained": [1, 2]})

    stopping = None
    try:
        await start_job(
            runtime,
            "returned",
            tool_name="tool",
            depth=0,
            adapter={},
            owner=job_owner(),
            operation=operation,
        )
        await asyncio.wait_for(cleaning.wait(), 2)
        stopping = asyncio.create_task(
            runtime.shutdown() if shutdown else runtime.cancel("returned", owner=job_owner(), depth=0),
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
        assert json.loads((tmp_path / "tool_jobs" / "returned.g0.result.json").read_text()) == {"retained": [1, 2]}
    finally:
        release.set()
        if stopping is not None:
            await asyncio.gather(stopping, return_exceptions=True)
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_returned_result_survives_cancel_admission_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Cancellation cannot overwrite output returned while its durable admission owns the lock."""
    runtime = tool_job_runtime(tmp_path)
    finish, returned, saving, release = (asyncio.Event() for _ in range(4))
    publish = runtime._publish

    async def operation() -> BackgroundOutcome:
        await finish.wait()
        with (tmp_path / "effect.txt").open("a") as effect:
            effect.write("completed once")
        returned.set()
        return BackgroundOutcome("completed", "exact result")

    async def delayed_publish(
        entry: runtime_module._Entry,
        job: runtime_module.BackgroundJob,
        payload: runtime_module.EncodedResultPayload | None = None,
    ) -> None:
        if job.status == "cancel_requested":
            saving.set()
            await release.wait()
        await publish(entry, job, payload)

    stopping = None
    try:
        await start_job(
            runtime,
            "returned",
            tool_name="tool",
            depth=0,
            adapter={},
            owner=job_owner(),
            operation=operation,
        )
        monkeypatch.setattr(runtime, "_publish", delayed_publish)
        stopping = asyncio.create_task(runtime.cancel("returned", owner=job_owner(), depth=0))
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
        assert (tmp_path / "effect.txt").read_text() == "completed once"
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
    runtime = tool_job_runtime(tmp_path)
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

    await start_job(
        runtime,
        "first",
        tool_name="tool",
        depth=0,
        adapter={},
        owner=job_owner(),
        operation=completed if first_completed else running,
        cancel=cleanup,
    )
    if first_completed:
        waited = await runtime.wait("first", owner=job_owner(), depth=0)
        await runtime.release_wait("first", waited.claim)
    await start_job(
        runtime,
        "second",
        tool_name="tool",
        depth=0,
        adapter={},
        owner=job_owner(),
        operation=running,
        cancel=cleanup,
    )
    await started.wait()
    writer = runtime_module.write_json_file_durable

    def fail_first_save(path: Path, payload: object, *, strict_atomic_replace: bool = False) -> None:
        if path.stem == "first":
            msg = "injected shutdown save failure"
            raise OSError(msg)
        writer(path, payload, strict_atomic_replace=strict_atomic_replace)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(runtime_module, "write_json_file_durable", fail_first_save)
            expectation = nullcontext() if first_completed else pytest.raises(ExceptionGroup, match="shutdown")
            with expectation as failure:
                await runtime.shutdown()
        assert stopped.is_set()
        assert cleaned == (["second"] if first_completed else ["first", "second"])
        snapshot = json.loads((tmp_path / "tool_jobs" / "second.json").read_text())
        assert snapshot["status"] == "interrupted"
        restored = tool_job_runtime(tmp_path)
        try:
            await restored.recover()
            assert (await restored.lookup("second", owner=job_owner(), depth=0)).status == "interrupted"
        finally:
            await restored.shutdown()
        if failure is not None:
            assert len(failure.value.exceptions) == 1
            assert isinstance(failure.value.exceptions[0], OSError)
    finally:
        await asyncio.gather(runtime.shutdown(), return_exceptions=True)


@pytest.mark.asyncio
async def test_blocked_shutdown_cleanup_still_settles_other_jobs(tmp_path: Path) -> None:
    """A child still owned elsewhere stays unsettled without stranding later jobs, and shutdown reports it."""
    started: set[str] = set()
    both_started = asyncio.Event()

    async def cleanup(job: runtime_module.BackgroundJob) -> None:
        if job.job_id == "blocked":
            msg = "Native child is still executing; recovery cannot settle it."
            raise JobRecoveryBlockedError(msg)

    runtime = tool_job_runtime(tmp_path, cancel=cleanup)

    def running(name: str) -> Callable[[], Awaitable[BackgroundOutcome]]:
        async def operation() -> BackgroundOutcome:
            started.add(name)
            if started == {"blocked", "later"}:
                both_started.set()
            await asyncio.Event().wait()
            raise AssertionError

        return operation

    for name in ("blocked", "later"):
        await start_job(
            runtime,
            name,
            tool_name="tool",
            depth=0,
            adapter={},
            owner=job_owner(),
            operation=running(name),
        )
    await both_started.wait()
    with pytest.raises(ExceptionGroup, match="shutdown") as failure:
        await runtime.shutdown()
    assert [type(error) for error in failure.value.exceptions] == [JobRecoveryBlockedError]
    saved = {
        name: runtime_module.read_job_snapshot(tmp_path / "tool_jobs" / f"{name}.json").status
        for name in ("blocked", "later")
    }
    assert saved == {"blocked": "running", "later": "interrupted"}
