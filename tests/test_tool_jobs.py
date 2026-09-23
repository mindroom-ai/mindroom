"""Generic job ownership, scoped discovery, admission, and durable consumption."""

from __future__ import annotations

import asyncio
import gc
import json
import threading
import weakref
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import pytest

from mindroom.tool_jobs import runtime as runtime_module
from mindroom.tool_jobs.control import HumanMessageSignal, human_message_signal_context, job_checkpoint
from mindroom.tool_jobs.runtime import BackgroundOutcome, ToolJobRuntime
from tests.test_background_subagents import _owner
from tests.tool_job_helpers import tool_job_runtime, wait_for_status


def _age(runtime: ToolJobRuntime, job_id: str, updated_at: datetime) -> None:
    """Backdate a saved job in memory so retention treats it as old."""
    entry = runtime._entries[job_id]
    entry.job = replace(entry.job, updated_at=updated_at.isoformat())


@pytest.mark.asyncio
async def test_wait_rejects_unrepresentable_timeout_before_lookup(tmp_path: Path) -> None:
    """Both entry points reject budgets that cannot be represented on the event-loop clock."""
    runtime = tool_job_runtime(tmp_path)
    try:
        with pytest.raises(ValueError, match="finite"):
            await runtime.wait("unknown", owner=_owner(), depth=0, timeout=10**400)
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_final_shutdown_waits_for_receipt_publication(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A replacement owner must never recover before the old owner's last receipt lands."""
    runtime = tool_job_runtime(tmp_path)

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "saved answer")

    await runtime.start("receipt", tool_name="tool", depth=0, adapter={}, owner=_owner(), operation=operation)
    waited = await runtime.wait("receipt", owner=_owner(), depth=0)
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
    acknowledging = asyncio.create_task(runtime.acknowledge_wait("receipt", waited.token))
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
        job = await runtime.start("paused", tool_name="tool", depth=0, adapter={}, owner=_owner(), operation=operation)
        waited = await runtime.wait(job.job_id, owner=_owner(), depth=0)
        await runtime.quiesce()
        with pytest.raises(BlockingIOError):
            tool_job_runtime(tmp_path)
        with pytest.raises(runtime_module.JobAccessError, match="shutting down"):
            await runtime.start("new", tool_name="tool", depth=0, adapter={}, owner=_owner(), operation=operation)
        with pytest.raises(runtime_module.JobAccessError, match="shutting down"):
            await runtime.continue_job("paused", owner=_owner(), depth=0, expected_generation=0, operation=operation)
        with pytest.raises(runtime_module.JobAccessError, match="shutting down"):
            await runtime.cancel("paused", owner=_owner(), depth=0)
        await runtime.acknowledge_wait(job.job_id, waited.token)
        assert (await runtime.lookup(job.job_id, owner=_owner(), depth=0)).wait_acknowledged
    finally:
        await runtime.shutdown()
    restored = tool_job_runtime(tmp_path)
    try:
        await restored.recover()
        assert await restored.pending_outcomes() == []
        assert (await restored.lookup(job.job_id, owner=_owner(), depth=0)).status == "awaiting_approval"
    finally:
        await restored.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("acknowledged", [False, True])
async def test_consumed_payload_is_loaded_on_demand_without_startup_rewrites(
    tmp_path: Path,
    *,
    acknowledged: bool,
) -> None:
    """Terminal history keeps disk results and replay ownership without resident payloads."""
    runtime = tool_job_runtime(tmp_path)
    value = "large result" * 10_000
    calls = 0

    async def operation() -> BackgroundOutcome:
        nonlocal calls
        calls += 1
        return BackgroundOutcome("completed", value, result_payload={"value": value})

    await runtime.start("consumed", tool_name="tool", depth=0, adapter={}, owner=_owner(), operation=operation)
    waited = await runtime.wait("consumed", owner=_owner(), depth=0)
    await runtime.cancel("consumed", owner=_owner(), depth=0)
    if acknowledged:
        await runtime.acknowledge_wait("consumed", waited.token)
    else:
        await runtime.release_wait("consumed", waited.token)
    path = tmp_path / "tool_jobs" / "consumed.json"
    published = path.stat().st_mtime_ns
    try:
        assert runtime._entries["consumed"].job.result_payload is None
        assert runtime._entries["consumed"].drain is None
        assert (await runtime.lookup("consumed", owner=_owner(), depth=0)).result_payload == {"value": value}
        assert bool(await runtime.pending_outcomes()) is not acknowledged
    finally:
        await runtime.shutdown()
    assert path.stat().st_mtime_ns == published
    restored = tool_job_runtime(tmp_path)
    try:
        await restored.recover()
        assert path.stat().st_mtime_ns == published
        assert restored._entries["consumed"].job.result_payload is None
        await restored.start(
            "consumed",
            tool_name="tool",
            depth=0,
            adapter={},
            owner=_owner(),
            operation=operation,
            reattach=True,
        )
        reread = await restored.wait("consumed", owner=_owner(), depth=0)
        assert (reread.job.result, reread.job.summary_truncated) == (
            value[: runtime_module._JOB_SUMMARY_MAX_CHARS],
            True,
        )
        assert reread.job.result_payload == {"value": value}
        assert calls == 1
        await restored.acknowledge_wait("consumed", reread.token)
        assert restored._entries["consumed"].job.result_payload is None
    finally:
        await restored.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("owned", [False, True])
async def test_cancelling_consumed_job_does_not_retain_the_returned_payload(tmp_path: Path, *, owned: bool) -> None:
    """Cancelling already-settled history must not put its full result back in the runtime cache."""
    runtime = tool_job_runtime(tmp_path)
    value = "large result" * 10_000

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome("completed", value, result_payload={"value": value})

    await runtime.start("consumed", tool_name="tool", depth=0, adapter={}, owner=_owner(), operation=operation)
    waited = await runtime.wait("consumed", owner=_owner(), depth=0)
    await runtime.acknowledge_wait("consumed", waited.token)
    try:
        result = (
            await runtime.cancel_owned("consumed", matches=lambda job: job.job_id == "consumed")
            if owned
            else await runtime.cancel("consumed", owner=_owner(), depth=0)
        )
        assert result is not None
        assert result.result_payload == {"value": value}
        reference = weakref.ref(result)
        del result
        await asyncio.sleep(0)
        gc.collect()
        assert reference() is None, "runtime retained the full cancelled-history snapshot"
        assert runtime._entries["consumed"].job.result_payload is None
        assert (await runtime.lookup("consumed", owner=_owner(), depth=0)).result_payload == {"value": value}
    finally:
        await runtime.shutdown()


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

    await runtime.start("retry", tool_name="tool", depth=0, adapter={}, owner=_owner(), operation=operation)
    with monkeypatch.context() as patch:
        patch.setattr(runtime_module, "write_json_file_durable", fail_terminal)
        with pytest.raises(OSError, match="terminal save failed"):
            await runtime.cancel("retry", owner=_owner(), depth=0)

    snapshot_started, release_snapshot = asyncio.Event(), asyncio.Event()
    retry_started, release_retry = asyncio.Event(), asyncio.Event()
    original_snapshot, original_publish = runtime._snapshot, runtime._publish

    async def snapshot(entry: runtime_module._Entry, *, include_result: bool = True) -> runtime_module.BackgroundJob:
        if not snapshot_started.is_set():
            snapshot_started.set()
            await release_snapshot.wait()
        return await original_snapshot(entry, include_result=include_result)

    async def publish(entry: runtime_module._Entry, job: runtime_module.BackgroundJob) -> None:
        if not retry_started.is_set():
            retry_started.set()
            await release_retry.wait()
        await original_publish(entry, job)

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
async def test_cancelled_saved_result_read_releases_its_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelling a disk reread must not leave a forgotten waiter owning the result."""
    runtime = tool_job_runtime(tmp_path)
    started, release = threading.Event(), threading.Event()
    reader = runtime_module.read_job_snapshot

    def gated_read(path: Path) -> runtime_module.BackgroundJob:
        started.set()
        assert release.wait(5)
        return reader(path)

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "saved")

    await runtime.start("read", tool_name="tool", depth=0, adapter={}, owner=_owner(), operation=operation)
    waited = await runtime.wait("read", owner=_owner(), depth=0)
    await runtime.acknowledge_wait("read", waited.token)
    monkeypatch.setattr(runtime_module, "read_job_snapshot", gated_read)
    reading = asyncio.create_task(runtime.wait("read", owner=_owner(), depth=0))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        reading.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await reading
        retried = await runtime.wait("read", owner=_owner(), depth=0)
        assert retried.token is not None
        assert retried.job.result == "saved"
        await runtime.release_wait("read", retried.token)
    finally:
        release.set()
        await runtime.shutdown()


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

    await runtime.start(
        "wake",
        tool_name="tool",
        depth=0,
        adapter={},
        owner=_owner(),
        operation=operation,
        cancel=cleanup,
    )
    waiter = asyncio.create_task(runtime.wait("wake", owner=_owner(), depth=0))
    try:
        with monkeypatch.context() as patch:
            patch.setattr(runtime_module, "write_json_file_durable", fail_terminal)
            cancelling = asyncio.create_task(runtime.cancel("wake", owner=_owner(), depth=0))
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
        await runtime.acknowledge_wait("wake", waited.token)
        saved = runtime_module.read_job_snapshot(tmp_path / "tool_jobs" / "wake.json")
        assert saved.wait_acknowledged
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
        await runtime.start("cancel-retry", tool_name="tool", depth=0, adapter={}, owner=_owner(), operation=operation)
        with monkeypatch.context() as patch:
            patch.setattr(runtime_module, "write_json_file_durable", fail_terminal)
            with pytest.raises(OSError, match="terminal save failed"):
                await runtime.cancel("cancel-retry", owner=_owner(), depth=0)
        waited = await runtime.wait("cancel-retry", owner=_owner(), depth=0)
        await runtime.acknowledge_wait("cancel-retry", waited.token)
        result = await runtime.cancel("cancel-retry", owner=_owner(), depth=0)
        assert result.status == "cancelled"
        assert result.wait_acknowledged
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_expiry_rechecks_a_read_completed_during_source_lookup(tmp_path: Path) -> None:
    """A concurrent reader renews retention before a previously eligible candidate is compacted."""
    runtime = tool_job_runtime(tmp_path)

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "saved")

    async def source_finished(job: runtime_module.BackgroundJob) -> bool:
        waited = await runtime.wait(job.job_id, owner=_owner(), depth=0)
        await runtime.acknowledge_wait(job.job_id, waited.token)
        return True

    try:
        await runtime.start("reread", tool_name="tool", depth=0, adapter={}, owner=_owner(), operation=operation)
        waited = await runtime.wait("reread", owner=_owner(), depth=0)
        await runtime.acknowledge_wait("reread", waited.token)
        cutoff = datetime.now(UTC) - timedelta(days=30)
        _age(runtime, "reread", cutoff - timedelta(seconds=1))
        await runtime.expire_consumed(before=cutoff, source_finished=source_finished)
        saved = await runtime.lookup("reread", owner=_owner(), depth=0)
        assert not saved.result_expired
        assert saved.result == "saved"
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_result_expiry_preserves_receipt_and_protected_work(tmp_path: Path) -> None:
    """Only old consumed terminal results with a finished source may expire."""
    runtime = tool_job_runtime(tmp_path)
    calls: list[str] = []

    def adapter() -> dict[str, object]:
        return {"arguments": {"payload": "sensitive input" * 500}}

    async def completed() -> BackgroundOutcome:
        calls.append("executed")
        return BackgroundOutcome("completed", "saved", result_payload={"value": "saved"})

    async def source_finished(job: runtime_module.BackgroundJob) -> bool:
        return job.job_id != "approval"

    now = datetime.now(UTC)
    cutoff = now - timedelta(days=30)
    claimed = None
    try:
        for name in ("expire", "approval", "unread", "recent", "claimed"):
            await runtime.start(
                name,
                tool_name="tool",
                depth=0,
                adapter=adapter(),
                owner=_owner(),
                operation=completed,
            )
            waited = await runtime.wait(name, owner=_owner(), depth=0)
            if name != "unread":
                await runtime.acknowledge_wait(name, waited.token)
            else:
                await runtime.release_wait(name, waited.token)
            if name != "recent":
                _age(runtime, name, cutoff - timedelta(seconds=1))
        claimed = await runtime.wait("claimed", owner=_owner(), depth=0)
        await runtime.expire_consumed(before=cutoff, source_finished=source_finished)
        expired = await runtime.lookup("expire", owner=_owner(), depth=0)
        assert expired.result_expired
        assert expired.result_payload is None
        assert "arguments" not in expired.adapter
        assert "sensitive input" not in (tmp_path / "tool_jobs" / "expire.json").read_text()
        for name in ("approval", "unread", "recent", "claimed"):
            saved = await runtime.lookup(name, owner=_owner(), depth=0)
            assert not saved.result_expired
            assert saved.result_payload == {"value": "saved"}
        assert [job.job_id for job in await runtime.pending_outcomes()] == ["unread"]
    finally:
        if claimed is not None:
            await runtime.release_wait("claimed", claimed.token)
        await runtime.shutdown()
    restored = tool_job_runtime(tmp_path)
    try:
        await restored.recover()
        with pytest.raises(runtime_module.JobAccessError, match="expired"):
            await restored.start(
                "expire",
                tool_name="tool",
                depth=0,
                adapter=adapter(),
                owner=_owner(),
                operation=completed,
                reattach=True,
            )
        with pytest.raises(ValueError, match="already exists"):
            await restored.start(
                "expire",
                tool_name="tool",
                depth=0,
                adapter=adapter(),
                owner=_owner(),
                operation=completed,
            )
        assert len(calls) == 5
        waited = await restored.wait("expire", owner=_owner(), depth=0)
        assert "expired" in waited.job.result
        assert waited.job.wait_acknowledged
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

    first = await runtime.start(
        "first",
        tool_name="search",
        depth=0,
        toolkit_name="web",
        adapter={"run_id": "old"},
        owner=_owner(),
        operation=completed,
    )
    waiting = await runtime.wait(first.job_id, owner=_owner(), depth=0)
    await runtime.acknowledge_wait(first.job_id, waiting.token)
    await runtime.start(
        "second",
        tool_name="fetch",
        depth=0,
        adapter={"run_id": "new"},
        owner=_owner(),
        operation=running,
    )
    assert [job.job_id for job in await runtime.list_jobs(owner=_owner(), depth=0)] == ["second", "first"]
    assert [job.job_id for job in await runtime.list_jobs(owner=_owner(), depth=0, limit=1, offset=1)] == ["first"]
    for other in (
        replace(_owner(), requester_id="@other:test"),
        replace(_owner(), transport_agent_name="other"),
        replace(_owner(), resolved_thread_id="$other"),
    ):
        assert await runtime.list_jobs(owner=other, depth=0) == []
    assert await runtime.list_jobs(owner=_owner(), depth=1) == []
    await runtime.shutdown()
    restored = tool_job_runtime(tmp_path)
    try:
        await restored.recover()
        jobs = await restored.list_jobs(owner=_owner(), depth=0)
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
        await runtime.start(job_id, tool_name="read", depth=0, adapter={}, owner=_owner(), operation=completed)
        waited = await runtime.wait(job_id, owner=_owner(), depth=0)
        await runtime.acknowledge_wait(job_id, waited.token)
    before = await runtime.list_jobs(owner=_owner(), depth=0)
    assert [job.job_id for job in before] == ["anewer", "zolder"]
    await runtime.shutdown()
    restored = tool_job_runtime(tmp_path)
    try:
        await restored.recover()
        after = await restored.list_jobs(owner=_owner(), depth=0)
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
            await runtime.start("retry", tool_name="tool", depth=0, adapter={}, owner=_owner(), operation=operation)
        assert not signal.has_subscribers
        assert await runtime.list_jobs(owner=_owner(), depth=0) == []
        assert calls == 0
        monkeypatch.setattr(runtime_module, "write_json_file_durable", original)
        job = await runtime.start("retry", tool_name="tool", depth=0, adapter={}, owner=_owner(), operation=operation)
        assert (await runtime.wait(job.job_id, owner=_owner(), depth=0)).job.result == "once"
        assert calls == 1
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_stale_wait_receipt_cannot_consume_replacement_generation(tmp_path: Path) -> None:
    """Replacing an approval invalidates its old result lease and pending generation."""
    runtime = tool_job_runtime(tmp_path)

    async def approval() -> BackgroundOutcome:
        return BackgroundOutcome("awaiting_approval")

    try:
        job = await runtime.start(
            "approval",
            tool_name="delegate",
            depth=0,
            adapter={},
            owner=_owner(),
            operation=approval,
        )
        waiting = await runtime.wait(job.job_id, owner=_owner(), depth=0)
        await runtime.cancel(job.job_id, owner=_owner(), depth=0)
        with pytest.raises(ValueError, match="no longer belongs"):
            await runtime.acknowledge_wait(job.job_id, waiting.token)
        pending = await runtime.pending_outcomes()
        assert len(pending) == 1
        assert pending[0].generation > waiting.job.generation
        assert await runtime.outcome(job.job_id, waiting.job.generation) is None
        current = await runtime.outcome(job.job_id, pending[0].generation)
        assert current is not None
        assert current.status == "cancelled"
        claimed = await runtime.wait(job.job_id, owner=_owner(), depth=0)
        assert await runtime.outcome(job.job_id, pending[0].generation) is None
        await runtime.acknowledge_wait(job.job_id, claimed.token)
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
            job = await runtime.start(
                "work",
                tool_name="tool",
                depth=0,
                adapter={},
                owner=_owner(),
                operation=operation,
            )
        await running.wait()
        waiter = asyncio.create_task(runtime.wait(job.job_id, owner=_owner(), depth=0))
        await asyncio.sleep(0)
        signal.notify()
        signal.clear()
        result = await asyncio.wait_for(waiter, 1)
        assert result.job.status == "running"
        assert result.token is None
        proceed.set()
        await asyncio.wait_for(finished.wait(), 1)
        assert (await runtime.wait(job.job_id, owner=_owner(), depth=0)).job.result == "done"
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
        await runtime.start("ambiguous", tool_name="write", depth=0, adapter={}, owner=_owner(), operation=operation)
    monkeypatch.setattr(runtime_module, "write_json_file_durable", writer)
    with pytest.raises(ValueError, match="already exists"):
        await runtime.start("ambiguous", tool_name="write", depth=0, adapter={}, owner=_owner(), operation=operation)
    with pytest.raises(runtime_module.JobAccessError):
        await runtime.lookup("ambiguous", owner=_owner(), depth=0)
    await runtime.shutdown()
    restored = tool_job_runtime(tmp_path)
    try:
        await restored.recover()
        assert (await restored.lookup("ambiguous", owner=_owner(), depth=0)).status == "interrupted"
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

        job = await runtime.start("approval", tool_name="tool", depth=0, adapter={}, owner=_owner(), operation=approval)
        result = await runtime.wait(job.job_id, owner=_owner(), depth=0)
        await runtime.release_wait(job.job_id, result.token)
        writer = runtime_module.write_json_file_durable
        monkeypatch.setattr(runtime_module, "write_json_file_durable", failed_write)
        with pytest.raises(OSError, match="write failed"):
            await runtime.continue_job(
                job.job_id,
                owner=_owner(),
                depth=0,
                expected_generation=0,
                operation=continuation,
            )
        assert (await runtime.lookup(job.job_id, owner=_owner(), depth=0)).status == "awaiting_approval"
        monkeypatch.setattr(runtime_module, "write_json_file_durable", writer)
        await runtime.continue_job(job.job_id, owner=_owner(), depth=0, expected_generation=0, operation=continuation)
        assert (await runtime.wait(job.job_id, owner=_owner(), depth=0)).job.result == "once"
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

    job = await runtime.start("cancel", tool_name="blocking", depth=0, adapter={}, owner=_owner(), operation=operation)
    await started.wait()
    cancelling = asyncio.create_task(runtime.cancel(job.job_id, owner=_owner(), depth=0))
    await cleaning.wait()
    assert (await runtime.lookup(job.job_id, owner=_owner(), depth=0)).status == "cancel_requested"
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
        await runtime.start(
            "twice",
            tool_name="tool",
            depth=0,
            adapter={},
            owner=_owner(),
            operation=operation,
            cancel=cleanup,
        )
        first = asyncio.create_task(runtime.cancel("twice", owner=_owner(), depth=0))
        await cleaning.wait()
        second = asyncio.create_task(runtime.cancel("twice", owner=_owner(), depth=0))
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
        await runtime.start("caller", tool_name="tool", depth=0, adapter={}, owner=_owner(), operation=operation)
        await started.wait()
        monkeypatch.setattr(runtime_module, "write_json_file_durable", blocked_request)
        cancelling = asyncio.create_task(runtime.cancel("caller", owner=_owner(), depth=0))
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
        job = await runtime.start(
            "retry-cancel",
            tool_name="tool",
            depth=0,
            adapter={},
            owner=_owner(),
            operation=operation,
            cancel=cleanup,
        )
    await started.wait()
    original_publish = runtime._publish
    writes = 0

    async def publish(entry: runtime_module._Entry, job: runtime_module.BackgroundJob) -> None:
        nonlocal writes
        writes += 1
        if writes == failed_write:
            msg = "injected durable write failure"
            raise OSError(msg)
        await original_publish(entry, job)

    runtime._publish = publish
    try:
        with pytest.raises(OSError, match="injected durable write failure"):
            await runtime.cancel(job.job_id, owner=_owner(), depth=0)
        settled = await runtime.cancel(job.job_id, owner=_owner(), depth=0)
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
        await runtime.start("generation", tool_name="tool", depth=0, adapter={}, owner=_owner(), operation=approval)
        waited = await runtime.wait("generation", owner=_owner(), depth=0)
        await runtime.acknowledge_wait("generation", waited.token)
        if next_state == "approval":
            await runtime.continue_job("generation", owner=_owner(), depth=0, expected_generation=0, operation=approval)
            waited = await runtime.wait("generation", owner=_owner(), depth=0)
            await runtime.acknowledge_wait("generation", waited.token)
        else:
            await runtime.cancel("generation", owner=_owner(), depth=0)
        before = await runtime.lookup("generation", owner=_owner(), depth=0)
        with pytest.raises(ValueError, match="Approval no longer applies"):
            await runtime.continue_job("generation", owner=_owner(), depth=0, expected_generation=0, operation=approval)
        assert await runtime.lookup("generation", owner=_owner(), depth=0) == before
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
        await runtime.start("cancel-crash", tool_name="tool", depth=0, adapter={}, owner=_owner(), operation=approval)
        waited = await runtime.wait("cancel-crash", owner=_owner(), depth=0)
        await runtime.acknowledge_wait("cancel-crash", waited.token)
        cancelling = asyncio.create_task(runtime.cancel("cancel-crash", owner=_owner(), depth=0))
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
        assert outcomes[0].status == "interrupted"
        assert outcomes[0].generation == 1
        assert not outcomes[0].wait_acknowledged
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
        await runtime.start("atomic", tool_name="tool", depth=0, adapter={}, owner=_owner(), operation=operation)
        waited = await runtime.wait("atomic", owner=_owner(), depth=0)
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
                await runtime.acknowledge_wait("atomic", waited.token)
            assert path.read_bytes() == before
        await runtime.acknowledge_wait("atomic", waited.token)
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

    job = await runtime.start(
        "approval-cancel",
        tool_name="tool",
        depth=0,
        adapter={},
        owner=_owner(),
        operation=approval,
    )
    claimed = await runtime.wait(job.job_id, owner=_owner(), depth=0)
    assert claimed.token is not None
    if prior_claim == "acknowledged":
        await runtime.acknowledge_wait(job.job_id, claimed.token)

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
            await runtime.cancel(job.job_id, owner=_owner(), depth=0)
        after_failure = await runtime.lookup(job.job_id, owner=_owner(), depth=0)
        assert after_failure.status == "awaiting_approval"
        assert after_failure.generation == 0
        assert after_failure.wait_acknowledged is (prior_claim == "acknowledged")
        expected_token = None if prior_claim == "acknowledged" else claimed.token
        assert runtime._entries[job.job_id].wait_token == expected_token

        settled = await runtime.cancel(job.job_id, owner=_owner(), depth=0)
        assert settled.status == "cancelled"
        assert settled.generation == 1
        assert not settled.wait_acknowledged
        assert runtime._entries[job.job_id].wait_token is None
        assert [pending.job_id for pending in await runtime.pending_outcomes()] == [job.job_id]
        if prior_claim == "retained":
            with pytest.raises(ValueError, match="claim no longer belongs"):
                await runtime.acknowledge_wait(job.job_id, claimed.token)
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

    job = await runtime.start(
        "retry-terminal",
        tool_name="tool",
        depth=0,
        adapter={},
        owner=_owner(),
        operation=operation,
    )
    await started.wait()
    original_publish = runtime._publish
    writes = 0

    async def publish(entry: runtime_module._Entry, job: runtime_module.BackgroundJob) -> None:
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
        await original_publish(entry, job)

    runtime._publish = publish
    retry: asyncio.Task[runtime_module.BackgroundJob] | None = None
    try:
        with pytest.raises(OSError, match="initial terminal write failed"):
            await runtime.cancel(job.job_id, owner=_owner(), depth=0)
        retry = asyncio.create_task(runtime.cancel(job.job_id, owner=_owner(), depth=0))
        await asyncio.wait_for(retry_write_started.wait(), 2)
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

    job = await runtime.start(
        "failed-cancel",
        tool_name="tool",
        depth=0,
        adapter={},
        owner=_owner(),
        operation=operation,
    )
    await started.wait()
    original_publish = runtime._publish

    async def fail_publish(_entry: runtime_module._Entry, _job: runtime_module.BackgroundJob) -> None:
        msg = "injected durable write failure"
        raise OSError(msg)

    runtime._publish = fail_publish
    with pytest.raises(OSError, match="injected durable write failure"):
        await runtime.cancel(job.job_id, owner=_owner(), depth=0)
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

        job = await runtime.start(
            "read",
            tool_name="tool",
            depth=0,
            adapter={"context": [1]},
            owner=_owner(),
            operation=operation,
        )
        result = await runtime.wait(job.job_id, owner=_owner(), depth=0)
        await runtime.release_wait(job.job_id, result.token)
        writer = runtime_module.write_json_file_durable

        def forbid_write(_path: Path, _payload: object, *, strict_atomic_replace: bool) -> None:
            assert strict_atomic_replace
            msg = "read attempted durable mutation"
            raise AssertionError(msg)

        monkeypatch.setattr(runtime_module, "write_json_file_durable", forbid_write)
        snapshot = await runtime.lookup(job.job_id, owner=_owner(), depth=0)
        snapshot.result_payload["exact"].append(2)
        pending = await runtime.pending_outcomes()
        assert pending[0].result_payload is None
        pending[0].adapter["context"].append(3)
        outcome = await runtime.outcome(job.job_id, job.generation)
        assert outcome is not None
        assert outcome.result_payload is None
        outcome.adapter["context"].append(4)
        listed = await runtime.list_jobs(owner=_owner(), depth=0)
        assert listed[0].result_payload is None
        assert listed[0].adapter == {"context": [1]}
        monkeypatch.setattr(runtime_module, "write_json_file_durable", writer)
        waited = await runtime.wait(job.job_id, owner=_owner(), depth=0)
        await runtime.acknowledge_wait(job.job_id, waited.token)
        assert await runtime.outcome(job.job_id, job.generation) is None
        assert await runtime.pending_outcomes() == []
        assert (await runtime.lookup(job.job_id, owner=_owner(), depth=0)).result_payload == {"exact": [1]}
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

    job = await runtime.start(
        "cleanup",
        tool_name="tool",
        depth=0,
        adapter={},
        owner=_owner(),
        operation=operation,
        cancel=cleanup,
    )
    await started.wait()
    cancelling = asyncio.create_task(runtime.cancel(job.job_id, owner=_owner(), depth=0))
    try:
        await cleaning.wait()
        assert (await runtime.lookup(job.job_id, owner=_owner(), depth=0)).status == "cancel_requested"
        waiting = await runtime.wait(job.job_id, owner=_owner(), depth=0, timeout=0)
        assert waiting.token is None
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
    assert settled.result_payload == {"source": expected_source}


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
            await runtime.start("failed", tool_name="tool", depth=0, adapter={}, owner=_owner(), operation=approval)
        waiting = await runtime.wait("failed", owner=_owner(), depth=0)
        await runtime.release_wait("failed", waiting.token)

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
            runtime.continue_job("failed", owner=_owner(), depth=0, expected_generation=0, operation=operation)
            if continuation
            else runtime.start("failed", tool_name="tool", depth=0, adapter={}, owner=_owner(), operation=operation),
        )
    try:
        await writing.wait()
        accepting.cancel()
        release_writer.set()
        with pytest.raises(asyncio.CancelledError):
            await accepting
        monkeypatch.setattr(runtime_module, "write_json_file_durable", original_writer)
        jobs = await runtime.list_jobs(owner=_owner(), depth=0)
        assert calls == 0
        if continuation:
            # The failed continuation never became current, so the approval can still continue exactly once.
            assert [(job.status, job.generation) for job in jobs] == [("awaiting_approval", 0)]
            assert human.has_subscribers
            await runtime.continue_job("failed", owner=_owner(), depth=0, expected_generation=0, operation=operation)
        else:
            assert jobs == []
            assert not human.has_subscribers
            if published:
                # The landed record keeps its identity from running; recovery reports it interrupted.
                with pytest.raises(ValueError, match="already exists"):
                    await runtime.start(
                        "failed",
                        tool_name="tool",
                        depth=0,
                        adapter={},
                        owner=_owner(),
                        operation=operation,
                    )
            else:
                await runtime.start(
                    "failed",
                    tool_name="tool",
                    depth=0,
                    adapter={},
                    owner=_owner(),
                    operation=operation,
                )
        if continuation or not published:
            assert (await runtime.wait("failed", owner=_owner(), depth=0)).job.result == "once"
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
        job = await runtime.start("budget", tool_name="tool", depth=0, adapter={}, owner=_owner(), operation=operation)
        await started.wait()
        waiter = asyncio.create_task(runtime.wait(job.job_id, owner=_owner(), depth=0, timeout=budget))
        if budget is None:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(waiter), 0.02)
            finish.set()
            result = await asyncio.wait_for(waiter, 1)
        else:
            detached = await asyncio.wait_for(waiter, 1)
            assert detached.job.status == "running"
            assert detached.token is None
            finish.set()
            result = await runtime.wait(job.job_id, owner=_owner(), depth=0)
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
        job = await runtime.start("invalid", tool_name="tool", depth=0, adapter={}, owner=_owner(), operation=operation)
        with pytest.raises(ValueError, match="timeout"):
            await runtime.wait(job.job_id, owner=_owner(), depth=0, timeout=budget)  # type: ignore[arg-type]
        assert (await runtime.wait(job.job_id, owner=_owner(), depth=0)).job.result == "saved"
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
            job = await runtime.start(
                "repeat",
                tool_name="tool",
                depth=0,
                adapter={},
                owner=_owner(),
                operation=operation,
            )
        for _ in range(2):
            signal.clear()
            waiter = asyncio.create_task(runtime.wait(job.job_id, owner=_owner(), depth=0))
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(waiter), 0.02)
            signal.notify()
            assert (await asyncio.wait_for(waiter, 1)).job.status == "running"
        signal.clear()
        finish.set()
        assert (await runtime.wait(job.job_id, owner=_owner(), depth=0)).job.result == "saved"
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
        job = await runtime.start(
            "cleanup",
            tool_name="tool",
            depth=0,
            adapter={},
            owner=_owner(),
            operation=operation,
            cancel=cleanup,
        )
        await started.wait()
        result = await runtime.cancel(job.job_id, owner=_owner(), depth=0)
        assert result.status == "failed"
        assert "remote cleanup unavailable" in (result.result or "")
        assert "may still" in (result.result or "")
        assert (await runtime.lookup(job.job_id, owner=_owner(), depth=0)).status == "failed"
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
        await runtime.start(
            name,
            tool_name="tool",
            depth=0,
            adapter={},
            owner=_owner(),
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
        assert (await restored.lookup("first", owner=_owner(), depth=0)).status == "failed"
        assert (await restored.lookup("second", owner=_owner(), depth=0)).status == "interrupted"
        assert cleaned == ["first", "second"]
    finally:
        await restored.shutdown()


@pytest.mark.asyncio
async def test_unsupported_snapshot_schema_fails_enabled_recovery(tmp_path: Path) -> None:
    """Enabled recovery reports a retired snapshot loudly instead of adopting or rewriting it."""
    runtime = tool_job_runtime(tmp_path)

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "saved answer")

    await runtime.start("retired", tool_name="tool", depth=0, adapter={}, owner=_owner(), operation=operation)
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
        job = await runtime.start(
            "draining",
            tool_name="tool",
            depth=0,
            adapter={},
            owner=_owner(),
            operation=operation,
        )
        await started.wait()
        result = await runtime.cancel(job.job_id, owner=_owner(), depth=0)
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

    await runtime.start("teardown", tool_name="tool", depth=0, adapter={}, owner=_owner(), operation=operation)
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
        recovered = await restored.lookup("teardown", owner=_owner(), depth=0)
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
        await runtime.start(
            "self-cancelled",
            tool_name="tool",
            depth=0,
            adapter={},
            owner=_owner(),
            operation=operation,
        )
        waited = await runtime.wait("self-cancelled", owner=_owner(), depth=0)
        assert waited.job.status == "cancelled"
        await runtime.acknowledge_wait("self-cancelled", waited.token)
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

    await runtime.start("claimed", tool_name="tool", depth=0, adapter={}, owner=_owner(), operation=operation)
    entry = runtime._entries["claimed"]
    waiter = asyncio.create_task(runtime.wait("claimed", owner=_owner(), depth=0))
    try:
        while entry.wait_token is None:  # noqa: ASYNC110 - the claim publishes no event to await
            await asyncio.sleep(0)
        await lock.acquire()
        lock.contended.clear()
        waiter.cancel()
        await asyncio.wait_for(lock.contended.wait(), 30)
        waiter.cancel()
        lock.release()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert entry.wait_token is None
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
