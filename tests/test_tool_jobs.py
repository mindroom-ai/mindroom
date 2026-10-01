"""Generic job ownership, scoped discovery, admission, and durable consumption."""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import pytest
from structlog.testing import capture_logs

from mindroom.tool_jobs import runtime as runtime_module
from mindroom.tool_jobs.control import HumanMessageSignal, human_message_signal_context, job_checkpoint
from mindroom.tool_jobs.results import ToolResultPayload, encode_result_payload, read_result_payload
from mindroom.tool_jobs.runtime import BackgroundOutcome, ToolJobRuntime
from tests.tool_job_helpers import (
    JOB_TEST_TIMEOUT,
    backdate_job,
    job_owner,
    start_job,
    tool_job_runtime,
    wait_for_status,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable


@pytest.mark.asyncio
async def test_wait_rejects_unrepresentable_timeout_before_lookup(tmp_path: Path) -> None:
    """Both entry points reject budgets that cannot be represented on the event-loop clock."""
    runtime = tool_job_runtime(tmp_path)
    try:
        with pytest.raises(ValueError, match="finite"):
            await runtime.wait("unknown", owner=job_owner(), depth=0, timeout=10**400)
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_final_shutdown_waits_for_receipt_publication(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A replacement owner must never recover before the old owner's last receipt lands."""
    runtime = tool_job_runtime(tmp_path)

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "saved answer")

    await start_job(runtime, "receipt", tool_name="tool", depth=0, adapter={}, owner=job_owner(), operation=operation)
    waited = await runtime.wait("receipt", owner=job_owner(), depth=0)
    await runtime.quiesce()
    original_writer = runtime_module.write_json_file_durable
    writing, closing = asyncio.Event(), asyncio.Event()
    release_writer = threading.Event()
    loop = asyncio.get_running_loop()

    def blocked_writer(path: Path, payload: object, *, strict_atomic_replace: bool) -> None:
        loop.call_soon_threadsafe(writing.set)
        assert release_writer.wait(30)
        original_writer(path, payload, strict_atomic_replace=strict_atomic_replace)

    async def close() -> None:
        closing.set()
        await runtime.shutdown()

    monkeypatch.setattr(runtime_module, "write_json_file_durable", blocked_writer)
    acknowledging = asyncio.create_task(runtime.acknowledge_wait("receipt", waited.claim))
    await asyncio.wait_for(writing.wait(), 30)
    shutdown = asyncio.create_task(close())
    try:
        await closing.wait()
        with pytest.raises(BlockingIOError):
            tool_job_runtime(tmp_path)
        assert not shutdown.done()
    finally:
        release_writer.set()
        await asyncio.gather(acknowledging, shutdown)
    restored = tool_job_runtime(tmp_path)
    try:
        await restored.recover()
        assert await restored.pending_outcomes() == []
    finally:
        await restored.shutdown()


@pytest.mark.asyncio
async def test_quiescence_fences_control_but_retains_receipts_and_storage(tmp_path: Path) -> None:
    """No new execution or cleanup races a shutdown drain; finalizers may still acknowledge."""
    runtime = tool_job_runtime(tmp_path)

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome("awaiting_approval", "approval required")

    try:
        job = await start_job(
            runtime,
            "paused",
            tool_name="tool",
            depth=0,
            adapter={},
            owner=job_owner(),
            operation=operation,
        )
        waited = await runtime.wait(job.job_id, owner=job_owner(), depth=0)
        await runtime.quiesce()
        with pytest.raises(BlockingIOError):
            tool_job_runtime(tmp_path)
        with pytest.raises(runtime_module.JobAccessError, match="shutting down"):
            await start_job(
                runtime,
                "new",
                tool_name="tool",
                depth=0,
                adapter={},
                owner=job_owner(),
                operation=operation,
            )
        with pytest.raises(runtime_module.JobAccessError, match="shutting down"):
            await runtime.continue_job(
                "paused",
                owner=job_owner(),
                depth=0,
                expected_generation=0,
                operation=operation,
                adapter={},
            )
        with pytest.raises(runtime_module.JobAccessError, match="shutting down"):
            await runtime.cancel("paused", owner=job_owner(), depth=0)
        await runtime.acknowledge_wait(job.job_id, waited.claim)
        assert (await runtime.lookup(job.job_id, owner=job_owner(), depth=0)).consumed
    finally:
        await runtime.shutdown()
    restored = tool_job_runtime(tmp_path)
    try:
        await restored.recover()
        assert await restored.pending_outcomes() == []
        assert (await restored.lookup(job.job_id, owner=job_owner(), depth=0)).status == "awaiting_approval"
    finally:
        await restored.shutdown()


@pytest.mark.asyncio
async def test_saved_outcome_keeps_its_payload_only_on_disk(tmp_path: Path) -> None:
    """Job metadata stays in memory, while the payload lives in its generation's file and is read on demand."""
    runtime = tool_job_runtime(tmp_path)
    value = "summary " * 100 + "payload tail"
    payload = encode_result_payload(ToolResultPayload(value))

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome("completed", value, result_payload=payload)

    try:
        await start_job(runtime, "disk", tool_name="tool", depth=0, adapter={}, owner=job_owner(), operation=operation)
        waited = await runtime.wait("disk", owner=job_owner(), depth=0)
        assert json.loads((tmp_path / "tool_jobs" / "disk.g0.result.json").read_text()) == payload
        assert "payload tail" not in (tmp_path / "tool_jobs" / "disk.json").read_text()
        assert "payload tail" not in repr(runtime._entries["disk"])
        assert (await read_result_payload(runtime, waited.job)).value == value
        await runtime.acknowledge_wait("disk", waited.claim)
        assert "payload tail" not in repr(runtime._entries["disk"])
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["acknowledge", "stop", "cancel"])
async def test_settled_payload_file_is_written_once(tmp_path: Path, action: str) -> None:
    """Acknowledgement, Stop, cancelling settled work, and restart recovery rewrite only job metadata."""
    runtime = tool_job_runtime(tmp_path)
    payload = encode_result_payload(ToolResultPayload("saved"))

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "saved", result_payload=payload)

    async def every_job(_job: runtime_module.BackgroundJob) -> bool:
        return True

    payload_path = tmp_path / "tool_jobs" / "once.g0.result.json"
    try:
        await start_job(runtime, "once", tool_name="tool", depth=0, adapter={}, owner=job_owner(), operation=operation)
        waited = await runtime.wait("once", owner=job_owner(), depth=0)
        written = payload_path.stat()
        if action == "acknowledge":
            await runtime.acknowledge_wait("once", waited.claim)
        else:
            await runtime.release_wait("once", waited.claim)
            if action == "stop":
                await runtime.stop_jobs(receipt_order=1, matches=every_job)
            else:
                await runtime.cancel("once", owner=job_owner(), depth=0)
    finally:
        await runtime.shutdown()
    restored = tool_job_runtime(tmp_path)
    try:
        await restored.recover()
        job = await restored.lookup("once", owner=job_owner(), depth=0)
        assert job.consumed is (action == "acknowledge")
        assert (job.user_stop_receipt_order is not None) is (action == "stop")
        assert (await read_result_payload(restored, job)).value == "saved"
    finally:
        await restored.shutdown()
    assert (payload_path.stat().st_ino, payload_path.stat().st_mtime_ns) == (written.st_ino, written.st_mtime_ns)


@pytest.mark.asyncio
async def test_waiter_that_missed_a_pause_claims_the_next_generation_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A continuation queued before the waiter sees the pause cannot leave the waiter a stale claim and deliver twice."""
    runtime = tool_job_runtime(tmp_path)
    loop = asyncio.get_running_loop()
    writer = runtime_module.write_json_file_durable
    pause_writing = asyncio.Event()
    release_pause = threading.Event()

    def blocked_pause(path: Path, payload: object, *, strict_atomic_replace: bool) -> None:
        if isinstance(payload, dict) and payload.get("status") == "awaiting_approval":
            loop.call_soon_threadsafe(pause_writing.set)
            assert release_pause.wait(30)
        writer(path, payload, strict_atomic_replace=strict_atomic_replace)

    async def paused() -> BackgroundOutcome:
        return BackgroundOutcome("awaiting_approval", "paused")

    async def resumed() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "resumed")

    async def queued[T](started: asyncio.Event, operation: Awaitable[T]) -> T:
        # Nothing yields between setting the event and queueing on the runtime lock.
        started.set()
        return await operation

    monkeypatch.setattr(runtime_module, "write_json_file_durable", blocked_pause)
    try:
        job, claim = await runtime.start(
            "missed",
            tool_name="tool",
            depth=0,
            adapter={},
            owner=job_owner(),
            operation=paused,
        )
        # The paused outcome holds the runtime lock while its save blocks.
        await asyncio.wait_for(pause_writing.wait(), 30)
        waiter_queued, continuation_queued = asyncio.Event(), asyncio.Event()
        waiter = asyncio.create_task(
            queued(waiter_queued, runtime.wait(job.job_id, owner=job_owner(), depth=0, claim=claim)),
        )
        await waiter_queued.wait()
        continuation = asyncio.create_task(
            queued(
                continuation_queued,
                runtime.continue_job(
                    job.job_id,
                    owner=job_owner(),
                    depth=0,
                    expected_generation=0,
                    operation=resumed,
                    adapter={},
                ),
            ),
        )
        await continuation_queued.wait()
        # The lock is FIFO: the waiter enters first, then queues behind the continuation and misses the pause.
        release_pause.set()
        await continuation
        waited = await asyncio.wait_for(waiter, 30)
        assert waited.job.status == "completed"
        assert waited.job.generation == 1
        assert waited.claim is not None
        assert waited.claim.generation == 1
        assert await runtime.pending_outcomes() == []
        await runtime.acknowledge_wait(job.job_id, waited.claim)
        assert (await runtime.lookup(job.job_id, owner=job_owner(), depth=0)).consumed
        assert await runtime.pending_outcomes() == []
    finally:
        release_pause.set()
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_next_generation_deletes_the_payload_it_replaces(tmp_path: Path) -> None:
    """Publishing a job's next generation deletes the previous generation's payload file."""
    runtime = tool_job_runtime(tmp_path)

    async def paused() -> BackgroundOutcome:
        return BackgroundOutcome("awaiting_approval", "paused", result_payload={"generation": 0})

    async def resumed() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "resumed", result_payload={"generation": 1})

    def payload_files() -> set[str]:
        return {path.name for path in (tmp_path / "tool_jobs").glob("next.*.result.json")}

    try:
        await start_job(runtime, "next", tool_name="tool", depth=0, adapter={}, owner=job_owner(), operation=paused)
        waited = await runtime.wait("next", owner=job_owner(), depth=0)
        await runtime.release_wait("next", waited.claim)
        assert payload_files() == {"next.g0.result.json"}
        await runtime.continue_job(
            "next",
            owner=job_owner(),
            depth=0,
            expected_generation=0,
            operation=resumed,
            adapter={},
        )
        waited = await runtime.wait("next", owner=job_owner(), depth=0)
        assert await runtime.read_payload(waited.job) == {"generation": 1}
        assert payload_files() == {"next.g1.result.json"}
        await runtime.release_wait("next", waited.claim)
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_crash_between_payload_and_metadata_save_recovers_interrupted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Saved metadata still says running, so recovery interrupts the call and deletes its orphaned payload."""
    runtime = tool_job_runtime(tmp_path)
    writer = runtime_module.write_json_file_durable
    executions = 0

    def die_before_metadata(path: Path, payload: dict[str, object], *, strict_atomic_replace: bool) -> None:
        if path.name == "crash.json" and payload["status"] == "completed":
            msg = "process died"
            raise OSError(msg)
        writer(path, payload, strict_atomic_replace=strict_atomic_replace)

    async def operation() -> BackgroundOutcome:
        nonlocal executions
        executions += 1
        return BackgroundOutcome("completed", "lost", result_payload=encode_result_payload(ToolResultPayload("lost")))

    orphan = tmp_path / "tool_jobs" / "crash.g0.result.json"
    with monkeypatch.context() as patch:
        patch.setattr(runtime_module, "write_json_file_durable", die_before_metadata)
        await start_job(runtime, "crash", tool_name="tool", depth=0, adapter={}, owner=job_owner(), operation=operation)
        await wait_for_status(runtime, "crash", "completed")
    assert orphan.exists()
    assert runtime_module.read_job_snapshot(tmp_path / "tool_jobs" / "crash.json").status == "running"
    # The process dies: its storage lease goes away without an orderly shutdown.
    runtime._lease.close()
    restored = tool_job_runtime(tmp_path)
    try:
        await restored.recover()
        recovered = await restored.lookup("crash", owner=job_owner(), depth=0)
        assert recovered.status == "interrupted"
        assert not orphan.exists()
        assert executions == 1
    finally:
        await restored.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("acknowledged", [False, True])
async def test_restart_reattaches_a_saved_result_without_rewrites_or_rerun(
    tmp_path: Path,
    *,
    acknowledged: bool,
) -> None:
    """Recovery rewrites no saved file, and reattaching the same call returns its result instead of running it."""
    runtime = tool_job_runtime(tmp_path)
    value = "large result" * 10_000
    payload = encode_result_payload(ToolResultPayload(value))
    calls = 0

    async def operation() -> BackgroundOutcome:
        nonlocal calls
        calls += 1
        return BackgroundOutcome("completed", value, result_payload=payload)

    await start_job(runtime, "consumed", tool_name="tool", depth=0, adapter={}, owner=job_owner(), operation=operation)
    waited = await runtime.wait("consumed", owner=job_owner(), depth=0)
    if acknowledged:
        await runtime.acknowledge_wait("consumed", waited.claim)
    else:
        await runtime.release_wait("consumed", waited.claim)
    await runtime.shutdown()
    saved = {path.name: path.stat().st_mtime_ns for path in (tmp_path / "tool_jobs").glob("consumed.*")}
    assert set(saved) == {"consumed.json", "consumed.g0.result.json"}
    restored = tool_job_runtime(tmp_path)
    try:
        await restored.recover()
        assert {path.name: path.stat().st_mtime_ns for path in (tmp_path / "tool_jobs").glob("consumed.*")} == saved
        assert bool(await restored.pending_outcomes()) is not acknowledged
        _, claim = await restored.start(
            "consumed",
            tool_name="tool",
            depth=0,
            adapter={},
            owner=job_owner(),
            operation=operation,
            reattach=True,
        )
        reread = await restored.wait("consumed", owner=job_owner(), depth=0, claim=claim)
        assert reread.claim == claim
        assert (reread.job.result, reread.job.summary_truncated) == (
            value[: runtime_module._JOB_SUMMARY_MAX_CHARS],
            True,
        )
        assert (await read_result_payload(restored, reread.job)).value == value
        assert calls == 1
        await restored.acknowledge_wait("consumed", reread.claim)
    finally:
        await restored.shutdown()


@pytest.mark.asyncio
async def test_shutdown_drains_a_terminal_cancellation_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Shutdown must retain its storage lease until an accepted terminal retry has finished writing."""
    runtime = tool_job_runtime(tmp_path)
    writer = runtime_module.write_json_file_durable

    async def operation() -> BackgroundOutcome:
        await asyncio.Event().wait()
        raise AssertionError

    def fail_terminal(path: Path, payload: dict[str, object], *, strict_atomic_replace: bool) -> None:
        if payload["status"] == "cancelled":
            message = "terminal save failed"
            raise OSError(message)
        writer(path, payload, strict_atomic_replace=strict_atomic_replace)

    await start_job(runtime, "retry", tool_name="tool", depth=0, adapter={}, owner=job_owner(), operation=operation)
    with monkeypatch.context() as patch:
        patch.setattr(runtime_module, "write_json_file_durable", fail_terminal)
        with pytest.raises(OSError, match="terminal save failed"):
            await runtime.cancel("retry", owner=job_owner(), depth=0)

    snapshot_started, release_snapshot = asyncio.Event(), asyncio.Event()
    retry_started, release_retry = asyncio.Event(), asyncio.Event()
    original_snapshot, original_publish = runtime._snapshot, runtime._publish

    async def snapshot(entry: runtime_module._Entry, *, include_result: bool = True) -> runtime_module.BackgroundJob:
        if not snapshot_started.is_set():
            snapshot_started.set()
            await release_snapshot.wait()
        return await original_snapshot(entry, include_result=include_result)

    async def publish(
        entry: runtime_module._Entry,
        job: runtime_module.BackgroundJob,
        payload: runtime_module.EncodedResultPayload | None = None,
    ) -> None:
        if not retry_started.is_set():
            retry_started.set()
            await release_retry.wait()
        await original_publish(entry, job, payload)

    monkeypatch.setattr(runtime, "_snapshot", snapshot)
    monkeypatch.setattr(runtime, "_publish", publish)
    retrying = asyncio.create_task(runtime.cancel_owned("retry", matches=lambda job: job.job_id == "retry"))
    await snapshot_started.wait()
    stopping = asyncio.create_task(runtime.shutdown())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    release_snapshot.set()
    try:
        await retry_started.wait()
        done, _pending = await asyncio.wait({stopping}, timeout=0.2)
        assert not done, "shutdown released storage while a terminal cancellation retry was still running"
    finally:
        release_retry.set()
        await asyncio.gather(retrying, stopping)
    saved = runtime_module.read_job_snapshot(tmp_path / "tool_jobs" / "retry.json")
    assert saved.status == "cancelled"


@pytest.mark.asyncio
async def test_failed_cancellation_save_wakes_an_existing_waiter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A terminal outcome must wake its claim owner even when the durable write fails."""
    runtime = tool_job_runtime(tmp_path)
    cleaning, release = asyncio.Event(), asyncio.Event()
    writer = runtime_module.write_json_file_durable

    async def operation() -> BackgroundOutcome:
        await asyncio.Event().wait()
        raise AssertionError

    async def cleanup(_job: runtime_module.BackgroundJob) -> None:
        cleaning.set()
        await release.wait()

    def fail_terminal(path: Path, payload: dict[str, object], *, strict_atomic_replace: bool) -> None:
        if payload["status"] == "cancelled":
            message = "terminal save failed"
            raise OSError(message)
        writer(path, payload, strict_atomic_replace=strict_atomic_replace)

    await start_job(
        runtime,
        "wake",
        tool_name="tool",
        depth=0,
        adapter={},
        owner=job_owner(),
        operation=operation,
        cancel=cleanup,
    )
    waiter = asyncio.create_task(runtime.wait("wake", owner=job_owner(), depth=0))
    try:
        with monkeypatch.context() as patch:
            patch.setattr(runtime_module, "write_json_file_durable", fail_terminal)
            cancelling = asyncio.create_task(runtime.cancel("wake", owner=job_owner(), depth=0))
            await cleaning.wait()
            await asyncio.sleep(0)
            assert not waiter.done()
            release.set()
            with pytest.raises(OSError, match="terminal save failed"):
                await cancelling
            done, _pending = await asyncio.wait({waiter}, timeout=0.2)
            assert waiter in done, "terminal write failure left the existing waiter asleep"
            waited = waiter.result()
            assert waited.job.status == "cancelled"
        await runtime.acknowledge_wait("wake", waited.claim)
        saved = runtime_module.read_job_snapshot(tmp_path / "tool_jobs" / "wake.json")
        assert saved.consumed
        assert saved.status == "cancelled"
    finally:
        release.set()
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_consumption_repairs_failed_cancellation_save_before_reread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acknowledgement can repair a terminal save without leaving a stale cancellation retry."""
    runtime = tool_job_runtime(tmp_path)
    writer = runtime_module.write_json_file_durable

    async def operation() -> BackgroundOutcome:
        await asyncio.Event().wait()
        raise AssertionError

    def fail_terminal(path: Path, payload: dict[str, object], *, strict_atomic_replace: bool) -> None:
        if payload["status"] == "cancelled":
            message = "terminal save failed"
            raise OSError(message)
        writer(path, payload, strict_atomic_replace=strict_atomic_replace)

    try:
        await start_job(
            runtime,
            "cancel-retry",
            tool_name="tool",
            depth=0,
            adapter={},
            owner=job_owner(),
            operation=operation,
        )
        with monkeypatch.context() as patch:
            patch.setattr(runtime_module, "write_json_file_durable", fail_terminal)
            with pytest.raises(OSError, match="terminal save failed"):
                await runtime.cancel("cancel-retry", owner=job_owner(), depth=0)
        waited = await runtime.wait("cancel-retry", owner=job_owner(), depth=0)
        await runtime.acknowledge_wait("cancel-retry", waited.claim)
        result = await runtime.cancel("cancel-retry", owner=job_owner(), depth=0)
        assert result.status == "cancelled"
        assert result.consumed
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_expiry_rechecks_a_read_completed_during_source_lookup(tmp_path: Path) -> None:
    """A concurrent reader renews retention before a previously eligible candidate is deleted."""
    runtime = tool_job_runtime(tmp_path)

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "saved")

    async def source_finished(job: runtime_module.BackgroundJob) -> bool:
        waited = await runtime.wait(job.job_id, owner=job_owner(), depth=0)
        await runtime.acknowledge_wait(job.job_id, waited.claim)
        return True

    try:
        await start_job(
            runtime,
            "reread",
            tool_name="tool",
            depth=0,
            adapter={},
            owner=job_owner(),
            operation=operation,
        )
        waited = await runtime.wait("reread", owner=job_owner(), depth=0)
        await runtime.acknowledge_wait("reread", waited.claim)
        cutoff = datetime.now(UTC) - timedelta(days=30)
        backdate_job(runtime, "reread", cutoff - timedelta(seconds=1))
        await runtime.expire_consumed(before=cutoff, source_finished=source_finished)
        assert (await runtime.lookup("reread", owner=job_owner(), depth=0)).result == "saved"
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_result_expiry_deletes_only_old_consumed_jobs_with_finished_sources(tmp_path: Path) -> None:
    """Expiry deletes an old consumed job's files and entry; approval-owned, unread, recent, and claimed work stays."""
    runtime = tool_job_runtime(tmp_path)
    payload = encode_result_payload(ToolResultPayload("saved"))

    async def completed() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "saved", result_payload=payload)

    async def source_finished(job: runtime_module.BackgroundJob) -> bool:
        return job.job_id != "approval"

    cutoff = datetime.now(UTC) - timedelta(days=30)
    directory = tmp_path / "tool_jobs"
    kept = ("approval", "unread", "recent", "claimed")
    claimed = None
    try:
        for name in ("expire", *kept):
            await start_job(
                runtime,
                name,
                tool_name="tool",
                depth=0,
                adapter={},
                owner=job_owner(),
                operation=completed,
            )
            waited = await runtime.wait(name, owner=job_owner(), depth=0)
            if name != "unread":
                await runtime.acknowledge_wait(name, waited.claim)
            else:
                await runtime.release_wait(name, waited.claim)
            if name != "recent":
                backdate_job(runtime, name, cutoff - timedelta(seconds=1))
        claimed = await runtime.wait("claimed", owner=job_owner(), depth=0)
        await runtime.expire_consumed(before=cutoff, source_finished=source_finished)
        assert "expire" not in runtime._entries
        assert not list(directory.glob("expire.*"))
        with pytest.raises(runtime_module.JobAccessError, match="not available"):
            await runtime.lookup("expire", owner=job_owner(), depth=0)
        for name in kept:
            assert await runtime.read_payload(await runtime.lookup(name, owner=job_owner(), depth=0)) == payload
        assert [job.job_id for job in await runtime.pending_outcomes()] == ["unread"]
    finally:
        if claimed is not None:
            await runtime.release_wait("claimed", claimed.claim)
        await runtime.shutdown()
    restored = tool_job_runtime(tmp_path)
    try:
        await restored.recover()
        assert set(restored._entries) == set(kept)
        assert {path.name for path in directory.glob("*.result.json")} == {f"{name}.g0.result.json" for name in kept}
    finally:
        await restored.shutdown()


@pytest.mark.asyncio
async def test_source_and_conversation_lookups_follow_admission_recovery_and_expiry(tmp_path: Path) -> None:
    """Per-turn lookups find exactly one source's or conversation's jobs in admission order, and forget expired ones."""
    owner = job_owner()
    jobs = {
        "a": (owner, "$turn"),
        "b": (owner, "$turn"),
        "c": (owner, "$later-turn"),
        "d": (replace(owner, requester_id="@bob:test"), "$turn"),
        "unsourced": (owner, None),
    }
    source = {"transport_agent_name": "parent", "room_id": "!room:test", "thread_id": "$root"}

    async def completed() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "saved")

    async def lookups(runtime: ToolJobRuntime) -> tuple[list[str], list[str]]:
        by_source = await runtime.source_jobs(
            "$turn",
            **source,
            session_id="parent-session",
            requester_id="@alice:test",
        )
        by_conversation = await runtime.conversation_jobs(**source, requester_id="@alice:test")
        return [job.job_id for job in by_source], [job.job_id for job in by_conversation]

    runtime = tool_job_runtime(tmp_path)
    try:
        for job_id, (source_owner, source_event_id) in jobs.items():
            await start_job(
                runtime,
                job_id,
                tool_name="tool",
                depth=0,
                source_event_id=source_event_id,
                adapter={},
                owner=source_owner,
                operation=completed,
            )
            waited = await runtime.wait(job_id, owner=source_owner, depth=0)
            await runtime.release_wait(job_id, waited.claim)
        assert await lookups(runtime) == (["a", "b"], ["a", "b", "c", "unsourced"])
        waited = await runtime.wait("a", owner=owner, depth=0)
        await runtime.acknowledge_wait("a", waited.claim)
        backdate_job(runtime, "a", datetime.now(UTC) - timedelta(days=31))
        # The conversation lookup skips consumed jobs, so only the index itself shows that expiry removed one.
        conversation = ("parent", "!room:test", "$root", "@alice:test")
        assert [entry.job.job_id for entry in runtime._by_conversation.get(conversation)] == [
            "a",
            "b",
            "c",
            "unsourced",
        ]

        async def source_finished(_job: runtime_module.BackgroundJob) -> bool:
            return True

        await runtime.expire_consumed(before=datetime.now(UTC) - timedelta(days=30), source_finished=source_finished)
        assert await lookups(runtime) == (["b"], ["b", "c", "unsourced"])
        assert [entry.job.job_id for entry in runtime._by_conversation.get(conversation)] == ["b", "c", "unsourced"]
    finally:
        await runtime.shutdown()
    restored = tool_job_runtime(tmp_path)
    try:
        await restored.recover()
        assert await lookups(restored) == (["b"], ["b", "c", "unsourced"])
    finally:
        await restored.shutdown()


@pytest.mark.asyncio
async def test_scoped_listing_keeps_consumed_outcomes_across_turns_and_restart(tmp_path: Path) -> None:
    """Discovery survives forgotten handles without exposing another caller or consuming results."""
    runtime = tool_job_runtime(tmp_path)

    async def completed() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "saved")

    async def running() -> BackgroundOutcome:
        await asyncio.Event().wait()
        raise AssertionError

    first = await start_job(
        runtime,
        "first",
        tool_name="search",
        depth=0,
        toolkit_name="web",
        adapter={"run_id": "old"},
        owner=job_owner(),
        operation=completed,
    )
    waiting = await runtime.wait(first.job_id, owner=job_owner(), depth=0)
    await runtime.acknowledge_wait(first.job_id, waiting.claim)
    await start_job(
        runtime,
        "second",
        tool_name="fetch",
        depth=0,
        adapter={"run_id": "new"},
        owner=job_owner(),
        operation=running,
    )
    assert [job.job_id for job in await runtime.list_jobs(owner=job_owner(), depth=0)] == ["second", "first"]
    assert [job.job_id for job in await runtime.list_jobs(owner=job_owner(), depth=0, limit=1, offset=1)] == ["first"]
    for other in (
        replace(job_owner(), requester_id="@other:test"),
        replace(job_owner(), transport_agent_name="other"),
        replace(job_owner(), resolved_thread_id="$other"),
    ):
        assert await runtime.list_jobs(owner=other, depth=0) == []
    assert await runtime.list_jobs(owner=job_owner(), depth=1) == []
    await runtime.shutdown()
    restored = tool_job_runtime(tmp_path)
    try:
        await restored.recover()
        jobs = await restored.list_jobs(owner=job_owner(), depth=0)
        assert {job.job_id for job in jobs} == {"first", "second"}
        assert next(job for job in jobs if job.job_id == "first").result == "saved"
    finally:
        await restored.shutdown()


@pytest.mark.asyncio
async def test_recent_outcome_order_and_timestamps_survive_restart(tmp_path: Path) -> None:
    """Flushing unchanged outcomes must not make filename order look like completion order."""
    runtime = tool_job_runtime(tmp_path)

    async def completed() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "saved")

    for job_id in ("zolder", "anewer"):
        await start_job(runtime, job_id, tool_name="read", depth=0, adapter={}, owner=job_owner(), operation=completed)
        waited = await runtime.wait(job_id, owner=job_owner(), depth=0)
        await runtime.acknowledge_wait(job_id, waited.claim)
    before = await runtime.list_jobs(owner=job_owner(), depth=0)
    assert [job.job_id for job in before] == ["anewer", "zolder"]
    await runtime.shutdown()
    restored = tool_job_runtime(tmp_path)
    try:
        await restored.recover()
        after = await restored.list_jobs(owner=job_owner(), depth=0)
        assert [(job.job_id, job.updated_at) for job in after] == [(job.job_id, job.updated_at) for job in before]
    finally:
        await restored.shutdown()


@pytest.mark.asyncio
async def test_failed_admission_leaves_no_subscription_or_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A write that never publishes a record must leave a safely retryable exact job ID."""
    runtime = tool_job_runtime(tmp_path)
    try:
        signal = HumanMessageSignal()
        original = runtime_module.write_json_file_durable
        calls = 0

        def failed_write(_path: Path, _payload: object, *, strict_atomic_replace: bool) -> None:
            assert strict_atomic_replace
            msg = "disk unavailable"
            raise OSError(msg)

        async def operation() -> BackgroundOutcome:
            nonlocal calls
            calls += 1
            return BackgroundOutcome("completed", "once")

        monkeypatch.setattr(runtime_module, "write_json_file_durable", failed_write)
        with pytest.raises(OSError, match="disk unavailable"), human_message_signal_context(signal):
            await start_job(
                runtime,
                "retry",
                tool_name="tool",
                depth=0,
                adapter={},
                owner=job_owner(),
                operation=operation,
            )
        assert not signal.has_subscribers
        assert await runtime.list_jobs(owner=job_owner(), depth=0) == []
        assert calls == 0
        monkeypatch.setattr(runtime_module, "write_json_file_durable", original)
        job = await start_job(
            runtime,
            "retry",
            tool_name="tool",
            depth=0,
            adapter={},
            owner=job_owner(),
            operation=operation,
        )
        assert (await runtime.wait(job.job_id, owner=job_owner(), depth=0)).job.result == "once"
        assert calls == 1
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", ["cancelled", "completed"])
async def test_stale_wait_receipt_cannot_consume_replacement_generation(tmp_path: Path, replacement: str) -> None:
    """Replacing an approval invalidates its old result lease and pending generation."""
    runtime = tool_job_runtime(tmp_path)

    async def approval() -> BackgroundOutcome:
        return BackgroundOutcome("awaiting_approval")

    async def resumed() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "resumed")

    try:
        job = await start_job(
            runtime,
            "approval",
            tool_name="delegate",
            depth=0,
            adapter={},
            owner=job_owner(),
            operation=approval,
        )
        waiting = await runtime.wait(job.job_id, owner=job_owner(), depth=0)
        if replacement == "cancelled":
            await runtime.cancel(job.job_id, owner=job_owner(), depth=0)
        else:
            await runtime.continue_job(
                job.job_id,
                owner=job_owner(),
                depth=0,
                expected_generation=0,
                operation=resumed,
                adapter={},
            )
            await wait_for_status(runtime, job.job_id, "completed")
        with pytest.raises(ValueError, match="no longer belongs"):
            await runtime.acknowledge_wait(job.job_id, waiting.claim)
        pending = await runtime.pending_outcomes()
        assert len(pending) == 1
        assert pending[0].generation > waiting.job.generation
        assert not pending[0].consumed
        assert await runtime.outcome(job.job_id, waiting.job.generation) is None
        current = await runtime.outcome(job.job_id, pending[0].generation)
        assert current is not None
        assert current.status == replacement
        claimed = await runtime.wait(job.job_id, owner=job_owner(), depth=0)
        assert await runtime.outcome(job.job_id, pending[0].generation) is None
        await runtime.acknowledge_wait(job.job_id, claimed.claim)
        assert await runtime.pending_outcomes() == []
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_human_followup_releases_wait_without_pausing_next_tool(tmp_path: Path) -> None:
    """A human follow-up releases only the waiter while one execution crosses later checkpoints."""
    runtime = tool_job_runtime(tmp_path)
    signal = HumanMessageSignal()
    running, proceed, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = 0

    async def operation() -> BackgroundOutcome:
        nonlocal calls
        calls += 1
        running.set()
        await proceed.wait()
        job_checkpoint()
        finished.set()
        return BackgroundOutcome("completed", "done")

    try:
        with human_message_signal_context(signal):
            job = await start_job(
                runtime,
                "work",
                tool_name="tool",
                depth=0,
                adapter={},
                owner=job_owner(),
                operation=operation,
            )
        await running.wait()
        waiter = asyncio.create_task(runtime.wait(job.job_id, owner=job_owner(), depth=0))
        await asyncio.sleep(0)
        signal.notify()
        signal.clear()
        result = await asyncio.wait_for(waiter, JOB_TEST_TIMEOUT)
        assert result.job.status == "running"
        assert result.claim is None
        proceed.set()
        await asyncio.wait_for(finished.wait(), JOB_TEST_TIMEOUT)
        assert (await runtime.wait(job.job_id, owner=job_owner(), depth=0)).job.result == "done"
        assert calls == 1
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_ambiguous_admission_never_launches_or_accepts_duplicate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A write that lands but reports failure never launches, and restart reports the saved identity interrupted."""
    runtime = tool_job_runtime(tmp_path)
    writer = runtime_module.write_json_file_durable
    calls = 0

    def published_then_failed(path: Path, payload: object, *, strict_atomic_replace: bool) -> None:
        writer(path, payload, strict_atomic_replace=strict_atomic_replace)
        msg = "durability uncertain"
        raise OSError(msg)

    async def operation() -> BackgroundOutcome:
        nonlocal calls
        calls += 1
        return BackgroundOutcome("completed")

    monkeypatch.setattr(runtime_module, "write_json_file_durable", published_then_failed)
    with pytest.raises(OSError, match="uncertain"):
        await start_job(
            runtime,
            "ambiguous",
            tool_name="write",
            depth=0,
            adapter={},
            owner=job_owner(),
            operation=operation,
        )
    monkeypatch.setattr(runtime_module, "write_json_file_durable", writer)
    with pytest.raises(ValueError, match="already exists"):
        await start_job(
            runtime,
            "ambiguous",
            tool_name="write",
            depth=0,
            adapter={},
            owner=job_owner(),
            operation=operation,
        )
    with pytest.raises(runtime_module.JobAccessError):
        await runtime.lookup("ambiguous", owner=job_owner(), depth=0)
    await runtime.shutdown()
    restored = tool_job_runtime(tmp_path)
    try:
        await restored.recover()
        assert (await restored.lookup("ambiguous", owner=job_owner(), depth=0)).status == "interrupted"
        assert calls == 0
    finally:
        await restored.shutdown()


@pytest.mark.asyncio
async def test_failed_continuation_preserves_approval_for_safe_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A continuation never accepted on disk must retain its prior approval generation."""
    runtime = tool_job_runtime(tmp_path)
    try:
        calls = 0

        async def approval() -> BackgroundOutcome:
            return BackgroundOutcome("awaiting_approval", approval_state={"owners": ["shell"]})

        async def continuation() -> BackgroundOutcome:
            nonlocal calls
            calls += 1
            return BackgroundOutcome("completed", "once")

        def failed_write(_path: Path, _payload: object, *, strict_atomic_replace: bool) -> None:
            assert strict_atomic_replace
            msg = "write failed"
            raise OSError(msg)

        job = await start_job(
            runtime,
            "approval",
            tool_name="tool",
            depth=0,
            adapter={},
            owner=job_owner(),
            operation=approval,
        )
        result = await runtime.wait(job.job_id, owner=job_owner(), depth=0)
        await runtime.release_wait(job.job_id, result.claim)
        writer = runtime_module.write_json_file_durable
        monkeypatch.setattr(runtime_module, "write_json_file_durable", failed_write)
        with pytest.raises(OSError, match="write failed"):
            await runtime.continue_job(
                job.job_id,
                owner=job_owner(),
                depth=0,
                expected_generation=0,
                operation=continuation,
                adapter={},
            )
        assert (await runtime.lookup(job.job_id, owner=job_owner(), depth=0)).status == "awaiting_approval"
        monkeypatch.setattr(runtime_module, "write_json_file_durable", writer)
        await runtime.continue_job(
            job.job_id,
            owner=job_owner(),
            depth=0,
            expected_generation=0,
            operation=continuation,
            adapter={},
        )
        assert (await runtime.wait(job.job_id, owner=job_owner(), depth=0)).job.result == "once"
        assert calls == 1
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_cancel_requested_remains_owned_until_operation_finally_finishes(tmp_path: Path) -> None:
    """A durable cancellation request cannot publish completion before real work drains."""
    runtime = tool_job_runtime(tmp_path)
    started, cleaning, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def operation() -> BackgroundOutcome:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await finished.wait()
        raise AssertionError

    job = await start_job(
        runtime,
        "cancel",
        tool_name="blocking",
        depth=0,
        adapter={},
        owner=job_owner(),
        operation=operation,
    )
    await started.wait()
    cancelling = asyncio.create_task(runtime.cancel(job.job_id, owner=job_owner(), depth=0))
    await cleaning.wait()
    assert (await runtime.lookup(job.job_id, owner=job_owner(), depth=0)).status == "cancel_requested"
    assert await runtime.pending_outcomes() == []
    assert not cancelling.done()
    finished.set()
    settled = await cancelling
    assert settled.status == "cancelled"
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_repeated_cancel_joins_the_in_flight_drain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A second cancellation awaits the same drain and reports the settled job, not the request."""
    runtime = tool_job_runtime(tmp_path)
    cleaning, release, joined = asyncio.Event(), asyncio.Event(), asyncio.Event()
    cleanups = 0
    request_cancel = runtime._request_cancel
    drains: list[asyncio.Task[runtime_module.BackgroundJob]] = []

    async def operation() -> BackgroundOutcome:
        await asyncio.Event().wait()
        raise AssertionError

    async def cleanup(_job: runtime_module.BackgroundJob) -> None:
        nonlocal cleanups
        cleanups += 1
        cleaning.set()
        await release.wait()

    async def recorded_request(entry: runtime_module._Entry) -> asyncio.Task[runtime_module.BackgroundJob]:
        drain = await request_cancel(entry)
        drains.append(drain)
        if len(drains) == 2:
            joined.set()
        return drain

    monkeypatch.setattr(runtime, "_request_cancel", recorded_request)
    try:
        await start_job(
            runtime,
            "twice",
            tool_name="tool",
            depth=0,
            adapter={},
            owner=job_owner(),
            operation=operation,
            cancel=cleanup,
        )
        first = asyncio.create_task(runtime.cancel("twice", owner=job_owner(), depth=0))
        await cleaning.wait()
        second = asyncio.create_task(runtime.cancel("twice", owner=job_owner(), depth=0))
        await joined.wait()
        assert drains[1] is drains[0]
        assert not drains[0].done()
        release.set()
        settled = await asyncio.gather(first, second)
        assert [job.status for job in settled] == ["cancelled", "cancelled"]
        assert cleanups == 1
    finally:
        release.set()
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_cancelled_caller_still_settles_its_cancellation_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller cancelled while its request is being saved still stops execution and settles the job."""
    runtime = tool_job_runtime(tmp_path)
    writer = runtime_module.write_json_file_durable
    loop = asyncio.get_running_loop()
    started, writing = asyncio.Event(), asyncio.Event()
    release = threading.Event()

    async def operation() -> BackgroundOutcome:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError

    def blocked_request(path: Path, payload: dict[str, object], *, strict_atomic_replace: bool) -> None:
        if payload["status"] == "cancel_requested":
            loop.call_soon_threadsafe(writing.set)
            assert release.wait(30)
        writer(path, payload, strict_atomic_replace=strict_atomic_replace)

    try:
        await start_job(
            runtime,
            "caller",
            tool_name="tool",
            depth=0,
            adapter={},
            owner=job_owner(),
            operation=operation,
        )
        await started.wait()
        monkeypatch.setattr(runtime_module, "write_json_file_durable", blocked_request)
        cancelling = asyncio.create_task(runtime.cancel("caller", owner=job_owner(), depth=0))
        await writing.wait()
        cancelling.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await cancelling
        await asyncio.wait_for(wait_for_status(runtime, "caller", "cancelled"), 30)
        assert runtime_module.read_job_snapshot(tmp_path / "tool_jobs" / "caller.json").status == "cancelled"
    finally:
        release.set()
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_write", [1, 2], ids=["admission", "terminal"])
async def test_failed_cancellation_persistence_can_be_retried(
    tmp_path: Path,
    failed_write: int,
) -> None:
    """A failed cancellation write cannot poison retry or publish an undurable terminal result."""
    runtime = tool_job_runtime(tmp_path)
    started = asyncio.Event()
    human_signal = HumanMessageSignal()
    cleanup_calls = 0

    async def operation() -> BackgroundOutcome:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError

    async def cleanup(_job: runtime_module.BackgroundJob) -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1

    with human_message_signal_context(human_signal):
        job = await start_job(
            runtime,
            "retry-cancel",
            tool_name="tool",
            depth=0,
            adapter={},
            owner=job_owner(),
            operation=operation,
            cancel=cleanup,
        )
    await started.wait()
    original_publish = runtime._publish
    writes = 0

    async def publish(
        entry: runtime_module._Entry,
        job: runtime_module.BackgroundJob,
        payload: runtime_module.EncodedResultPayload | None = None,
    ) -> None:
        nonlocal writes
        writes += 1
        if writes == failed_write:
            msg = "injected durable write failure"
            raise OSError(msg)
        await original_publish(entry, job, payload)

    runtime._publish = publish
    try:
        with pytest.raises(OSError, match="injected durable write failure"):
            await runtime.cancel(job.job_id, owner=job_owner(), depth=0)
        settled = await runtime.cancel(job.job_id, owner=job_owner(), depth=0)
        saved = json.loads((tmp_path / "tool_jobs" / "retry-cancel.json").read_text())
        assert settled.status == "cancelled"
        assert saved["status"] == "cancelled"
        assert cleanup_calls == 1
        assert not human_signal.has_subscribers
    finally:
        runtime._publish = original_publish
        task = runtime._entries[job.job_id].task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("next_state", ["approval", "cancelled"])
async def test_stale_approval_cannot_change_a_newer_job_generation(tmp_path: Path, next_state: str) -> None:
    """A card authorizes only the generation originally projected to its parent."""
    runtime = tool_job_runtime(tmp_path)
    executions = 0

    async def approval() -> BackgroundOutcome:
        nonlocal executions
        executions += 1
        return BackgroundOutcome("awaiting_approval")

    try:
        await start_job(
            runtime,
            "generation",
            tool_name="tool",
            depth=0,
            adapter={},
            owner=job_owner(),
            operation=approval,
        )
        waited = await runtime.wait("generation", owner=job_owner(), depth=0)
        await runtime.acknowledge_wait("generation", waited.claim)
        if next_state == "approval":
            await runtime.continue_job(
                "generation",
                owner=job_owner(),
                depth=0,
                expected_generation=0,
                operation=approval,
                adapter={},
            )
            waited = await runtime.wait("generation", owner=job_owner(), depth=0)
            await runtime.acknowledge_wait("generation", waited.claim)
        else:
            await runtime.cancel("generation", owner=job_owner(), depth=0)
        before = await runtime.lookup("generation", owner=job_owner(), depth=0)
        with pytest.raises(ValueError, match="Approval no longer applies"):
            await runtime.continue_job(
                "generation",
                owner=job_owner(),
                depth=0,
                expected_generation=0,
                operation=approval,
                adapter={},
            )
        assert await runtime.lookup("generation", owner=job_owner(), depth=0) == before
        assert executions == (2 if next_state == "approval" else 1)
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_approval_cancellation_survives_a_crash_during_cleanup(tmp_path: Path) -> None:
    """The durable cancellation admission already owns a fresh unconsumed generation."""
    cleaning, release = asyncio.Event(), asyncio.Event()

    async def approval() -> BackgroundOutcome:
        return BackgroundOutcome("awaiting_approval")

    async def cleanup(_job: runtime_module.BackgroundJob) -> None:
        cleaning.set()
        await release.wait()

    runtime = tool_job_runtime(tmp_path, cancel=cleanup)
    path = tmp_path / "tool_jobs" / "cancel-crash.json"
    cancelling = None
    try:
        await start_job(
            runtime,
            "cancel-crash",
            tool_name="tool",
            depth=0,
            adapter={},
            owner=job_owner(),
            operation=approval,
        )
        waited = await runtime.wait("cancel-crash", owner=job_owner(), depth=0)
        await runtime.acknowledge_wait("cancel-crash", waited.claim)
        cancelling = asyncio.create_task(runtime.cancel("cancel-crash", owner=job_owner(), depth=0))
        await cleaning.wait()
        admitted = path.read_bytes()
    finally:
        release.set()
        if cancelling is not None:
            await cancelling
        await runtime.shutdown()
    path.write_bytes(admitted)
    restored = tool_job_runtime(tmp_path)
    try:
        await restored.recover()
        outcomes = await restored.pending_outcomes()
        assert len(outcomes) == 1
        assert outcomes[0].status == "cancelled"
        assert outcomes[0].generation == 1
        assert not outcomes[0].consumed
    finally:
        await restored.shutdown()


@pytest.mark.asyncio
async def test_failed_atomic_receipt_replacement_preserves_previous_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed rename cannot fall back to overwriting an existing replay receipt."""
    runtime = tool_job_runtime(tmp_path)

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "saved")

    try:
        await start_job(
            runtime,
            "atomic",
            tool_name="tool",
            depth=0,
            adapter={},
            owner=job_owner(),
            operation=operation,
        )
        waited = await runtime.wait("atomic", owner=job_owner(), depth=0)
        path = tmp_path / "tool_jobs" / "atomic.json"
        before = path.read_bytes()
        original = Path.replace

        def fail_replace(source: Path, target: Path) -> Path:
            if target == path:
                message = "receipt rename failed"
                raise OSError(message)
            return original(source, target)

        with monkeypatch.context() as patch:
            patch.setattr(Path, "replace", fail_replace)
            with pytest.raises(OSError, match="receipt rename failed"):
                await runtime.acknowledge_wait("atomic", waited.claim)
            assert path.read_bytes() == before
        await runtime.acknowledge_wait("atomic", waited.claim)
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("prior_claim", ["acknowledged", "retained"])
@pytest.mark.parametrize("published", [False, True])
async def test_failed_approval_cancellation_admission_preserves_generation_transition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    prior_claim: str,
    published: bool,
) -> None:
    """A failed admission keeps the approval current; the retry publishes one new generation and drops old claims."""
    runtime = tool_job_runtime(tmp_path)

    async def approval() -> BackgroundOutcome:
        return BackgroundOutcome("awaiting_approval")

    job = await start_job(
        runtime,
        "approval-cancel",
        tool_name="tool",
        depth=0,
        adapter={},
        owner=job_owner(),
        operation=approval,
    )
    claimed = await runtime.wait(job.job_id, owner=job_owner(), depth=0)
    assert claimed.claim is not None
    if prior_claim == "acknowledged":
        await runtime.acknowledge_wait(job.job_id, claimed.claim)

    writer = runtime_module.write_json_file_durable
    failed = False

    def fail_admission(path: Path, payload: object, *, strict_atomic_replace: bool) -> None:
        nonlocal failed
        if not failed:
            failed = True
            if published:
                writer(path, payload, strict_atomic_replace=strict_atomic_replace)
            msg = "approval cancellation admission failed"
            raise OSError(msg)
        writer(path, payload, strict_atomic_replace=strict_atomic_replace)

    monkeypatch.setattr(runtime_module, "write_json_file_durable", fail_admission)
    try:
        with pytest.raises(OSError, match="approval cancellation admission failed"):
            await runtime.cancel(job.job_id, owner=job_owner(), depth=0)
        after_failure = await runtime.lookup(job.job_id, owner=job_owner(), depth=0)
        assert after_failure.status == "awaiting_approval"
        assert after_failure.generation == 0
        assert after_failure.consumed is (prior_claim == "acknowledged")
        expected_claim = None if prior_claim == "acknowledged" else claimed.claim
        assert runtime._entries[job.job_id].live_claim == expected_claim

        settled = await runtime.cancel(job.job_id, owner=job_owner(), depth=0)
        assert settled.status == "cancelled"
        assert settled.generation == 1
        assert not settled.consumed
        assert runtime._entries[job.job_id].live_claim is None
        assert [pending.job_id for pending in await runtime.pending_outcomes()] == [job.job_id]
        if prior_claim == "retained":
            with pytest.raises(ValueError, match="claim no longer belongs"):
                await runtime.acknowledge_wait(job.job_id, claimed.claim)
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_cancellation_retry_propagates_failed_terminal_persistence(tmp_path: Path) -> None:
    """A cancellation retry reports its own failed terminal persistence instead of success."""
    runtime = tool_job_runtime(tmp_path)
    started = asyncio.Event()
    retry_write_started = asyncio.Event()
    release_retry_write = asyncio.Event()

    async def operation() -> BackgroundOutcome:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError

    job = await start_job(
        runtime,
        "retry-terminal",
        tool_name="tool",
        depth=0,
        adapter={},
        owner=job_owner(),
        operation=operation,
    )
    await started.wait()
    original_publish = runtime._publish
    writes = 0

    async def publish(
        entry: runtime_module._Entry,
        job: runtime_module.BackgroundJob,
        payload: runtime_module.EncodedResultPayload | None = None,
    ) -> None:
        nonlocal writes
        writes += 1
        if writes == 2:
            msg = "initial terminal write failed"
            raise OSError(msg)
        if writes == 3:
            retry_write_started.set()
            await release_retry_write.wait()
            msg = "retry terminal write failed"
            raise OSError(msg)
        await original_publish(entry, job, payload)

    runtime._publish = publish
    retry: asyncio.Task[runtime_module.BackgroundJob] | None = None
    try:
        with pytest.raises(OSError, match="initial terminal write failed"):
            await runtime.cancel(job.job_id, owner=job_owner(), depth=0)
        retry = asyncio.create_task(runtime.cancel(job.job_id, owner=job_owner(), depth=0))
        await asyncio.wait_for(retry_write_started.wait(), JOB_TEST_TIMEOUT)
        for _ in range(3):
            await asyncio.sleep(0)
        release_retry_write.set()
        with pytest.raises(OSError, match="retry terminal write failed"):
            await retry
        saved = json.loads((tmp_path / "tool_jobs" / "retry-terminal.json").read_text())
        assert saved["status"] == "cancel_requested"
    finally:
        release_retry_write.set()
        if retry is not None:
            await asyncio.gather(retry, return_exceptions=True)
        drain = runtime._entries[job.job_id].drain
        if drain is not None:
            await asyncio.gather(drain, return_exceptions=True)
        runtime._publish = original_publish
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_shutdown_cancels_operation_after_failed_cancellation_admission(tmp_path: Path) -> None:
    """A failed cancellation request cannot leave its operation blocking shutdown."""
    runtime = tool_job_runtime(tmp_path)
    started = asyncio.Event()

    async def operation() -> BackgroundOutcome:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError

    job = await start_job(
        runtime,
        "failed-cancel",
        tool_name="tool",
        depth=0,
        adapter={},
        owner=job_owner(),
        operation=operation,
    )
    await started.wait()
    original_publish = runtime._publish

    async def fail_publish(
        _entry: runtime_module._Entry,
        _job: runtime_module.BackgroundJob,
        _payload: runtime_module.EncodedResultPayload | None = None,
    ) -> None:
        msg = "injected durable write failure"
        raise OSError(msg)

    runtime._publish = fail_publish
    with pytest.raises(OSError, match="injected durable write failure"):
        await runtime.cancel(job.job_id, owner=job_owner(), depth=0)
    runtime._publish = original_publish
    shutdown = asyncio.create_task(runtime.shutdown())
    done, _ = await asyncio.wait({shutdown}, timeout=0.2)
    completed_without_rescue = shutdown in done
    if not completed_without_rescue:
        task = runtime._entries[job.job_id].task
        assert task is not None
        task.cancel()
    await shutdown
    assert completed_without_rescue


@pytest.mark.asyncio
async def test_reads_are_copies_and_consumption_hides_pending_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read snapshots cannot mutate stored results, and only acknowledgement consumes them."""
    runtime = tool_job_runtime(tmp_path)
    try:

        async def operation() -> BackgroundOutcome:
            return BackgroundOutcome("completed", "answer", result_payload={"exact": [1]})

        job = await start_job(
            runtime,
            "read",
            tool_name="tool",
            depth=0,
            adapter={"context": [1]},
            owner=job_owner(),
            operation=operation,
        )
        result = await runtime.wait(job.job_id, owner=job_owner(), depth=0)
        await runtime.release_wait(job.job_id, result.claim)
        writer = runtime_module.write_json_file_durable

        def forbid_write(_path: Path, _payload: object, *, strict_atomic_replace: bool) -> None:
            assert strict_atomic_replace
            msg = "read attempted durable mutation"
            raise AssertionError(msg)

        monkeypatch.setattr(runtime_module, "write_json_file_durable", forbid_write)
        snapshot = await runtime.lookup(job.job_id, owner=job_owner(), depth=0)
        snapshot.adapter["context"].append(2)
        (await runtime.read_payload(snapshot))["exact"].append(2)
        pending = await runtime.pending_outcomes()
        pending[0].adapter["context"].append(3)
        outcome = await runtime.outcome(job.job_id, job.generation)
        assert outcome is not None
        outcome.adapter["context"].append(4)
        listed = await runtime.list_jobs(owner=job_owner(), depth=0)
        assert listed[0].adapter == {"context": [1]}
        monkeypatch.setattr(runtime_module, "write_json_file_durable", writer)
        waited = await runtime.wait(job.job_id, owner=job_owner(), depth=0)
        await runtime.acknowledge_wait(job.job_id, waited.claim)
        assert await runtime.outcome(job.job_id, job.generation) is None
        assert await runtime.pending_outcomes() == []
        assert await runtime.read_payload(await runtime.lookup(job.job_id, owner=job_owner(), depth=0)) == {
            "exact": [1],
        }
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_status", [None, "cancelled", "completed"])
async def test_terminal_operation_stays_pending_until_cancellation_cleanup_settles(
    tmp_path: Path,
    cleanup_status: str | None,
) -> None:
    """A cancellation-catching operation cannot make its result deliverable before cleanup."""
    runtime = tool_job_runtime(tmp_path)
    started, cleaning, finish_cleanup = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def operation() -> BackgroundOutcome:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return BackgroundOutcome("completed", "operation answer", result_payload={"source": "operation"})

    async def cleanup(_job: runtime_module.BackgroundJob) -> BackgroundOutcome | None:
        cleaning.set()
        await finish_cleanup.wait()
        if cleanup_status == "completed":
            return BackgroundOutcome("completed", "reconciled answer", result_payload={"source": "cleanup"})
        if cleanup_status == "cancelled":
            return BackgroundOutcome("cancelled")
        return None

    job = await start_job(
        runtime,
        "cleanup",
        tool_name="tool",
        depth=0,
        adapter={},
        owner=job_owner(),
        operation=operation,
        cancel=cleanup,
    )
    await started.wait()
    cancelling = asyncio.create_task(runtime.cancel(job.job_id, owner=job_owner(), depth=0))
    try:
        await cleaning.wait()
        assert (await runtime.lookup(job.job_id, owner=job_owner(), depth=0)).status == "cancel_requested"
        waiting = await runtime.wait(job.job_id, owner=job_owner(), depth=0, timeout=0)
        assert waiting.claim is None
        assert waiting.job.status == "cancel_requested"
        assert await runtime.pending_outcomes() == []
        assert not cancelling.done()
    finally:
        finish_cleanup.set()
        settled = await cancelling
        await runtime.shutdown()
    assert settled.status == "completed"
    expected_source = "cleanup" if cleanup_status == "completed" else "operation"
    expected_answer = "reconciled answer" if cleanup_status == "completed" else "operation answer"
    assert settled.result == expected_answer
    assert json.loads((tmp_path / "tool_jobs" / "cleanup.g0.result.json").read_text()) == {"source": expected_source}


@pytest.mark.asyncio
@pytest.mark.parametrize("continuation", [False, True])
@pytest.mark.parametrize("published", [False, True])
async def test_cancelled_parent_and_failed_admission_reconcile_acceptance(  # noqa: PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    continuation: bool,
    published: bool,
) -> None:
    """Parent cancellation must not hide writer failure or leave a failed admission half-published in memory."""
    runtime = tool_job_runtime(tmp_path)
    human = HumanMessageSignal()
    original_writer = runtime_module.write_json_file_durable
    writing, release_writer = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    calls = 0

    async def approval() -> BackgroundOutcome:
        return BackgroundOutcome("awaiting_approval")

    async def operation() -> BackgroundOutcome:
        nonlocal calls
        calls += 1
        return BackgroundOutcome("completed", "once")

    if continuation:
        with human_message_signal_context(human):
            await start_job(
                runtime,
                "failed",
                tool_name="tool",
                depth=0,
                adapter={},
                owner=job_owner(),
                operation=approval,
            )
        waiting = await runtime.wait("failed", owner=job_owner(), depth=0)
        await runtime.release_wait("failed", waiting.claim)

    def failed_writer(path: Path, payload: object, *, strict_atomic_replace: bool) -> None:
        if published:
            original_writer(path, payload, strict_atomic_replace=strict_atomic_replace)
        loop.call_soon_threadsafe(writing.set)
        assert release_writer.wait(5)
        msg = "admission write failed"
        raise OSError(msg)

    monkeypatch.setattr(runtime_module, "write_json_file_durable", failed_writer)
    with human_message_signal_context(human):
        accepting = asyncio.create_task(
            runtime.continue_job(
                "failed",
                owner=job_owner(),
                depth=0,
                expected_generation=0,
                operation=operation,
                adapter={},
            )
            if continuation
            else runtime.start("failed", tool_name="tool", depth=0, adapter={}, owner=job_owner(), operation=operation),
        )
    try:
        await writing.wait()
        accepting.cancel()
        release_writer.set()
        with pytest.raises(asyncio.CancelledError):
            await accepting
        monkeypatch.setattr(runtime_module, "write_json_file_durable", original_writer)
        jobs = await runtime.list_jobs(owner=job_owner(), depth=0)
        assert calls == 0
        if continuation:
            # The failed continuation never became current, so the approval can still continue exactly once.
            assert [(job.status, job.generation) for job in jobs] == [("awaiting_approval", 0)]
            assert human.has_subscribers
            await runtime.continue_job(
                "failed",
                owner=job_owner(),
                depth=0,
                expected_generation=0,
                operation=operation,
                adapter={},
            )
        else:
            assert jobs == []
            assert not human.has_subscribers
            if published:
                # The landed record keeps its identity from running; recovery reports it interrupted.
                with pytest.raises(ValueError, match="already exists"):
                    await start_job(
                        runtime,
                        "failed",
                        tool_name="tool",
                        depth=0,
                        adapter={},
                        owner=job_owner(),
                        operation=operation,
                    )
            else:
                await start_job(
                    runtime,
                    "failed",
                    tool_name="tool",
                    depth=0,
                    adapter={},
                    owner=job_owner(),
                    operation=operation,
                )
        if continuation or not published:
            assert (await runtime.wait("failed", owner=job_owner(), depth=0)).job.result == "once"
            assert calls == 1
    finally:
        release_writer.set()
        monkeypatch.setattr(runtime_module, "write_json_file_durable", original_writer)
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("budget", [None, 0, 0.01])
async def test_wait_budget_never_cancels_owned_execution(tmp_path: Path, budget: float | None) -> None:
    """Unlimited waits stay pending; finite waits detach and every mode preserves one operation."""
    runtime = tool_job_runtime(tmp_path)
    started, finish = asyncio.Event(), asyncio.Event()
    calls = 0

    async def operation() -> BackgroundOutcome:
        nonlocal calls
        calls += 1
        started.set()
        await finish.wait()
        return BackgroundOutcome("completed", "once")

    waiter = None
    try:
        job = await start_job(
            runtime,
            "budget",
            tool_name="tool",
            depth=0,
            adapter={},
            owner=job_owner(),
            operation=operation,
        )
        await started.wait()
        waiter = asyncio.create_task(runtime.wait(job.job_id, owner=job_owner(), depth=0, timeout=budget))
        if budget is None:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(waiter), 0.02)
            finish.set()
            result = await asyncio.wait_for(waiter, 1)
        else:
            detached = await asyncio.wait_for(waiter, JOB_TEST_TIMEOUT)
            assert detached.job.status == "running"
            assert detached.claim is None
            finish.set()
            result = await runtime.wait(job.job_id, owner=job_owner(), depth=0)
        assert result.job.result == "once"
        assert calls == 1
    finally:
        if waiter is not None:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("budget", [-1, float("inf"), float("-inf"), float("nan"), True, "1"])
async def test_invalid_wait_budget_cannot_claim_result(tmp_path: Path, budget: object) -> None:
    """Malformed budgets fail before acquiring the job's result lease."""
    runtime = tool_job_runtime(tmp_path)

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "saved")

    try:
        job = await start_job(
            runtime,
            "invalid",
            tool_name="tool",
            depth=0,
            adapter={},
            owner=job_owner(),
            operation=operation,
        )
        with pytest.raises(ValueError, match="timeout"):
            await runtime.wait(job.job_id, owner=job_owner(), depth=0, timeout=budget)  # type: ignore[arg-type]
        assert (await runtime.wait(job.job_id, owner=job_owner(), depth=0)).job.result == "saved"
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_repeated_human_followups_release_each_wait_and_clear_allows_waiting(tmp_path: Path) -> None:
    """A prior follow-up cannot permanently detach later turns from the same job."""
    runtime = tool_job_runtime(tmp_path)
    signal = HumanMessageSignal()
    finish = asyncio.Event()

    async def operation() -> BackgroundOutcome:
        await finish.wait()
        return BackgroundOutcome("completed", "saved")

    try:
        with human_message_signal_context(signal):
            job = await start_job(
                runtime,
                "repeat",
                tool_name="tool",
                depth=0,
                adapter={},
                owner=job_owner(),
                operation=operation,
            )
        for _ in range(2):
            signal.clear()
            waiter = asyncio.create_task(runtime.wait(job.job_id, owner=job_owner(), depth=0))
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(waiter), 0.02)
            signal.notify()
            assert (await asyncio.wait_for(waiter, 1)).job.status == "running"
        signal.clear()
        finish.set()
        assert (await runtime.wait(job.job_id, owner=job_owner(), depth=0)).job.result == "saved"
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_failed_cancellation_cleanup_settles_without_claiming_side_effects_stopped(tmp_path: Path) -> None:
    """Cleanup failure must release ownership and retain an honest durable failure outcome."""
    runtime = tool_job_runtime(tmp_path)
    started = asyncio.Event()

    async def operation() -> BackgroundOutcome:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError

    async def cleanup(_job: runtime_module.BackgroundJob) -> None:
        msg = "remote cleanup unavailable"
        raise RuntimeError(msg)

    try:
        job = await start_job(
            runtime,
            "cleanup",
            tool_name="tool",
            depth=0,
            adapter={},
            owner=job_owner(),
            operation=operation,
            cancel=cleanup,
        )
        await started.wait()
        result = await runtime.cancel(job.job_id, owner=job_owner(), depth=0)
        assert result.status == "failed"
        assert "remote cleanup unavailable" in (result.result or "")
        assert "may still" in (result.result or "")
        assert (await runtime.lookup(job.job_id, owner=job_owner(), depth=0)).status == "failed"
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("recovery", [False, True])
async def test_shutdown_cleanup_failure_does_not_strand_other_jobs(tmp_path: Path, *, recovery: bool) -> None:
    """One failing cleanup cannot prevent later executions from settling and releasing the lease."""
    runtime = tool_job_runtime(tmp_path)
    cleaned: list[str] = []

    async def operation() -> BackgroundOutcome:
        await asyncio.Event().wait()
        raise AssertionError

    async def cleanup(job: runtime_module.BackgroundJob) -> None:
        cleaned.append(job.job_id)
        if job.job_id == "first":
            msg = "cleanup failed"
            raise RuntimeError(msg)

    for name in ("first", "second"):
        await start_job(
            runtime,
            name,
            tool_name="tool",
            depth=0,
            adapter={},
            owner=job_owner(),
            operation=operation,
            cancel=cleanup,
        )
    snapshots = {path: path.read_bytes() for path in (tmp_path / "tool_jobs").glob("*.json")}
    await runtime.shutdown()
    assert cleaned == ["first", "second"]
    if recovery:
        for path, snapshot in snapshots.items():
            path.write_bytes(snapshot)
        cleaned.clear()
    restored = tool_job_runtime(tmp_path, cancel=cleanup)
    try:
        await restored.recover()
        assert (await restored.lookup("first", owner=job_owner(), depth=0)).status == "failed"
        assert (await restored.lookup("second", owner=job_owner(), depth=0)).status == "interrupted"
        assert cleaned == ["first", "second"]
    finally:
        await restored.shutdown()


@pytest.mark.asyncio
async def test_unsupported_snapshot_schema_fails_enabled_recovery(tmp_path: Path) -> None:
    """Enabled recovery reports a retired snapshot loudly instead of adopting or rewriting it."""
    runtime = tool_job_runtime(tmp_path)

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "saved answer")

    await start_job(runtime, "retired", tool_name="tool", depth=0, adapter={}, owner=job_owner(), operation=operation)
    await runtime.shutdown()
    path = tmp_path / "tool_jobs" / "retired.json"
    path.write_text(json.dumps({**json.loads(path.read_text()), "schema_version": 1}))
    retired = path.read_bytes()
    restored = tool_job_runtime(tmp_path)
    try:
        with pytest.raises(
            runtime_module.UnsupportedToolJobSnapshotError,
            match=r"\(schema_version=1\); remove it to continue\.$",
        ):
            await restored.recover()
        assert path.read_bytes() == retired
    finally:
        await restored.shutdown()


@pytest.mark.asyncio
async def test_failure_while_draining_cancellation_is_not_reported_cancelled(tmp_path: Path) -> None:
    """An operation's failing finalizer remains visible after its cancellation request."""
    runtime = tool_job_runtime(tmp_path)
    started = asyncio.Event()

    async def operation() -> BackgroundOutcome:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            msg = "operation cleanup failed"
            raise RuntimeError(msg)

    try:
        job = await start_job(
            runtime,
            "draining",
            tool_name="tool",
            depth=0,
            adapter={},
            owner=job_owner(),
            operation=operation,
        )
        await started.wait()
        result = await runtime.cancel(job.job_id, owner=job_owner(), depth=0)
        assert result.status == "failed"
        assert result.result == "operation cleanup failed"
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_external_task_cancel_recovers_as_interrupted(tmp_path: Path) -> None:
    """Event-loop teardown is not a user cancellation; recovery reports the execution as interrupted."""
    runtime = tool_job_runtime(tmp_path)
    started = asyncio.Event()

    async def operation() -> BackgroundOutcome:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError

    await start_job(runtime, "teardown", tool_name="tool", depth=0, adapter={}, owner=job_owner(), operation=operation)
    await started.wait()
    task = runtime._entries["teardown"].task
    assert task is not None
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    path = tmp_path / "tool_jobs" / "teardown.json"
    assert runtime_module.read_job_snapshot(path).status == "running"
    # The process dies with its loop: the storage lease goes away without an orderly shutdown.
    runtime._lease.close()
    restored = tool_job_runtime(tmp_path)
    try:
        await restored.recover()
        recovered = await restored.lookup("teardown", owner=job_owner(), depth=0)
        assert recovered.status == "interrupted"
        assert "runtime restart" in (recovered.result or "")
    finally:
        await restored.shutdown()


@pytest.mark.asyncio
async def test_operation_raising_cancellation_itself_settles_cancelled(tmp_path: Path) -> None:
    """A CancelledError the operation raises without a task cancellation request still settles and wakes waiters."""
    runtime = tool_job_runtime(tmp_path)

    async def operation() -> BackgroundOutcome:
        cancelled = asyncio.get_running_loop().create_future()
        cancelled.cancel()
        await cancelled
        raise AssertionError

    try:
        await start_job(
            runtime,
            "self-cancelled",
            tool_name="tool",
            depth=0,
            adapter={},
            owner=job_owner(),
            operation=operation,
        )
        waited = await runtime.wait("self-cancelled", owner=job_owner(), depth=0)
        assert waited.job.status == "cancelled"
        await runtime.acknowledge_wait("self-cancelled", waited.claim)
        saved = runtime_module.read_job_snapshot(tmp_path / "tool_jobs" / "self-cancelled.json")
        assert saved.status == "cancelled"
    finally:
        await runtime.shutdown()


class _ContendedLock(asyncio.Lock):
    """Report when another task waits for this lock while it is held."""

    def __init__(self) -> None:
        super().__init__()
        self.contended = asyncio.Event()

    async def acquire(self) -> Literal[True]:
        if self.locked():
            self.contended.set()
        return await super().acquire()


@pytest.mark.asyncio
async def test_repeated_waiter_cancellation_still_releases_its_claim(tmp_path: Path) -> None:
    """A second cancellation while the claim release waits for the runtime lock cannot leak the claim."""
    runtime = tool_job_runtime(tmp_path)
    lock = _ContendedLock()
    runtime._lock = lock
    finish = asyncio.Event()

    async def operation() -> BackgroundOutcome:
        await finish.wait()
        return BackgroundOutcome("completed", "saved")

    await start_job(runtime, "claimed", tool_name="tool", depth=0, adapter={}, owner=job_owner(), operation=operation)
    entry = runtime._entries["claimed"]
    waiter = asyncio.create_task(runtime.wait("claimed", owner=job_owner(), depth=0))
    try:
        while entry.claim is None:  # noqa: ASYNC110 - the claim publishes no event to await
            await asyncio.sleep(0)
        await lock.acquire()
        lock.contended.clear()
        waiter.cancel()
        await asyncio.wait_for(lock.contended.wait(), 30)
        waiter.cancel()
        lock.release()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert entry.claim is None
        finish.set()
        assert entry.task is not None
        await entry.task
        assert await runtime.outcome("claimed", 0) is not None
    finally:
        if lock.locked():
            lock.release()
        finish.set()
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["stop", "revoke"])
async def test_one_failed_cancellation_request_does_not_block_the_others(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    """Stop and revocation still reach later jobs when one job's save fails, and report which job failed."""
    revoked = False
    runtime = tool_job_runtime(tmp_path, authorize=lambda _job: not revoked)
    writer = runtime_module.write_json_file_durable
    failing = False

    def failing_writer(path: Path, payload: object, *, strict_atomic_replace: bool) -> None:
        if failing and path.name == "first.json":
            msg = "injected durable write failure"
            raise OSError(msg)
        writer(path, payload, strict_atomic_replace=strict_atomic_replace)

    async def operation() -> BackgroundOutcome:
        await asyncio.Event().wait()
        raise AssertionError

    async def every_job(_job: runtime_module.BackgroundJob) -> bool:
        return True

    monkeypatch.setattr(runtime_module, "write_json_file_durable", failing_writer)
    try:
        for job_id in ("first", "second"):
            await start_job(
                runtime,
                job_id,
                tool_name="tool",
                depth=0,
                adapter={},
                owner=job_owner(),
                operation=operation,
            )
        failing = revoked = True
        with capture_logs() as logs:
            if action == "stop":
                with pytest.raises(ExceptionGroup, match="Tool job Stop failed"):
                    await runtime.stop_jobs(receipt_order=1, matches=every_job)
            else:
                # Revocation only logs; its next pass retries the failed job.
                await runtime.cancel_revoked(denied=lambda _job: revoked)
        failing = revoked = False
        assert [entry["job_id"] for entry in logs if entry["log_level"] == "error"] == ["first"]
        settled = await runtime.wait("second", owner=job_owner(), depth=0)
        await runtime.release_wait("second", settled.claim)
        assert settled.job.status == "cancelled"
        assert (await runtime.lookup("first", owner=job_owner(), depth=0)).status == "running"
    finally:
        failing = revoked = False
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_failed_detached_drain_reports_its_job(tmp_path: Path) -> None:
    """A drain Stop leaves running reports its failure with the job it could not settle, which stays requested."""
    release = asyncio.Event()
    blocked = True

    async def cleanup(_job: runtime_module.BackgroundJob) -> None:
        await release.wait()
        if blocked:
            msg = "child still running"
            raise runtime_module.JobRecoveryBlockedError(msg)

    async def operation() -> BackgroundOutcome:
        await asyncio.Event().wait()
        raise AssertionError

    async def every_job(_job: runtime_module.BackgroundJob) -> bool:
        return True

    runtime = tool_job_runtime(tmp_path, cancel=cleanup)
    try:
        await start_job(runtime, "held", tool_name="tool", depth=0, adapter={}, owner=job_owner(), operation=operation)
        await runtime.stop_jobs(receipt_order=1, matches=every_job)
        drain = runtime._entries["held"].drain
        assert drain is not None
        with capture_logs() as logs:
            release.set()
            await asyncio.gather(drain, return_exceptions=True)
        assert [(entry["event"], entry["job_id"]) for entry in logs if entry["log_level"] == "error"] == [
            ("Tool job cancellation drain failed", "held"),
        ]
        assert (await runtime.lookup("held", owner=job_owner(), depth=0)).status == "cancel_requested"
    finally:
        release.set()
        blocked = False
        await runtime.shutdown()
