"""Generic job ownership, scoped discovery, admission, and durable consumption."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
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
    intercept_job_saves,
    job_owner,
    lookup,
    pending_outcome,
    pending_outcomes,
    saved_jobs,
    saved_payload,
    start_job,
    tool_job_journal,
    tool_job_runtime,
    wait_for_status,
    write_saved_job,
)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
async def test_wait_rejects_unrepresentable_timeout_before_lookup(tmp_path: Path) -> None:
    """Both entry points reject budgets that cannot be represented on the event-loop clock."""
    runtime = await tool_job_runtime(tmp_path)
    try:
        with pytest.raises(ValueError, match="finite"):
            await runtime.wait("unknown", owner=job_owner(), depth=0, timeout=10**400)
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_final_shutdown_waits_for_receipt_publication(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A replacement owner must never recover before the old owner's last receipt lands."""
    runtime = await tool_job_runtime(tmp_path)

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "saved answer")

    await start_job(runtime, "receipt", tool_name="tool", depth=0, adapter={}, owner=job_owner(), operation=operation)
    waited = await runtime.wait("receipt", owner=job_owner(), depth=0)
    await runtime.quiesce()
    writing, closing, release_writer = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def blocked_save(_job: dict[str, object], _payload: object) -> None:
        writing.set()
        await release_writer.wait()

    async def close() -> None:
        closing.set()
        await runtime.shutdown()

    intercept_job_saves(monkeypatch, before=blocked_save)
    acknowledging = asyncio.create_task(runtime.acknowledge_wait("receipt", waited.claim))
    await asyncio.wait_for(writing.wait(), 30)
    shutdown = asyncio.create_task(close())
    try:
        await closing.wait()
        await asyncio.sleep(0)
        assert not shutdown.done()
    finally:
        release_writer.set()
        await asyncio.gather(acknowledging, shutdown)
    restored = await tool_job_runtime(tmp_path)
    try:
        await restored.recover()
        assert pending_outcomes(restored) == []
    finally:
        await restored.shutdown()


@pytest.mark.asyncio
async def test_quiescence_fences_control_but_retains_receipts_and_storage(tmp_path: Path) -> None:
    """No new execution or cleanup races a shutdown drain; finalizers may still acknowledge."""
    runtime = await tool_job_runtime(tmp_path)

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "done")

    try:
        job = await start_job(
            runtime,
            "quiesced",
            tool_name="tool",
            depth=0,
            adapter={},
            owner=job_owner(),
            operation=operation,
        )
        waited = await runtime.wait(job.job_id, owner=job_owner(), depth=0)
        await runtime.quiesce()
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
            await runtime.cancel("quiesced", owner=job_owner(), depth=0)
        await runtime.acknowledge_wait(job.job_id, waited.claim)
        assert (await lookup(runtime, job.job_id, owner=job_owner(), depth=0)).consumed
    finally:
        await runtime.shutdown()
    restored = await tool_job_runtime(tmp_path)
    try:
        await restored.recover()
        assert pending_outcomes(restored) == []
        assert (await lookup(restored, job.job_id, owner=job_owner(), depth=0)).status == "completed"
    finally:
        await restored.shutdown()


@pytest.mark.asyncio
async def test_saved_outcome_keeps_its_payload_only_in_the_journal(tmp_path: Path) -> None:
    """Job metadata stays in memory, while the payload lives in the journal beside it and is read on demand."""
    runtime = await tool_job_runtime(tmp_path)
    value = "summary " * 100 + "payload tail"
    payload = encode_result_payload(ToolResultPayload(value))

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome("completed", value, result_payload=payload)

    try:
        await start_job(runtime, "disk", tool_name="tool", depth=0, adapter={}, owner=job_owner(), operation=operation)
        waited = await runtime.wait("disk", owner=job_owner(), depth=0)
        assert await saved_payload(tmp_path, "disk") == payload
        assert (await saved_jobs(tmp_path))["disk"].result is not None
        assert "payload tail" not in (await saved_jobs(tmp_path))["disk"].result
        assert "payload tail" not in repr(runtime._entries["disk"])
        assert (await read_result_payload(runtime, waited.job)).value == value
        await runtime.acknowledge_wait("disk", waited.claim)
        assert "payload tail" not in repr(runtime._entries["disk"])
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["acknowledge", "stop", "cancel"])
async def test_settled_payload_is_written_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str) -> None:
    """Acknowledgement, Stop, cancelling settled work, and restart recovery rewrite only job metadata."""
    runtime = await tool_job_runtime(tmp_path)
    payload = encode_result_payload(ToolResultPayload("saved"))
    payload_saves: list[object] = []

    def count_payload(_job: dict[str, object], saved: object) -> None:
        if saved is not None:
            payload_saves.append(saved)

    intercept_job_saves(monkeypatch, before=count_payload)

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "saved", result_payload=payload)

    async def every_job(_job: runtime_module.BackgroundJob) -> bool:
        return True

    try:
        await start_job(runtime, "once", tool_name="tool", depth=0, adapter={}, owner=job_owner(), operation=operation)
        waited = await runtime.wait("once", owner=job_owner(), depth=0)
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
    restored = await tool_job_runtime(tmp_path)
    try:
        await restored.recover()
        job = await lookup(restored, "once", owner=job_owner(), depth=0)
        assert job.consumed is (action == "acknowledge")
        assert (job.user_stop_receipt_order is not None) is (action == "stop")
        assert (await read_result_payload(restored, job)).value == "saved"
    finally:
        await restored.shutdown()
    assert payload_saves == [payload]


@pytest.mark.asyncio
async def test_crash_before_the_outcome_save_recovers_interrupted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An outcome and its payload save together, so a crash before that save leaves the job running for recovery."""
    runtime = await tool_job_runtime(tmp_path)
    executions = 0

    def die_before_outcome(job: dict[str, object], _payload: object) -> None:
        if job["status"] == "completed":
            msg = "process died"
            raise OSError(msg)

    async def operation() -> BackgroundOutcome:
        nonlocal executions
        executions += 1
        return BackgroundOutcome("completed", "lost", result_payload=encode_result_payload(ToolResultPayload("lost")))

    with monkeypatch.context() as patch:
        intercept_job_saves(patch, before=die_before_outcome)
        await start_job(runtime, "crash", tool_name="tool", depth=0, adapter={}, owner=job_owner(), operation=operation)
        await wait_for_status(runtime, "crash", "completed")
    assert (await saved_jobs(tmp_path))["crash"].status == "running"
    assert await saved_payload(tmp_path, "crash") is None
    # The process dies without an orderly shutdown; the next one takes the jobs over.
    restored = await tool_job_runtime(tmp_path)
    try:
        await restored.recover()
        recovered = await lookup(restored, "crash", owner=job_owner(), depth=0)
        assert recovered.status == "interrupted"
        assert await saved_payload(tmp_path, "crash") is None
        assert executions == 1
    finally:
        await restored.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("acknowledged", [False, True])
async def test_restart_reattaches_a_saved_result_without_rewrites_or_rerun(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    acknowledged: bool,
) -> None:
    """Recovery rewrites no saved job, and reattaching the same call returns its result instead of running it."""
    runtime = await tool_job_runtime(tmp_path)
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
    saved = await saved_jobs(tmp_path)
    rewrites: list[object] = []
    intercept_job_saves(monkeypatch, before=lambda job, _payload: rewrites.append(job))
    restored = await tool_job_runtime(tmp_path)
    try:
        await restored.recover()
        assert rewrites == []
        assert await saved_jobs(tmp_path) == saved
        assert bool(pending_outcomes(restored)) is not acknowledged
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
    """Shutdown must wait until an accepted terminal retry has finished writing."""
    runtime = await tool_job_runtime(tmp_path)

    async def operation() -> BackgroundOutcome:
        await asyncio.Event().wait()
        raise AssertionError

    def fail_terminal(job: dict[str, object], _payload: object) -> None:
        if job["status"] == "cancelled":
            message = "terminal save failed"
            raise OSError(message)

    await start_job(runtime, "retry", tool_name="tool", depth=0, adapter={}, owner=job_owner(), operation=operation)
    with monkeypatch.context() as patch:
        intercept_job_saves(patch, before=fail_terminal)
        with pytest.raises(OSError, match="terminal save failed"):
            await runtime.cancel("retry", owner=job_owner(), depth=0)

    retry_started, release_retry = asyncio.Event(), asyncio.Event()
    original_publish = runtime._publish

    async def publish(
        entry: runtime_module._Entry,
        job: runtime_module.BackgroundJob,
        payload: runtime_module.EncodedResultPayload | None = None,
    ) -> None:
        if not retry_started.is_set():
            retry_started.set()
            await release_retry.wait()
        await original_publish(entry, job, payload)

    monkeypatch.setattr(runtime, "_publish", publish)
    retrying = asyncio.create_task(runtime.cancel("retry", owner=job_owner(), depth=0))
    await retry_started.wait()
    stopping = asyncio.create_task(runtime.shutdown())
    try:
        done, _pending = await asyncio.wait({stopping}, timeout=0.2)
        assert not done, "shutdown finished while a terminal cancellation retry was still running"
    finally:
        release_retry.set()
        await asyncio.gather(retrying, stopping)
    saved = (await saved_jobs(tmp_path))["retry"]
    assert saved.status == "cancelled"


@pytest.mark.asyncio
async def test_failed_cancellation_save_wakes_an_existing_waiter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A terminal outcome must wake its claim owner even when the durable write fails."""
    runtime = await tool_job_runtime(tmp_path)
    cleaning, release = asyncio.Event(), asyncio.Event()

    async def operation() -> BackgroundOutcome:
        await asyncio.Event().wait()
        raise AssertionError

    async def cleanup(_job: runtime_module.BackgroundJob) -> None:
        cleaning.set()
        await release.wait()

    def fail_terminal(job: dict[str, object], _payload: object) -> None:
        if job["status"] == "cancelled":
            message = "terminal save failed"
            raise OSError(message)

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
            intercept_job_saves(patch, before=fail_terminal)
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
        saved = (await saved_jobs(tmp_path))["wake"]
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
    runtime = await tool_job_runtime(tmp_path)

    async def operation() -> BackgroundOutcome:
        await asyncio.Event().wait()
        raise AssertionError

    def fail_terminal(job: dict[str, object], _payload: object) -> None:
        if job["status"] == "cancelled":
            message = "terminal save failed"
            raise OSError(message)

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
            intercept_job_saves(patch, before=fail_terminal)
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
    runtime = await tool_job_runtime(tmp_path)

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
        assert (await lookup(runtime, "reread", owner=job_owner(), depth=0)).result == "saved"
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_result_expiry_deletes_only_old_consumed_jobs_with_finished_sources(tmp_path: Path) -> None:
    """Expiry deletes an old consumed job's files and entry; approval-owned, unread, recent, and claimed work stays."""
    runtime = await tool_job_runtime(tmp_path)
    payload = encode_result_payload(ToolResultPayload("saved"))

    async def completed() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "saved", result_payload=payload)

    async def source_finished(job: runtime_module.BackgroundJob) -> bool:
        return job.job_id != "approval"

    cutoff = datetime.now(UTC) - timedelta(days=30)
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
        assert "expire" not in await saved_jobs(tmp_path)
        assert await saved_payload(tmp_path, "expire") is None
        with pytest.raises(runtime_module.JobAccessError, match="not available"):
            await lookup(runtime, "expire", owner=job_owner(), depth=0)
        for name in kept:
            assert await runtime.read_payload(await lookup(runtime, name, owner=job_owner(), depth=0)) == payload
        assert [job.job_id for job in pending_outcomes(runtime)] == ["unread"]
    finally:
        if claimed is not None:
            await runtime.release_wait("claimed", claimed.claim)
        await runtime.shutdown()
    restored = await tool_job_runtime(tmp_path)
    try:
        await restored.recover()
        assert set(restored._entries) == set(kept)
        assert [await saved_payload(tmp_path, name) for name in kept] == [payload] * len(kept)
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

    runtime = await tool_job_runtime(tmp_path)
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
    restored = await tool_job_runtime(tmp_path)
    try:
        await restored.recover()
        assert await lookups(restored) == (["b"], ["b", "c", "unsourced"])
    finally:
        await restored.shutdown()


@pytest.mark.asyncio
async def test_scoped_listing_keeps_consumed_outcomes_across_turns_and_restart(tmp_path: Path) -> None:
    """Discovery survives forgotten handles without exposing another caller or consuming results."""
    runtime = await tool_job_runtime(tmp_path)

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
    restored = await tool_job_runtime(tmp_path)
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
    runtime = await tool_job_runtime(tmp_path)

    async def completed() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "saved")

    for job_id in ("zolder", "anewer"):
        await start_job(runtime, job_id, tool_name="read", depth=0, adapter={}, owner=job_owner(), operation=completed)
        waited = await runtime.wait(job_id, owner=job_owner(), depth=0)
        await runtime.acknowledge_wait(job_id, waited.claim)
    before = await runtime.list_jobs(owner=job_owner(), depth=0)
    assert [job.job_id for job in before] == ["anewer", "zolder"]
    await runtime.shutdown()
    restored = await tool_job_runtime(tmp_path)
    try:
        await restored.recover()
        after = await restored.list_jobs(owner=job_owner(), depth=0)
        assert [(job.job_id, job.updated_at) for job in after] == [(job.job_id, job.updated_at) for job in before]
    finally:
        await restored.shutdown()


@pytest.mark.asyncio
async def test_failed_admission_leaves_no_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A write that never publishes a record must leave a safely retryable exact job ID."""
    runtime = await tool_job_runtime(tmp_path)
    try:
        calls = 0

        def failed_write(_job: dict[str, object], _payload: object) -> None:
            msg = "disk unavailable"
            raise OSError(msg)

        async def operation() -> BackgroundOutcome:
            nonlocal calls
            calls += 1
            return BackgroundOutcome("completed", "once")

        intercept_job_saves(monkeypatch, before=failed_write)
        with pytest.raises(OSError, match="disk unavailable"):
            await start_job(
                runtime,
                "retry",
                tool_name="tool",
                depth=0,
                adapter={},
                owner=job_owner(),
                operation=operation,
            )
        assert await runtime.list_jobs(owner=job_owner(), depth=0) == []
        assert calls == 0
        intercept_job_saves(monkeypatch)
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
async def test_human_followup_releases_wait_without_pausing_next_tool(tmp_path: Path) -> None:
    """A human follow-up releases only the waiter while one execution crosses later checkpoints."""
    runtime = await tool_job_runtime(tmp_path)
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
        with human_message_signal_context(signal):
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
    runtime = await tool_job_runtime(tmp_path)
    calls = 0

    def published_then_failed(_job: dict[str, object], _payload: object) -> None:
        msg = "durability uncertain"
        raise OSError(msg)

    async def operation() -> BackgroundOutcome:
        nonlocal calls
        calls += 1
        return BackgroundOutcome("completed")

    intercept_job_saves(monkeypatch, after=published_then_failed)
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
    intercept_job_saves(monkeypatch)
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
        await lookup(runtime, "ambiguous", owner=job_owner(), depth=0)
    await runtime.shutdown()
    restored = await tool_job_runtime(tmp_path)
    try:
        await restored.recover()
        assert (await lookup(restored, "ambiguous", owner=job_owner(), depth=0)).status == "interrupted"
        assert calls == 0
    finally:
        await restored.shutdown()


@pytest.mark.asyncio
async def test_cancel_requested_remains_owned_until_operation_finally_finishes(tmp_path: Path) -> None:
    """A durable cancellation request cannot publish completion before real work drains."""
    runtime = await tool_job_runtime(tmp_path)
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
    assert (await lookup(runtime, job.job_id, owner=job_owner(), depth=0)).status == "cancel_requested"
    assert pending_outcomes(runtime) == []
    assert not cancelling.done()
    finished.set()
    settled = await cancelling
    assert settled.status == "cancelled"
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_repeated_cancel_joins_the_in_flight_drain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A second cancellation awaits the same drain and reports the settled job, not the request."""
    runtime = await tool_job_runtime(tmp_path)
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
    runtime = await tool_job_runtime(tmp_path)
    started, writing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def operation() -> BackgroundOutcome:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError

    async def blocked_request(job: dict[str, object], _payload: object) -> None:
        if job["status"] == "cancel_requested":
            writing.set()
            await release.wait()

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
        intercept_job_saves(monkeypatch, before=blocked_request)
        cancelling = asyncio.create_task(runtime.cancel("caller", owner=job_owner(), depth=0))
        await writing.wait()
        cancelling.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await cancelling
        await asyncio.wait_for(wait_for_status(runtime, "caller", "cancelled"), 30)
        assert (await saved_jobs(tmp_path))["caller"].status == "cancelled"
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
    runtime = await tool_job_runtime(tmp_path)
    started = asyncio.Event()
    cleanup_calls = 0

    async def operation() -> BackgroundOutcome:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError

    async def cleanup(_job: runtime_module.BackgroundJob) -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1

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
        saved = (await saved_jobs(tmp_path))["retry-cancel"]
        assert settled.status == "cancelled"
        assert saved.status == "cancelled"
        assert cleanup_calls == 1
    finally:
        runtime._publish = original_publish
        task = runtime._entries[job.job_id].task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_failed_receipt_save_preserves_the_previous_saved_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A receipt save that fails changes nothing saved, and the claim can still acknowledge."""
    runtime = await tool_job_runtime(tmp_path)

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "saved")

    def fail_receipt(job: dict[str, object], _payload: object) -> None:
        if job["consumed"]:
            message = "receipt save failed"
            raise OSError(message)

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
        before = await saved_jobs(tmp_path)
        with monkeypatch.context() as patch:
            intercept_job_saves(patch, before=fail_receipt)
            with pytest.raises(OSError, match="receipt save failed"):
                await runtime.acknowledge_wait("atomic", waited.claim)
            assert await saved_jobs(tmp_path) == before
        await runtime.acknowledge_wait("atomic", waited.claim)
        assert (await saved_jobs(tmp_path))["atomic"].consumed
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_cancellation_retry_propagates_failed_terminal_persistence(tmp_path: Path) -> None:
    """A cancellation retry reports its own failed terminal persistence instead of success."""
    runtime = await tool_job_runtime(tmp_path)
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
        saved = (await saved_jobs(tmp_path))["retry-terminal"]
        assert saved.status == "cancel_requested"
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
    runtime = await tool_job_runtime(tmp_path)
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
    runtime = await tool_job_runtime(tmp_path)
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

        def forbid_write(_job: dict[str, object], _payload: object) -> None:
            msg = "read attempted durable mutation"
            raise AssertionError(msg)

        intercept_job_saves(monkeypatch, before=forbid_write)
        snapshot = await lookup(runtime, job.job_id, owner=job_owner(), depth=0)
        snapshot.adapter["context"].append(2)
        (await runtime.read_payload(snapshot))["exact"].append(2)
        listed = await runtime.list_jobs(owner=job_owner(), depth=0)
        assert listed[0].adapter == {"context": [1]}
        intercept_job_saves(monkeypatch)
        waited = await runtime.wait(job.job_id, owner=job_owner(), depth=0)
        await runtime.acknowledge_wait(job.job_id, waited.claim)
        assert pending_outcome(runtime, job.job_id) is None
        assert pending_outcomes(runtime) == []
        assert await runtime.read_payload(await lookup(runtime, job.job_id, owner=job_owner(), depth=0)) == {
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
    runtime = await tool_job_runtime(tmp_path)
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
        assert (await lookup(runtime, job.job_id, owner=job_owner(), depth=0)).status == "cancel_requested"
        waiting = await runtime.wait(job.job_id, owner=job_owner(), depth=0, timeout=0)
        assert waiting.claim is None
        assert waiting.job.status == "cancel_requested"
        assert pending_outcomes(runtime) == []
        assert not cancelling.done()
    finally:
        finish_cleanup.set()
        settled = await cancelling
        await runtime.shutdown()
    assert settled.status == "completed"
    expected_source = "cleanup" if cleanup_status == "completed" else "operation"
    expected_answer = "reconciled answer" if cleanup_status == "completed" else "operation answer"
    assert settled.result == expected_answer
    assert await saved_payload(tmp_path, "cleanup") == {"source": expected_source}


@pytest.mark.asyncio
@pytest.mark.parametrize("published", [False, True])
async def test_cancelled_parent_and_failed_admission_reconcile_acceptance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    published: bool,
) -> None:
    """Parent cancellation must not hide writer failure or leave a failed admission half-published in memory."""
    runtime = await tool_job_runtime(tmp_path)
    writing, release_writer = asyncio.Event(), asyncio.Event()
    calls = 0

    async def operation() -> BackgroundOutcome:
        nonlocal calls
        calls += 1
        return BackgroundOutcome("completed", "once")

    async def failed_writer(_job: dict[str, object], _payload: object) -> None:
        writing.set()
        await release_writer.wait()
        msg = "admission write failed"
        raise OSError(msg)

    if published:
        intercept_job_saves(monkeypatch, after=failed_writer)
    else:
        intercept_job_saves(monkeypatch, before=failed_writer)
    accepting = asyncio.create_task(
        runtime.start("failed", tool_name="tool", depth=0, adapter={}, owner=job_owner(), operation=operation),
    )
    try:
        await writing.wait()
        accepting.cancel()
        release_writer.set()
        with pytest.raises(asyncio.CancelledError):
            await accepting
        intercept_job_saves(monkeypatch)
        jobs = await runtime.list_jobs(owner=job_owner(), depth=0)
        assert calls == 0
        assert jobs == []
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
        if not published:
            assert (await runtime.wait("failed", owner=job_owner(), depth=0)).job.result == "once"
            assert calls == 1
    finally:
        release_writer.set()
        intercept_job_saves(monkeypatch)
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("budget", [None, 0, 0.01])
async def test_wait_budget_never_cancels_owned_execution(tmp_path: Path, budget: float | None) -> None:
    """Unlimited waits stay pending; finite waits detach and every mode preserves one operation."""
    runtime = await tool_job_runtime(tmp_path)
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
    runtime = await tool_job_runtime(tmp_path)

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
    runtime = await tool_job_runtime(tmp_path)
    signal = HumanMessageSignal()
    finish = asyncio.Event()

    async def operation() -> BackgroundOutcome:
        await finish.wait()
        return BackgroundOutcome("completed", "saved")

    try:
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
            with human_message_signal_context(signal):
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
    runtime = await tool_job_runtime(tmp_path)
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
        assert (await lookup(runtime, job.job_id, owner=job_owner(), depth=0)).status == "failed"
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("recovery", [False, True])
async def test_shutdown_cleanup_failure_does_not_strand_other_jobs(tmp_path: Path, *, recovery: bool) -> None:
    """One failing cleanup cannot prevent later executions from settling and releasing the lease."""
    runtime = await tool_job_runtime(tmp_path)
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
    snapshots = await saved_jobs(tmp_path)
    await runtime.shutdown()
    assert cleaned == ["first", "second"]
    if recovery:
        # The process died before shutdown saved anything, leaving the running snapshots for recovery.
        for job_id, snapshot in snapshots.items():
            await write_saved_job(tmp_path, job_id, json.dumps(asdict(snapshot)))
        cleaned.clear()
    restored = await tool_job_runtime(tmp_path, cancel=cleanup)
    try:
        await restored.recover()
        assert (await lookup(restored, "first", owner=job_owner(), depth=0)).status == "failed"
        assert (await lookup(restored, "second", owner=job_owner(), depth=0)).status == "interrupted"
        assert cleaned == ["first", "second"]
    finally:
        await restored.shutdown()


@pytest.mark.asyncio
async def test_invalid_saved_snapshot_fails_enabled_recovery(tmp_path: Path) -> None:
    """Enabled recovery reports a snapshot it cannot read loudly instead of adopting or rewriting it."""
    runtime = await tool_job_runtime(tmp_path)

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "saved answer")

    await start_job(runtime, "retired", tool_name="tool", depth=0, adapter={}, owner=job_owner(), operation=operation)
    await runtime.shutdown()
    retired = json.dumps({**asdict((await saved_jobs(tmp_path))["retired"]), "retired_field": 1})
    await write_saved_job(tmp_path, "retired", retired)
    restored = await tool_job_runtime(tmp_path)
    try:
        with pytest.raises(ValueError, match="Invalid tool job snapshot"):
            await restored.recover()
        journal = tool_job_journal(tmp_path)
        try:
            assert [saved.job_json for saved in await journal.saved_tool_jobs()] == [retired]
        finally:
            await journal.close()
    finally:
        await restored.shutdown()


@pytest.mark.asyncio
async def test_failure_while_draining_cancellation_is_not_reported_cancelled(tmp_path: Path) -> None:
    """An operation's failing finalizer remains visible after its cancellation request."""
    runtime = await tool_job_runtime(tmp_path)
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
    runtime = await tool_job_runtime(tmp_path)
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
    assert (await saved_jobs(tmp_path))["teardown"].status == "running"
    # The process dies with its loop, without an orderly shutdown; the next process takes over its jobs.
    restored = await tool_job_runtime(tmp_path)
    try:
        await restored.recover()
        recovered = await lookup(restored, "teardown", owner=job_owner(), depth=0)
        assert recovered.status == "interrupted"
        assert "runtime restart" in (recovered.result or "")
    finally:
        await restored.shutdown()


@pytest.mark.asyncio
async def test_operation_raising_cancellation_itself_settles_cancelled(tmp_path: Path) -> None:
    """A CancelledError the operation raises without a task cancellation request still settles and wakes waiters."""
    runtime = await tool_job_runtime(tmp_path)

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
        saved = (await saved_jobs(tmp_path))["self-cancelled"]
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
    runtime = await tool_job_runtime(tmp_path)
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
        assert pending_outcome(runtime, "claimed") is not None
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
    runtime = await tool_job_runtime(tmp_path, authorize=lambda _job: not revoked)
    failing = False

    def failing_writer(job: dict[str, object], _payload: object) -> None:
        if failing and job["job_id"] == "first":
            msg = "injected durable write failure"
            raise OSError(msg)

    async def operation() -> BackgroundOutcome:
        await asyncio.Event().wait()
        raise AssertionError

    async def every_job(_job: runtime_module.BackgroundJob) -> bool:
        return True

    intercept_job_saves(monkeypatch, before=failing_writer)
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
        assert (await lookup(runtime, "first", owner=job_owner(), depth=0)).status == "running"
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

    runtime = await tool_job_runtime(tmp_path, cancel=cleanup)
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
        assert (await lookup(runtime, "held", owner=job_owner(), depth=0)).status == "cancel_requested"
    finally:
        release.set()
        blocked = False
        await runtime.shutdown()
