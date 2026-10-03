"""Behavioral checks for durable ownership of background delegation turns."""

from __future__ import annotations

import asyncio
import gc
import json
import weakref
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING

import pytest
from agno.tools.function import FunctionCall
from agno.tools.toolkit import Toolkit

from mindroom.delegation.background import (
    delegation_child,
    delegation_outcome,
    delegation_result,
    reconcile_delegation,
    start_delegation,
)
from mindroom.delegation.sessions import subagent_liveness
from mindroom.hooks import HookRegistry
from mindroom.tool_jobs import runtime as background
from mindroom.tool_jobs.control import (
    HumanMessageSignal,
    JobControl,
    human_message_signal_context,
    job_checkpoint,
    job_control_context,
    job_stopped_by_shutdown,
)
from mindroom.tool_jobs.runtime import BackgroundOutcome
from mindroom.tool_system import tool_hooks
from tests.conftest import test_runtime_paths
from tests.tool_job_helpers import (
    JOB_TEST_TIMEOUT,
    awaiting_approval,
    intercept_job_saves,
    job_child,
    job_owner,
    keep_child,
    lookup,
    pending_outcome,
    pending_outcomes,
    saved_jobs,
    saved_payload,
    start_delegation_job,
    tool_job_runtime,
    wait_for_status,
    write_saved_job,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.delegation.state import DelegationChild


@pytest.mark.asyncio
async def test_consumed_native_result_releases_live_child_and_discovery_payload(tmp_path: Path) -> None:
    """Consumption releases native objects while the exact formatted result remains readable."""
    runtime = await tool_job_runtime(tmp_path)
    raw = "native output " * 65536
    delivered = raw + "\n\nSaved native receipt"

    async def start() -> weakref.ReferenceType[DelegationChild]:
        child = job_child()

        async def operation() -> BackgroundOutcome:
            child.status = "completed"
            child.result = raw
            return delegation_outcome("completed", delivered)

        await start_delegation_job(runtime, child, owner=job_owner(), operation=operation)
        return weakref.ref(child)

    try:
        child_ref = await start()
        job_id = job_child().delegation_id
        waited = await runtime.wait(job_id, owner=job_owner(), depth=0)
        assert await delegation_result(runtime, waited.job) == delivered
        await runtime.acknowledge_wait(job_id, waited.claim)
        gc.collect()
        assert child_ref() is None
        discovered = await runtime.list_jobs(owner=job_owner(), depth=0)
        assert len(json.dumps([asdict(job) for job in discovered])) < 8192
        reread = await runtime.wait(job_id, owner=job_owner(), depth=0)
        assert await delegation_result(runtime, reread.job) == delivered
        await runtime.acknowledge_wait(job_id, reread.claim)
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_native_result_expiry_after_restart_deletes_the_job(tmp_path: Path) -> None:
    """A recovered native result stays small in discovery and expiry deletes its metadata and payload files."""
    runtime = await tool_job_runtime(tmp_path)
    child = job_child()
    raw = "native output " * 65536
    delivered = raw + "\n\nSaved native receipt"

    async def operation() -> BackgroundOutcome:
        child.status = "completed"
        child.result = raw
        return delegation_outcome("completed", delivered)

    await start_delegation_job(runtime, child, owner=job_owner(), operation=operation)
    waited = await runtime.wait(child.delegation_id, owner=job_owner(), depth=0)
    await runtime.acknowledge_wait(child.delegation_id, waited.claim)
    await runtime.shutdown()
    restored = await tool_job_runtime(tmp_path)

    async def source_finished(_job: background.BackgroundJob) -> bool:
        return True

    try:
        await restored.recover()
        saved = await lookup(restored, child.delegation_id, owner=job_owner(), depth=0)
        assert await delegation_result(restored, saved) == delivered
        discovered = await restored.list_jobs(owner=job_owner(), depth=0)
        assert len(json.dumps([asdict(job) for job in discovered])) < 8192
        await restored.expire_consumed(
            before=datetime.now(UTC) + timedelta(days=31),
            source_finished=source_finished,
        )
        assert child.delegation_id not in restored._entries
        assert child.delegation_id not in await saved_jobs(tmp_path)
        assert await saved_payload(tmp_path, child.delegation_id) is None
    finally:
        await restored.shutdown()


@pytest.mark.asyncio
async def test_cancellation_after_native_settlement_keeps_its_outcome(tmp_path: Path) -> None:
    """Cancellation after native settlement preserves an outcome the generic job never published."""
    runtime = await tool_job_runtime(tmp_path)
    child = job_child()
    settled = asyncio.Event()

    async def operation() -> BackgroundOutcome:
        child.status = "completed"
        child.result = "Native completion before generic settlement"
        settled.set()
        await asyncio.Event().wait()
        pytest.fail("Cancellation should interrupt generic settlement")

    try:
        await start_delegation_job(runtime, child, owner=job_owner(), operation=operation)
        await settled.wait()
        result = await runtime.cancel(child.delegation_id, owner=job_owner(), depth=0)
        assert result.status == "completed"
        assert result.result == "Native completion before generic settlement"
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_timeout_and_cancelled_waiter_leave_one_child_alive(tmp_path: Path) -> None:
    """Foreground abandonment must never cancel or restart an owned operation."""
    runtime = await tool_job_runtime(tmp_path)
    started, finish = asyncio.Event(), asyncio.Event()
    calls = 0

    async def operation() -> BackgroundOutcome:
        nonlocal calls
        calls += 1
        started.set()
        await finish.wait()
        return BackgroundOutcome("completed", "answer")

    try:
        job = await start_delegation_job(runtime, job_child(), owner=job_owner(), operation=operation)
        await started.wait()
        first = await runtime.wait(job.job_id, owner=job_owner(), depth=0, timeout=0)
        assert first.job.status == "running"
        entered = asyncio.Event()

        async def wait_for_result() -> background.JobWait:
            entered.set()
            return await runtime.wait(job.job_id, owner=job_owner(), depth=0)

        waiter = asyncio.create_task(wait_for_result())
        await asyncio.wait_for(entered.wait(), 30)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        finish.set()
        result = await runtime.wait(job.job_id, owner=job_owner(), depth=0)
        assert result.job.result == "answer"
        assert calls == 1
        assert pending_outcomes(runtime) == []
        await runtime.acknowledge_wait(job.job_id, result.claim)
    finally:
        finish.set()
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_wait_claim_released_without_ack_keeps_outcome_pending(tmp_path: Path) -> None:
    """An abandoned result lease remains eligible for a later serialized consumer."""
    runtime = await tool_job_runtime(tmp_path)

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "done")

    try:
        job = await start_delegation_job(runtime, job_child(), owner=job_owner(), operation=operation)
        result = await runtime.wait(job.job_id, owner=job_owner(), depth=0)
        assert result.claim is not None
        assert pending_outcomes(runtime) == []
        await runtime.release_wait(job.job_id, result.claim)
        assert [item.job_id for item in pending_outcomes(runtime)] == [job.job_id]
        waiting = await runtime.wait(job.job_id, owner=job_owner(), depth=0)
        assert waiting.claim is not None
        assert pending_outcomes(runtime) == []
        await runtime.acknowledge_wait(job.job_id, waiting.claim)
        assert pending_outcomes(runtime) == []
        assert pending_outcome(runtime, job.job_id) is None
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_human_followup_allows_subagent_next_tool(tmp_path: Path) -> None:
    """Human input cannot block later tools inside an accepted subagent turn."""
    runtime = await tool_job_runtime(tmp_path)
    human = HumanMessageSignal()
    started, proceed, next_tool = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def operation() -> BackgroundOutcome:
        started.set()
        await proceed.wait()
        job_checkpoint()
        next_tool.set()
        return BackgroundOutcome("completed", "finished")

    try:
        job = await start_delegation_job(runtime, job_child(), owner=job_owner(), operation=operation)
        await started.wait()
        human.notify()
        with human_message_signal_context(human):
            assert (await runtime.wait(job.job_id, owner=job_owner(), depth=0)).job.status == "running"
        proceed.set()
        await asyncio.wait_for(next_tool.wait(), JOB_TEST_TIMEOUT)
        human.clear()
        assert (await runtime.wait(job.job_id, owner=job_owner(), depth=0)).job.result == "finished"
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"requester_id": "@other:test"},
        {"agent_name": "other"},
        {"room_id": "!other:test"},
        {"resolved_thread_id": "$other"},
        {"session_id": "other"},
        {"transport_agent_name": "team"},
    ],
)
async def test_scope_mismatch_cannot_inspect_or_cancel(tmp_path: Path, change: dict[str, str]) -> None:
    """Knowledge of a job ID conveys no authority in another execution scope."""
    runtime = await tool_job_runtime(tmp_path)
    try:
        finish = asyncio.Event()

        async def operation() -> BackgroundOutcome:
            await finish.wait()
            return BackgroundOutcome("completed", "done")

        job = await start_delegation_job(runtime, job_child(), owner=job_owner(), operation=operation)
        with pytest.raises(ValueError, match="not available"):
            await lookup(runtime, job.job_id, owner=replace(job_owner(), **change), depth=0)
        with pytest.raises(ValueError, match="not available"):
            await runtime.cancel(job.job_id, owner=replace(job_owner(), **change), depth=0)
        assert (await runtime.cancel(job.job_id, owner=job_owner(), depth=0)).status == "cancelled"
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_restart_retains_result_and_marks_live_work_interrupted(tmp_path: Path) -> None:
    """Restart returns stored exact outcomes without executing abandoned work again."""
    runtime = await tool_job_runtime(tmp_path)
    try:

        async def completed() -> BackgroundOutcome:
            return BackgroundOutcome("completed", "durable")

        async def running() -> BackgroundOutcome:
            await asyncio.Event().wait()
            raise AssertionError

        first = await start_delegation_job(runtime, job_child(), owner=job_owner(), operation=completed)
        result = await runtime.wait(first.job_id, owner=job_owner(), depth=0)
        await runtime.release_wait(first.job_id, result.claim)
        second = await start_delegation_job(runtime, job_child("c" * 32), owner=job_owner(), operation=running)
    finally:
        await runtime.shutdown()
    restored = await tool_job_runtime(tmp_path)
    try:
        await restored.recover()
        assert (await lookup(restored, first.job_id, owner=job_owner(), depth=0)).result == "durable"
        assert (await lookup(restored, second.job_id, owner=job_owner(), depth=0)).status == "interrupted"
        assert len(pending_outcomes(restored)) == 2
    finally:
        await restored.shutdown()


@pytest.mark.asyncio
async def test_existing_queued_human_releases_wait_without_blocking_first_tool(tmp_path: Path) -> None:
    """Pending input is observed on subscription while accepted work still starts."""
    runtime = await tool_job_runtime(tmp_path)
    human = HumanMessageSignal()
    human.notify()
    entered, finish = asyncio.Event(), asyncio.Event()

    async def operation() -> BackgroundOutcome:
        job_checkpoint()
        entered.set()
        await finish.wait()
        return BackgroundOutcome("completed")

    try:
        job = await start_delegation_job(runtime, job_child(), owner=job_owner(), operation=operation)
        with human_message_signal_context(human):
            assert (await runtime.wait(job.job_id, owner=job_owner(), depth=0)).job.status == "running"
        await asyncio.wait_for(entered.wait(), JOB_TEST_TIMEOUT)
    finally:
        finish.set()
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_cancelled_active_wait_releases_result_claim(tmp_path: Path) -> None:
    """Cancellation after wait admission must release its lease without killing the child."""
    runtime = await tool_job_runtime(tmp_path)
    try:
        running, finish = asyncio.Event(), asyncio.Event()

        async def operation() -> BackgroundOutcome:
            running.set()
            await finish.wait()
            return BackgroundOutcome("completed", "survived")

        job = await start_delegation_job(runtime, job_child(), owner=job_owner(), operation=operation)
        await running.wait()
        waiter = asyncio.create_task(runtime.wait(job.job_id, owner=job_owner(), depth=0))
        admitted = asyncio.Event()
        asyncio.get_running_loop().call_soon(admitted.set)
        await admitted.wait()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        finish.set()
        result = await runtime.wait(job.job_id, owner=job_owner(), depth=0)
        assert result.job.result == "survived"
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_cancellation_waits_for_native_approval_cleanup(tmp_path: Path) -> None:
    """A terminal job must never leave its child conversation locked in a paused approval."""
    runtime = await tool_job_runtime(tmp_path)
    try:
        cleaning, cleaned = asyncio.Event(), asyncio.Event()

        child = job_child()

        async def operation() -> BackgroundOutcome:
            return await awaiting_approval(runtime, child.delegation_id)

        async def cleanup(child: DelegationChild) -> None:
            cleaning.set()
            await cleaned.wait()
            child.status = "cancelled"

        job = await start_delegation_job(runtime, child, owner=job_owner(), operation=operation, cancel=cleanup)
        await wait_for_status(runtime, job.job_id, "awaiting_approval")
        cancelling = asyncio.create_task(runtime.cancel(job.job_id, owner=job_owner(), depth=0))
        await cleaning.wait()
        assert (await lookup(runtime, job.job_id, owner=job_owner(), depth=0)).status == "cancel_requested"
        cleaned.set()
        assert delegation_child(await cancelling).status == "cancelled"
        assert (await lookup(runtime, job.job_id, owner=job_owner(), depth=0)).status == "cancelled"
        assert len(pending_outcomes(runtime)) == 1
    finally:
        cleaned.set()
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("stopped", [False, True])
async def test_shutdown_keeps_a_saved_stop_a_cancellation(tmp_path: Path, stopped: bool) -> None:
    """A Stop saved while shutdown refused new cancellations still stops the job as a cancellation."""
    runtime = await tool_job_runtime(tmp_path)
    started = asyncio.Event()
    by_shutdown: list[bool] = []

    async def operation() -> BackgroundOutcome:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            by_shutdown.append(job_stopped_by_shutdown())
            raise
        raise AssertionError

    job = await start_delegation_job(runtime, job_child(), owner=job_owner(), operation=operation)
    await asyncio.wait_for(started.wait(), JOB_TEST_TIMEOUT)
    if stopped:
        entry = runtime._entries[job.job_id]
        entry.job = replace(entry.job, user_stop_receipt_order=1)
    await runtime.shutdown()
    assert by_shutdown == [not stopped]


@pytest.mark.asyncio
async def test_recovery_interrupts_running_work_and_cancels_only_on_request(tmp_path: Path) -> None:
    """Work a crash left running settles as interrupted by the restart; a saved or later cancel stays a cancellation."""
    runtime = await tool_job_runtime(tmp_path)
    release = asyncio.Event()

    async def blocked() -> BackgroundOutcome:
        await asyncio.Event().wait()
        raise AssertionError

    async def unwinding() -> BackgroundOutcome:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()
            raise
        raise AssertionError

    by_restart: dict[str, bool] = {}

    async def cleanup(job: background.BackgroundJob) -> BackgroundOutcome | None:
        by_restart[job.job_id] = job_stopped_by_shutdown()
        return None

    cancelling = None
    restored = None
    try:
        running = await start_delegation_job(runtime, job_child(), owner=job_owner(), operation=blocked)
        cancelled = await start_delegation_job(runtime, job_child("d" * 32), owner=job_owner(), operation=unwinding)
        # A requested cancellation is saved before its cleanup finishes.
        cancelling = asyncio.create_task(runtime.cancel(cancelled.job_id, owner=job_owner(), depth=0))
        await wait_for_status(runtime, cancelled.job_id, "cancel_requested")
        # The process dies without an orderly shutdown, and the next one takes its jobs over.
        restored = await tool_job_runtime(tmp_path, cancel=cleanup)
        await restored.recover()
        assert by_restart == {running.job_id: True, cancelled.job_id: False}
    finally:
        if restored is not None:
            await restored.shutdown()
        release.set()
        abandoned = [entry.task for entry in runtime._entries.values() if entry.task is not None]
        for task in abandoned:
            task.cancel()
        await asyncio.gather(*abandoned, *([cancelling] if cancelling is not None else []), return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_sync_agno_tool_checkpoint_respects_cancellation_across_loops(cancelled: bool) -> None:
    """Threaded tools cross a cancellation checkpoint without waiting on an unrelated loop."""
    control = JobControl()
    if cancelled:
        control.cancel()
    results: list[str] = []

    def tool() -> str:
        results.append("executed")
        return "done"

    toolkit = Toolkit(name="test", tools=[tool])
    tool_hooks.prepend_tool_hook_bridge(
        toolkit,
        tool_hooks.build_tool_hook_bridge(HookRegistry.empty(), agent_name="parent"),
    )
    call = FunctionCall(function=toolkit.functions["tool"], arguments={}, call_id="sync-call")
    with job_control_context(control):
        if cancelled:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.to_thread(call.execute)
            assert results == []
        else:
            result = await asyncio.to_thread(call.execute)
            assert result.status == "success"
            assert result.result == "done"
            assert results == ["executed"]


@pytest.mark.asyncio
async def test_cancelled_admission_still_launches_owned_operation_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancelled parent cannot strand a durably accepted job between its write and launch."""
    runtime = await tool_job_runtime(tmp_path)
    child = job_child()
    written, executing, finish, release_writer = (asyncio.Event() for _ in range(4))

    async def blocked_writer(_job: dict[str, object], _payload: object) -> None:
        written.set()
        await release_writer.wait()

    intercept_job_saves(monkeypatch, before=blocked_writer)
    calls = 0

    async def operation() -> BackgroundOutcome:
        nonlocal calls
        calls += 1
        executing.set()
        await finish.wait()
        return BackgroundOutcome("completed", "survived admission cancellation")

    admission = asyncio.create_task(
        start_delegation(runtime, child, owner=job_owner(), operation=operation, cancel=keep_child),
    )
    try:
        await written.wait()
        admission.cancel()
        release_writer.set()
        with pytest.raises(asyncio.CancelledError):
            await admission
        assert executing.is_set()
        claimed = await runtime.wait(child.delegation_id, owner=job_owner(), depth=0, timeout=0)
        assert claimed.claim is None
        finish.set()
        result = await runtime.wait(child.delegation_id, owner=job_owner(), depth=0)
        assert result.job.result == "survived admission cancellation"
        assert calls == 1
    finally:
        release_writer.set()
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_shutdown_keeps_native_result_committed_before_outcome_publication(tmp_path: Path) -> None:
    """Shutdown cannot replace a committed native answer with an interruption notice."""
    runtime = await tool_job_runtime(tmp_path)
    try:
        child = job_child()
        committed = asyncio.Event()

        async def operation() -> BackgroundOutcome:
            child.status = "completed"
            child.result = "Exact durable completed answer"
            committed.set()
            await asyncio.Event().wait()
            raise AssertionError

        job = await start_delegation_job(runtime, child, owner=job_owner(), operation=operation)
        await committed.wait()
    finally:
        await runtime.shutdown()
    saved = (await saved_jobs(tmp_path))[job.job_id]
    assert saved.status == "completed"
    assert saved.result == "Exact durable completed answer"


@pytest.mark.asyncio
async def test_restart_adopts_native_completion_found_by_reconciliation(tmp_path: Path) -> None:
    """Startup reconciliation is authoritative when native storage proves completion."""
    runtime = await tool_job_runtime(tmp_path)
    child = job_child()

    async def operation() -> BackgroundOutcome:
        await asyncio.Event().wait()
        raise AssertionError

    await start_delegation_job(runtime, child, owner=job_owner(), operation=operation)
    await runtime.shutdown()
    snapshot = asdict(replace((await saved_jobs(tmp_path))[child.delegation_id], status="running", result=None))
    await write_saved_job(tmp_path, child.delegation_id, json.dumps(snapshot))

    async def reconcile(retained: DelegationChild) -> None:
        retained.status = "completed"
        retained.result = "Exact durable completed answer"

    restored = await tool_job_runtime(tmp_path, cancel=partial(reconcile_delegation, cleanup=reconcile))
    try:
        await restored.recover()
        job = await lookup(restored, child.delegation_id, owner=job_owner(), depth=0)
        assert job.status == "completed"
        assert job.result == "Exact durable completed answer"
    finally:
        await restored.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_shutdown_waiter", [False, True])
async def test_shutdown_drains_accepted_cancellation(
    tmp_path: Path,
    cancel_shutdown_waiter: bool,
) -> None:
    """Shutdown waits until an accepted cancellation's cleanup has saved its outcome."""
    cleanup_started, release_cleanup = asyncio.Event(), asyncio.Event()

    async def cleanup(child: DelegationChild) -> None:
        cleanup_started.set()
        await release_cleanup.wait()
        child.status = "cancelled"

    runtime = await tool_job_runtime(tmp_path, cancel=partial(reconcile_delegation, cleanup=cleanup))
    child = job_child()

    async def approval() -> BackgroundOutcome:
        return await awaiting_approval(runtime, child.delegation_id)

    job = await start_delegation_job(runtime, child, owner=job_owner(), operation=approval)
    await wait_for_status(runtime, job.job_id, "awaiting_approval")
    cancelling = asyncio.create_task(runtime.cancel(job.job_id, owner=job_owner(), depth=0))
    await cleanup_started.wait()
    runtime.changed.clear()
    stopping = asyncio.create_task(runtime.shutdown())
    await runtime.changed.wait()
    if cancel_shutdown_waiter:
        stopping.cancel()
    try:
        # This is a bounded assertion of non-completion while an explicit cleanup gate is closed.
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.1):
                await asyncio.shield(stopping)
    finally:
        release_cleanup.set()
        await cancelling
        if cancel_shutdown_waiter:
            with pytest.raises(asyncio.CancelledError):
                await stopping
        else:
            await stopping
    restored = await tool_job_runtime(tmp_path)
    try:
        await restored.recover()
        assert (await lookup(restored, job.job_id, owner=job_owner(), depth=0)).status == "cancelled"
    finally:
        await restored.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation_name",
    ["acknowledge_wait", "lookup", "wait", "cancel"],
)
async def test_closed_runtime_rejects_stale_parent_operations(tmp_path: Path, operation_name: str) -> None:
    """Old response callbacks cannot write after a replacement acquires runtime storage."""
    runtime = await tool_job_runtime(tmp_path)

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "Saved")

    job = await start_delegation_job(runtime, job_child(), owner=job_owner(), operation=operation)
    waiting = await runtime.wait(job.job_id, owner=job_owner(), depth=0)
    second = await start_delegation_job(runtime, job_child("c" * 32), owner=job_owner(), operation=operation)
    second_wait = await runtime.wait(second.job_id, owner=job_owner(), depth=0)
    await runtime.release_wait(second.job_id, second_wait.claim)
    await runtime.shutdown()
    restored = await tool_job_runtime(tmp_path)
    try:
        await restored.recover()
        operations = {
            "acknowledge_wait": lambda: runtime.acknowledge_wait(job.job_id, waiting.claim),
            "lookup": lambda: lookup(runtime, job.job_id, owner=job_owner(), depth=0),
            "wait": lambda: runtime.wait(job.job_id, owner=job_owner(), depth=0),
            "cancel": lambda: runtime.cancel(job.job_id, owner=job_owner(), depth=0),
        }
        with pytest.raises(ValueError, match="closed"):
            await operations[operation_name]()
        assert len(pending_outcomes(restored)) == 2
    finally:
        await restored.shutdown()


@pytest.mark.asyncio
async def test_shutdown_rejects_new_cancel_while_draining_execution(tmp_path: Path) -> None:
    """Shutdown's task drain cannot be bypassed by a newly admitted public cancellation."""
    runtime = await tool_job_runtime(tmp_path)
    executing, draining, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def operation() -> BackgroundOutcome:
        try:
            executing.set()
            await asyncio.Event().wait()
        finally:
            draining.set()
            await release.wait()
        raise AssertionError

    async def approval() -> BackgroundOutcome:
        return await awaiting_approval(runtime, "c" * 32)

    await start_delegation_job(runtime, job_child(), owner=job_owner(), operation=operation)
    await executing.wait()
    second = await start_delegation_job(runtime, job_child("c" * 32), owner=job_owner(), operation=approval)
    await wait_for_status(runtime, second.job_id, "awaiting_approval")
    stopping = asyncio.create_task(runtime.shutdown())
    await draining.wait()
    try:
        with pytest.raises(ValueError, match="shutting down"):
            await runtime.cancel(second.job_id, owner=job_owner(), depth=0)
    finally:
        release.set()
        await stopping


@pytest.mark.asyncio
async def test_shutdown_does_not_cancel_an_existing_execution_cleanup_twice(tmp_path: Path) -> None:
    """An accepted cancel already owns execution cancellation; shutdown only drains it."""
    runtime = await tool_job_runtime(tmp_path)
    executing, cleaning, release, cleaned = (asyncio.Event() for _ in range(4))

    async def operation() -> BackgroundOutcome:
        try:
            executing.set()
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            cleaned.set()
        raise AssertionError

    job = await start_delegation_job(runtime, job_child(), owner=job_owner(), operation=operation)
    await executing.wait()
    cancelling = asyncio.create_task(runtime.cancel(job.job_id, owner=job_owner(), depth=0))
    await cleaning.wait()
    stopping = asyncio.create_task(runtime.shutdown())
    admitted = asyncio.Event()
    asyncio.get_running_loop().call_soon(admitted.set)
    await admitted.wait()
    release.set()
    await cancelling
    await stopping
    assert cleaned.is_set()


@pytest.mark.asyncio
async def test_native_recovery_requires_exclusive_child_liveness(tmp_path: Path) -> None:
    """The generic registry cannot repair native records while another native executor owns them."""
    paths = test_runtime_paths(tmp_path)
    runtime = await tool_job_runtime(paths.storage_root)
    child = job_child()

    async def operation() -> BackgroundOutcome:
        await asyncio.Event().wait()
        raise AssertionError

    async def earlier_result() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "Earlier answer")

    earlier = await start_delegation_job(runtime, job_child("0" * 32), owner=job_owner(), operation=earlier_result)
    waiting = await runtime.wait(earlier.job_id, owner=job_owner(), depth=0)
    await runtime.release_wait(earlier.job_id, waiting.claim)
    await start_delegation_job(runtime, child, owner=job_owner(), operation=operation)
    await runtime.shutdown()
    snapshot = asdict(replace((await saved_jobs(paths.storage_root))[child.delegation_id], status="running"))
    await write_saved_job(paths.storage_root, child.delegation_id, json.dumps(snapshot))
    calls = 0

    async def cleanup(retained: DelegationChild) -> None:
        nonlocal calls
        calls += 1
        retained.status = "completed"
        retained.result = "Native durable answer"

    restored = await tool_job_runtime(
        paths.storage_root,
        cancel=partial(reconcile_delegation, cleanup=cleanup, runtime_paths=paths),
    )
    try:
        async with subagent_liveness(child, paths):
            with pytest.raises(background.JobRecoveryBlockedError, match="still executing"):
                await restored.recover()
            assert calls == 0
        await restored.recover()
        assert {job.job_id for job in await restored.list_jobs(owner=job_owner(), depth=0)} == {
            earlier.job_id,
            child.delegation_id,
        }
        job = await lookup(restored, child.delegation_id, owner=job_owner(), depth=0)
        assert job.status == "completed"
        assert job.result == "Native durable answer"
        assert calls == 1
    finally:
        await restored.shutdown()
