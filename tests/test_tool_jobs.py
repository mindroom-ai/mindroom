"""Generic job ownership, scoped discovery, admission, and durable delivery."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from mindroom.tool_jobs import runtime as runtime_module
from mindroom.tool_jobs.control import HumanMessageSignal, job_checkpoint
from mindroom.tool_jobs.runtime import BackgroundOutcome, JobSpec, ToolJobRuntime
from tests.test_background_subagents import _owner

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
async def test_scoped_listing_keeps_delivered_outcomes_across_turns_and_restart(tmp_path: Path) -> None:
    """Discovery survives forgotten handles without exposing another caller or consuming results."""
    runtime = ToolJobRuntime(tmp_path)

    async def completed() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "saved")

    async def running() -> BackgroundOutcome:
        await asyncio.Event().wait()
        raise AssertionError

    first = await runtime.start(
        JobSpec("first", "search", 0, toolkit_name="web", adapter={"run_id": "old"}),
        owner=_owner(),
        operation=completed,
    )
    waiting = await runtime.wait(first.job_id, owner=_owner(), depth=0)
    await runtime.acknowledge_wait(first.job_id, waiting.token)
    await runtime.start(JobSpec("second", "fetch", 0, adapter={"run_id": "new"}), owner=_owner(), operation=running)
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
    restored = ToolJobRuntime(tmp_path)
    try:
        await restored.recover()
        jobs = await restored.list_jobs(owner=_owner(), depth=0)
        assert {job.job_id for job in jobs} == {"first", "second"}
        assert next(job for job in jobs if job.job_id == "first").result == "saved"
    finally:
        await restored.shutdown()


@pytest.mark.asyncio
async def test_failed_admission_rolls_back_without_subscription_or_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A write that never publishes a record must leave a safely retryable exact job ID."""
    runtime = ToolJobRuntime(tmp_path)
    signal = HumanMessageSignal()
    original = runtime_module.write_json_file_durable
    calls = 0

    def failed_write(_path: Path, _payload: object) -> None:
        msg = "disk unavailable"
        raise OSError(msg)

    async def operation() -> BackgroundOutcome:
        nonlocal calls
        calls += 1
        return BackgroundOutcome("completed", "once")

    monkeypatch.setattr(runtime_module, "write_json_file_durable", failed_write)
    with pytest.raises(OSError, match="disk unavailable"):
        await runtime.start(JobSpec("retry", "tool", 0), owner=_owner(), operation=operation, human_signal=signal)
    assert not signal.has_subscribers
    assert await runtime.list_jobs(owner=_owner(), depth=0) == []
    assert calls == 0
    monkeypatch.setattr(runtime_module, "write_json_file_durable", original)
    job = await runtime.start(JobSpec("retry", "tool", 0), owner=_owner(), operation=operation)
    assert (await runtime.wait(job.job_id, owner=_owner(), depth=0)).job.result == "once"
    assert calls == 1
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_old_delivery_ack_cannot_consume_replacement_generation(tmp_path: Path) -> None:
    """An in-flight approval send retains its frozen identity through cancellation."""
    runtime = ToolJobRuntime(tmp_path)

    async def approval() -> BackgroundOutcome:
        return BackgroundOutcome("awaiting_approval")

    job = await runtime.start(JobSpec("approval", "delegate", 0), owner=_owner(), operation=approval)
    waiting = await runtime.wait(job.job_id, owner=_owner(), depth=0)
    await runtime.release_wait(job.job_id, waiting.token)
    claim = await runtime.claim_delivery(job.job_id, content={"body": "approval"}, transaction_id="first")
    assert claim is not None
    await runtime.cancel(job.job_id, owner=_owner(), depth=0, await_completion=True)
    await runtime.acknowledge_delivery(job.job_id, "first", event_id="$old")
    pending = await runtime.pending_deliveries()
    assert len(pending) == 1
    assert pending[0].generation > claim.generation
    assert await runtime.delivery_outcome(job.job_id, claim.generation, claim.transaction_id) is None
    replacement = await runtime.claim_delivery(job.job_id, content={"body": "cancelled"}, transaction_id="second")
    assert replacement is not None
    assert replacement.transaction_id == "second"
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_human_followup_does_not_claim_running_tool_is_paused(tmp_path: Path) -> None:
    """Only a reached cooperative checkpoint reports paused execution."""
    runtime = ToolJobRuntime(tmp_path)
    signal = HumanMessageSignal()
    running, checkpoint = asyncio.Event(), asyncio.Event()

    async def operation() -> BackgroundOutcome:
        running.set()
        await checkpoint.wait()
        await job_checkpoint()
        return BackgroundOutcome("completed", "done")

    job = await runtime.start(JobSpec("work", "tool", 0), owner=_owner(), operation=operation, human_signal=signal)
    await running.wait()
    signal.notify()
    result = await runtime.wait(job.job_id, owner=_owner(), depth=0)
    assert result.job.status == "running"
    assert result.job.human_paused
    checkpoint.set()
    await runtime.resume(job.job_id, owner=_owner(), depth=0)
    assert (await runtime.wait(job.job_id, owner=_owner(), depth=0)).job.result == "done"
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_ambiguous_admission_never_launches_or_accepts_duplicate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An error after replacement retains a non-replayable exact identity through restart."""
    runtime = ToolJobRuntime(tmp_path)
    writer = runtime_module.write_json_file_durable
    calls = 0

    def published_then_failed(path: Path, payload: object) -> None:
        writer(path, payload)
        msg = "durability uncertain"
        raise OSError(msg)

    async def operation() -> BackgroundOutcome:
        nonlocal calls
        calls += 1
        return BackgroundOutcome("completed")

    monkeypatch.setattr(runtime_module, "write_json_file_durable", published_then_failed)
    with pytest.raises(OSError, match="uncertain"):
        await runtime.start(JobSpec("ambiguous", "write", 0), owner=_owner(), operation=operation)
    monkeypatch.setattr(runtime_module, "write_json_file_durable", writer)
    with pytest.raises(ValueError, match="already exists"):
        await runtime.start(JobSpec("ambiguous", "write", 0), owner=_owner(), operation=operation)
    assert (await runtime.lookup("ambiguous", owner=_owner(), depth=0)).status == "interrupted"
    await runtime.shutdown()
    restored = ToolJobRuntime(tmp_path)
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
    runtime = ToolJobRuntime(tmp_path)
    calls = 0

    async def approval() -> BackgroundOutcome:
        return BackgroundOutcome("awaiting_approval", approval_state={"owners": ["shell"]})

    async def continuation() -> BackgroundOutcome:
        nonlocal calls
        calls += 1
        return BackgroundOutcome("completed", "once")

    def failed_write(_path: Path, _payload: object) -> None:
        msg = "write failed"
        raise OSError(msg)

    job = await runtime.start(JobSpec("approval", "tool", 0), owner=_owner(), operation=approval)
    result = await runtime.wait(job.job_id, owner=_owner(), depth=0)
    await runtime.release_wait(job.job_id, result.token)
    writer = runtime_module.write_json_file_durable
    monkeypatch.setattr(runtime_module, "write_json_file_durable", failed_write)
    with pytest.raises(OSError, match="write failed"):
        await runtime.continue_job(job.job_id, owner=_owner(), depth=0, operation=continuation)
    assert (await runtime.lookup(job.job_id, owner=_owner(), depth=0)).status == "awaiting_approval"
    monkeypatch.setattr(runtime_module, "write_json_file_durable", writer)
    await runtime.continue_job(job.job_id, owner=_owner(), depth=0, operation=continuation)
    assert (await runtime.wait(job.job_id, owner=_owner(), depth=0)).job.result == "once"
    assert calls == 1
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_cancel_requested_remains_owned_until_operation_finally_finishes(tmp_path: Path) -> None:
    """Prompt cancellation acknowledgement cannot publish completion before real work drains."""
    runtime = ToolJobRuntime(tmp_path)
    started, cleaning, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def operation() -> BackgroundOutcome:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await finished.wait()
        raise AssertionError

    job = await runtime.start(JobSpec("cancel", "blocking", 0), owner=_owner(), operation=operation)
    await started.wait()
    requested = await runtime.cancel(job.job_id, owner=_owner(), depth=0)
    await cleaning.wait()
    assert requested.status == "cancel_requested"
    assert (await runtime.lookup(job.job_id, owner=_owner(), depth=0)).status == "cancel_requested"
    assert await runtime.pending_deliveries() == []
    finished.set()
    settled = await runtime.cancel(job.job_id, owner=_owner(), depth=0, await_completion=True)
    assert settled.status == "cancelled"
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_reads_are_copies_and_blocked_delivery_does_not_hide_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inspect/list are pure snapshots; recipient mismatch preserves discoverable results."""
    runtime = ToolJobRuntime(tmp_path)

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "answer", result_payload={"exact": [1]})

    job = await runtime.start(JobSpec("read", "tool", 0), owner=_owner(), operation=operation)
    result = await runtime.wait(job.job_id, owner=_owner(), depth=0)
    await runtime.release_wait(job.job_id, result.token)
    claim = await runtime.claim_delivery(job.job_id, content={"body": "answer"}, transaction_id="frozen")
    assert claim is not None
    assert await runtime.delivery_outcome(job.job_id, job.generation, claim.transaction_id) is not None
    await runtime.block_delivery(job.job_id, claim.transaction_id)
    assert await runtime.pending_deliveries() == []
    assert await runtime.delivery_outcome(job.job_id, job.generation, claim.transaction_id) is None
    writer = runtime_module.write_json_file_durable

    def forbid_write(_path: Path, _payload: object) -> None:
        msg = "read attempted durable mutation"
        raise AssertionError(msg)

    monkeypatch.setattr(runtime_module, "write_json_file_durable", forbid_write)
    snapshot = await runtime.lookup(job.job_id, owner=_owner(), depth=0)
    snapshot.result_payload["exact"].append(2)
    listed = await runtime.list_jobs(owner=_owner(), depth=0)
    assert listed[0].result_payload == {"exact": [1]}
    assert listed[0].delivery.disposition == "recipient_mismatch"
    monkeypatch.setattr(runtime_module, "write_json_file_durable", writer)
    waited = await runtime.wait(job.job_id, owner=_owner(), depth=0)
    await runtime.acknowledge_wait(job.job_id, waited.token)
    assert await runtime.delivery_outcome(job.job_id, job.generation, claim.transaction_id) is None
    await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_status", [None, "cancelled", "completed"])
async def test_terminal_operation_stays_pending_until_cancellation_cleanup_settles(
    tmp_path: Path,
    cleanup_status: str | None,
) -> None:
    """A cancellation-catching operation cannot make its result deliverable before cleanup."""
    runtime = ToolJobRuntime(tmp_path)
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

    job = await runtime.start(JobSpec("cleanup", "tool", 0), owner=_owner(), operation=operation, cancel=cleanup)
    await started.wait()
    cancelling = asyncio.create_task(runtime.cancel(job.job_id, owner=_owner(), depth=0, await_completion=True))
    try:
        await cleaning.wait()
        assert (await runtime.lookup(job.job_id, owner=_owner(), depth=0)).status == "cancel_requested"
        waiting = await runtime.wait(job.job_id, owner=_owner(), depth=0, timeout=0)
        assert waiting.token is None
        assert waiting.job.status == "cancel_requested"
        assert await runtime.pending_deliveries() == []
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
    """Parent cancellation must not hide writer failure or prevent exact admission rollback."""
    runtime = ToolJobRuntime(tmp_path)
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
        await runtime.start(JobSpec("failed", "tool", 0), owner=_owner(), operation=approval, human_signal=human)
        waiting = await runtime.wait("failed", owner=_owner(), depth=0)
        await runtime.release_wait("failed", waiting.token)

    def failed_writer(path: Path, payload: object) -> None:
        if published:
            original_writer(path, payload)
        loop.call_soon_threadsafe(writing.set)
        assert release_writer.wait(5)
        msg = "admission write failed"
        raise OSError(msg)

    monkeypatch.setattr(runtime_module, "write_json_file_durable", failed_writer)
    accepting = asyncio.create_task(
        runtime.continue_job("failed", owner=_owner(), depth=0, operation=operation)
        if continuation
        else runtime.start(JobSpec("failed", "tool", 0), owner=_owner(), operation=operation, human_signal=human),
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
        if published:
            assert len(jobs) == 1
            assert jobs[0].status == "interrupted"
            assert not human.has_subscribers
        elif continuation:
            assert len(jobs) == 1
            assert jobs[0].status == "awaiting_approval"
            assert jobs[0].generation == 0
            assert human.has_subscribers
            await runtime.continue_job("failed", owner=_owner(), depth=0, operation=operation)
        else:
            assert jobs == []
            assert not human.has_subscribers
            await runtime.start(JobSpec("failed", "tool", 0), owner=_owner(), operation=operation)
        if not published:
            assert (await runtime.wait("failed", owner=_owner(), depth=0)).job.result == "once"
            assert calls == 1
    finally:
        release_writer.set()
        monkeypatch.setattr(runtime_module, "write_json_file_durable", original_writer)
        await runtime.shutdown()
