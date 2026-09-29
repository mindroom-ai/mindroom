"""Behavioral checks for durable ownership of background delegation turns."""

from __future__ import annotations

import asyncio
import gc
import json
import threading
import weakref
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING

import pytest
from agno.tools.function import FunctionCall
from agno.tools.toolkit import Toolkit

from mindroom.delegation.background import (
    cancel_retained_delegation,
    continue_delegation,
    delegation_child,
    delegation_outcome,
    delegation_result,
    reconcile_delegation,
    retained_child,
    start_delegation,
)
from mindroom.delegation.sessions import subagent_liveness
from mindroom.hooks import HookRegistry
from mindroom.message_target import MessageTarget
from mindroom.response_lifecycle import ResponseLifecycleCoordinator
from mindroom.tool_jobs import runtime as background
from mindroom.tool_jobs.control import (
    HumanMessageSignal,
    JobControl,
    human_message_signal_context,
    job_checkpoint,
    job_control_context,
)
from mindroom.tool_jobs.runtime import BackgroundOutcome
from mindroom.tool_system import tool_hooks
from tests.conftest import test_runtime_paths
from tests.test_queued_message_notify import _envelope
from tests.tool_job_helpers import job_child, job_owner, start_delegation_job, tool_job_runtime

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.delegation.state import DelegationChild


@pytest.mark.asyncio
async def test_consumed_native_result_releases_live_child_and_discovery_payload(tmp_path: Path) -> None:
    """Consumption releases native objects while the exact formatted result remains readable."""
    runtime = tool_job_runtime(tmp_path)
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
    runtime = tool_job_runtime(tmp_path)
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
    directory = tmp_path / "tool_jobs"
    restored = tool_job_runtime(tmp_path)

    async def source_finished(_job: background.BackgroundJob) -> bool:
        return True

    try:
        await restored.recover()
        saved = await restored.lookup(child.delegation_id, owner=job_owner(), depth=0)
        assert await delegation_result(restored, saved) == delivered
        discovered = await restored.list_jobs(owner=job_owner(), depth=0)
        assert len(json.dumps([asdict(job) for job in discovered])) < 8192
        await restored.expire_consumed(
            before=datetime.now(UTC) + timedelta(days=31),
            source_finished=source_finished,
        )
        assert child.delegation_id not in restored._entries
        assert not list(directory.glob(f"{child.delegation_id}.*"))
    finally:
        await restored.shutdown()


@pytest.mark.asyncio
async def test_cancelled_recovered_delegation_reads_terminal_native_evidence(tmp_path: Path) -> None:
    """Cancellation after native settlement preserves an outcome absent from the generic snapshot."""
    runtime = tool_job_runtime(tmp_path)
    child = job_child()

    async def approval() -> BackgroundOutcome:
        child.status = "paused"
        return BackgroundOutcome("awaiting_approval")

    await start_delegation_job(runtime, child, owner=job_owner(), operation=approval)
    paused = await runtime.wait(child.delegation_id, owner=job_owner(), depth=0)
    await runtime.release_wait(child.delegation_id, paused.claim)
    await runtime.shutdown()
    native_result = tmp_path / "native-result.txt"
    settled = asyncio.Event()

    async def reconcile(recovered: DelegationChild) -> None:
        recovered.status = "completed"
        recovered.result = native_result.read_text()

    restored = tool_job_runtime(tmp_path, cancel=partial(reconcile_delegation, cleanup=reconcile))
    try:
        await restored.recover()
        child = retained_child(restored, await restored.lookup(child.delegation_id, owner=job_owner(), depth=0))

        async def continuation() -> BackgroundOutcome:
            child.status = "completed"
            child.result = "Native completion before generic settlement"
            native_result.write_text(child.result)
            settled.set()
            await asyncio.Event().wait()
            pytest.fail("Cancellation should interrupt generic settlement")

        await continue_delegation(
            restored,
            child.delegation_id,
            owner=job_owner(),
            depth=0,
            expected_generation=0,
            operation=continuation,
        )
        await settled.wait()
        result = await restored.cancel(child.delegation_id, owner=job_owner(), depth=0)
        assert result.status == "completed"
        assert result.result == native_result.read_text()
    finally:
        await restored.shutdown()


@pytest.mark.asyncio
async def test_timeout_and_cancelled_waiter_leave_one_child_alive(tmp_path: Path) -> None:
    """Foreground abandonment must never cancel or restart an owned operation."""
    runtime = tool_job_runtime(tmp_path)
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
        assert await runtime.pending_outcomes() == []
        await runtime.acknowledge_wait(job.job_id, result.claim)
    finally:
        finish.set()
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_wait_claim_released_without_ack_keeps_outcome_pending(tmp_path: Path) -> None:
    """An abandoned result lease remains eligible for a later serialized consumer."""
    runtime = tool_job_runtime(tmp_path)

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "done")

    try:
        job = await start_delegation_job(runtime, job_child(), owner=job_owner(), operation=operation)
        result = await runtime.wait(job.job_id, owner=job_owner(), depth=0)
        assert result.claim is not None
        assert await runtime.pending_outcomes() == []
        await runtime.release_wait(job.job_id, result.claim)
        assert [item.job_id for item in await runtime.pending_outcomes()] == [job.job_id]
        waiting = await runtime.wait(job.job_id, owner=job_owner(), depth=0)
        assert waiting.claim is not None
        assert await runtime.pending_outcomes() == []
        await runtime.acknowledge_wait(job.job_id, waiting.claim)
        assert await runtime.pending_outcomes() == []
        assert await runtime.outcome(job.job_id, result.job.generation) is None
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_human_followup_allows_subagent_next_tool(tmp_path: Path) -> None:
    """Human input cannot block later tools inside an accepted subagent turn."""
    runtime = tool_job_runtime(tmp_path)
    human = HumanMessageSignal()
    started, proceed, next_tool = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def operation() -> BackgroundOutcome:
        started.set()
        await proceed.wait()
        job_checkpoint()
        next_tool.set()
        return BackgroundOutcome("completed", "finished")

    try:
        with human_message_signal_context(human):
            job = await start_delegation_job(runtime, job_child(), owner=job_owner(), operation=operation)
        await started.wait()
        human.notify()
        assert (await runtime.wait(job.job_id, owner=job_owner(), depth=0)).job.status == "running"
        proceed.set()
        await asyncio.wait_for(next_tool.wait(), 1)
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
    runtime = tool_job_runtime(tmp_path)
    try:
        finish = asyncio.Event()

        async def operation() -> BackgroundOutcome:
            await finish.wait()
            return BackgroundOutcome("completed", "done")

        job = await start_delegation_job(runtime, job_child(), owner=job_owner(), operation=operation)
        with pytest.raises(ValueError, match="not available"):
            await runtime.lookup(job.job_id, owner=replace(job_owner(), **change), depth=0)
        with pytest.raises(ValueError, match="not available"):
            await runtime.cancel(job.job_id, owner=replace(job_owner(), **change), depth=0)
        assert (await runtime.cancel(job.job_id, owner=job_owner(), depth=0)).status == "cancelled"
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_restart_retains_result_and_marks_live_work_interrupted(tmp_path: Path) -> None:
    """Restart returns stored exact outcomes without executing abandoned work again."""
    runtime = tool_job_runtime(tmp_path)
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
    restored = tool_job_runtime(tmp_path)
    try:
        await restored.recover()
        assert (await restored.lookup(first.job_id, owner=job_owner(), depth=0)).result == "durable"
        assert (await restored.lookup(second.job_id, owner=job_owner(), depth=0)).status == "interrupted"
        assert len(await restored.pending_outcomes()) == 2
    finally:
        await restored.shutdown()


@pytest.mark.asyncio
async def test_approval_continuation_runs_after_human_followup(tmp_path: Path) -> None:
    """Human input neither grants native approval nor blocks an approved continuation."""
    runtime = tool_job_runtime(tmp_path)
    human = HumanMessageSignal()
    executed = asyncio.Event()

    async def approval() -> BackgroundOutcome:
        return BackgroundOutcome("awaiting_approval", approval_state={"toolkit_owners": [["call", "shell"]]})

    async def continuation() -> BackgroundOutcome:
        job_checkpoint()
        executed.set()
        return BackgroundOutcome("completed", "approved")

    try:
        with human_message_signal_context(human):
            job = await start_delegation_job(runtime, job_child(), owner=job_owner(), operation=approval)
        first = await runtime.wait(job.job_id, owner=job_owner(), depth=0)
        await runtime.acknowledge_wait(job.job_id, first.claim)
        human.notify()
        assert (await runtime.lookup(job.job_id, owner=job_owner(), depth=0)).status == "awaiting_approval"
        assert not executed.is_set()
        await continue_delegation(
            runtime,
            job.job_id,
            owner=job_owner(),
            depth=0,
            expected_generation=0,
            operation=continuation,
        )
        await asyncio.wait_for(executed.wait(), 1)
        human.clear()
        result = await runtime.wait(job.job_id, owner=job_owner(), depth=0)
        assert result.job.result == "approved"
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_existing_queued_human_releases_wait_without_blocking_first_tool(tmp_path: Path) -> None:
    """Pending input is observed on subscription while accepted work still starts."""
    runtime = tool_job_runtime(tmp_path)
    human = HumanMessageSignal()
    human.notify()
    entered, finish = asyncio.Event(), asyncio.Event()

    async def operation() -> BackgroundOutcome:
        job_checkpoint()
        entered.set()
        await finish.wait()
        return BackgroundOutcome("completed")

    try:
        with human_message_signal_context(human):
            job = await start_delegation_job(runtime, job_child(), owner=job_owner(), operation=operation)
        assert (await runtime.wait(job.job_id, owner=job_owner(), depth=0)).job.status == "running"
        await asyncio.wait_for(entered.wait(), 1)
    finally:
        finish.set()
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_idle_parent_human_ingress_releases_active_job_wait(tmp_path: Path) -> None:
    """Background jobs retain their conversation signal after parent lifecycle completion."""
    runtime = tool_job_runtime(tmp_path)
    try:
        coordinator = ResponseLifecycleCoordinator()
        target = MessageTarget.resolve("!room:test", "$root", "$human")
        signal = coordinator._get_or_create_queued_signal(target)

        async def operation() -> BackgroundOutcome:
            await asyncio.Event().wait()
            raise AssertionError

        with human_message_signal_context(signal.human_signal):
            job = await start_delegation_job(runtime, job_child(), owner=job_owner(), operation=operation)
        assert not coordinator.has_active_response_for_target(target)
        waiter = asyncio.create_task(runtime.wait(job.job_id, owner=job_owner(), depth=0))
        await asyncio.sleep(0)
        coordinator.reserve_waiting_human_message(target=target, response_envelope=_envelope(target=target))
        result = await asyncio.wait_for(waiter, 1)
        assert result.job.status == "running"
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_cancelled_active_wait_releases_result_claim(tmp_path: Path) -> None:
    """Cancellation after wait admission must release its lease without killing the child."""
    runtime = tool_job_runtime(tmp_path)
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
    runtime = tool_job_runtime(tmp_path)
    try:
        cleaning, cleaned = asyncio.Event(), asyncio.Event()

        async def operation() -> BackgroundOutcome:
            return BackgroundOutcome("awaiting_approval", approval_state={"owners": [["tool", "shell"]]})

        async def cleanup(child: DelegationChild) -> None:
            cleaning.set()
            await cleaned.wait()
            child.status = "cancelled"

        job = await start_delegation_job(runtime, job_child(), owner=job_owner(), operation=operation, cancel=cleanup)
        result = await runtime.wait(job.job_id, owner=job_owner(), depth=0)
        await runtime.acknowledge_wait(job.job_id, result.claim)
        cancelling = asyncio.create_task(runtime.cancel(job.job_id, owner=job_owner(), depth=0))
        await cleaning.wait()
        assert (await runtime.lookup(job.job_id, owner=job_owner(), depth=0)).status == "cancel_requested"
        cleaned.set()
        assert delegation_child(await cancelling).status == "cancelled"
        assert (await runtime.lookup(job.job_id, owner=job_owner(), depth=0)).status == "cancelled"
        assert len(await runtime.pending_outcomes()) == 1
    finally:
        cleaned.set()
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_restart_preserves_native_approval_owner_snapshot(tmp_path: Path) -> None:
    """Restart reconstructs approval authority without granting the protected action."""
    runtime = tool_job_runtime(tmp_path)
    try:
        human = HumanMessageSignal()

        async def approval() -> BackgroundOutcome:
            return BackgroundOutcome("awaiting_approval", approval_state={"owners": [["run", "tool", "shell"]]})

        with human_message_signal_context(human):
            job = await start_delegation_job(runtime, job_child(), owner=job_owner(), operation=approval)
        waited = await runtime.wait(job.job_id, owner=job_owner(), depth=0)
        await runtime.release_wait(job.job_id, waited.claim)
        human.notify()
        await runtime.lookup(job.job_id, owner=job_owner(), depth=0)
    finally:
        await runtime.shutdown()
    restored = tool_job_runtime(tmp_path)
    try:
        await restored.recover()
        saved = await restored.lookup(job.job_id, owner=job_owner(), depth=0)
        assert saved.approval_state == {"owners": [["run", "tool", "shell"]]}
        assert saved.status == "awaiting_approval"
    finally:
        await restored.shutdown()


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
async def test_retained_cleanup_can_cancel_after_authorization_revocation(tmp_path: Path) -> None:
    """Revocation blocks public control but cannot prevent exact persisted approval cleanup."""
    allowed = True
    runtime = tool_job_runtime(tmp_path, authorize=lambda _job: allowed)
    try:

        async def operation() -> BackgroundOutcome:
            return BackgroundOutcome("awaiting_approval")

        job = await start_delegation_job(runtime, job_child(), owner=job_owner(), operation=operation)
        result = await runtime.wait(job.job_id, owner=job_owner(), depth=0)
        await runtime.release_wait(job.job_id, result.claim)
        allowed = False
        with pytest.raises(ValueError, match="not available"):
            await runtime.cancel(job.job_id, owner=job_owner(), depth=0)
        assert not await cancel_retained_delegation(
            runtime,
            replace(delegation_child(job), run_id="other"),
            generation=job.generation,
        )
        assert await cancel_retained_delegation(runtime, delegation_child(job), generation=job.generation)
        allowed = True
        assert (await runtime.lookup(job.job_id, owner=job_owner(), depth=0)).status == "cancelled"
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_stale_approval_card_cannot_cancel_the_continued_child(tmp_path: Path) -> None:
    """A duplicate card for an approved generation leaves the child's newer generation running."""
    runtime = tool_job_runtime(tmp_path)
    release = asyncio.Event()

    async def approval() -> BackgroundOutcome:
        return BackgroundOutcome("awaiting_approval")

    async def resumed() -> BackgroundOutcome:
        await release.wait()
        return BackgroundOutcome("completed", "done")

    try:
        job = await start_delegation_job(runtime, job_child(), owner=job_owner(), operation=approval)
        waited = await runtime.wait(job.job_id, owner=job_owner(), depth=0)
        await runtime.release_wait(job.job_id, waited.claim)
        stale = delegation_child(waited.job)
        continued = await continue_delegation(
            runtime,
            job.job_id,
            owner=job_owner(),
            depth=0,
            expected_generation=waited.job.generation,
            operation=resumed,
        )
        assert continued.generation == waited.job.generation + 1
        assert not await cancel_retained_delegation(runtime, stale, generation=waited.job.generation)
        assert (await runtime.lookup(job.job_id, owner=job_owner(), depth=0)).status == "running"
        assert await cancel_retained_delegation(runtime, stale, generation=continued.generation)
        assert (await runtime.lookup(job.job_id, owner=job_owner(), depth=0)).status == "cancelled"
    finally:
        release.set()
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("continuation", [False, True])
async def test_cancelled_admission_still_launches_owned_operation_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    continuation: bool,
) -> None:
    """A cancelled parent cannot strand a durably accepted job between its write and launch."""
    runtime = tool_job_runtime(tmp_path)
    child = job_child()

    async def approval() -> BackgroundOutcome:
        return BackgroundOutcome("awaiting_approval")

    if continuation:
        await start_delegation_job(runtime, child, owner=job_owner(), operation=approval)
        previous = await runtime.wait(child.delegation_id, owner=job_owner(), depth=0)
        await runtime.acknowledge_wait(child.delegation_id, previous.claim)
    written, executing, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
    release_writer = threading.Event()
    owner_loop = asyncio.get_running_loop()
    original_writer = background.write_json_file_durable

    def blocked_writer(path: Path, payload: object, *, strict_atomic_replace: bool) -> None:
        original_writer(path, payload, strict_atomic_replace=strict_atomic_replace)
        owner_loop.call_soon_threadsafe(written.set)
        release_writer.wait()

    monkeypatch.setattr(background, "write_json_file_durable", blocked_writer)
    calls = 0

    async def operation() -> BackgroundOutcome:
        nonlocal calls
        calls += 1
        executing.set()
        await finish.wait()
        return BackgroundOutcome("completed", "survived admission cancellation")

    admission = asyncio.create_task(
        continue_delegation(
            runtime,
            child.delegation_id,
            owner=job_owner(),
            depth=0,
            expected_generation=0,
            operation=operation,
        )
        if continuation
        else start_delegation(runtime, child, owner=job_owner(), operation=operation),
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
    runtime = tool_job_runtime(tmp_path)
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
    saved = json.loads((tmp_path / "tool_jobs" / f"{job.job_id}.json").read_text())
    assert saved["status"] == "completed"
    assert saved["result"] == "Exact durable completed answer"


@pytest.mark.asyncio
async def test_restart_adopts_native_completion_found_by_reconciliation(tmp_path: Path) -> None:
    """Startup reconciliation is authoritative when native storage proves completion."""
    runtime = tool_job_runtime(tmp_path)
    child = job_child()

    async def operation() -> BackgroundOutcome:
        await asyncio.Event().wait()
        raise AssertionError

    await start_delegation_job(runtime, child, owner=job_owner(), operation=operation)
    await runtime.shutdown()
    path = tmp_path / "tool_jobs" / f"{child.delegation_id}.json"
    snapshot = json.loads(path.read_text())
    snapshot["status"] = "running"
    snapshot["result"] = None
    background.write_json_file_durable(path, snapshot)

    async def reconcile(retained: DelegationChild) -> None:
        retained.status = "completed"
        retained.result = "Exact durable completed answer"

    restored = tool_job_runtime(tmp_path, cancel=partial(reconcile_delegation, cleanup=reconcile))
    try:
        await restored.recover()
        job = await restored.lookup(child.delegation_id, owner=job_owner(), depth=0)
        assert job.status == "completed"
        assert job.result == "Exact durable completed answer"
    finally:
        await restored.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("restart_again", [False, True])
async def test_recovered_approval_continues_after_human_followup(tmp_path: Path, restart_again: bool) -> None:
    """Recovered approval authority survives human input and later explicit approval."""
    owner = replace(job_owner(), transport_agent_name="team")
    runtime = tool_job_runtime(tmp_path)

    async def approval() -> BackgroundOutcome:
        return BackgroundOutcome("awaiting_approval", approval_state={"owners": [["call", "shell"]]})

    job = await start_delegation_job(runtime, job_child(), owner=owner, operation=approval)
    waiting = await runtime.wait(job.job_id, owner=owner, depth=0)
    await runtime.release_wait(job.job_id, waiting.claim)
    await runtime.shutdown()
    restored = tool_job_runtime(tmp_path)
    executed = asyncio.Event()

    async def continuation() -> BackgroundOutcome:
        job_checkpoint()
        executed.set()
        return BackgroundOutcome("completed", "approved")

    try:
        await restored.recover()
        signal = restored.human_signal_for("team", "!room:test", "$root")
        signal.notify()
        signal.clear()
        if restart_again:
            await restored.shutdown()
            restored = tool_job_runtime(tmp_path)
            await restored.recover()
        assert (await restored.lookup(job.job_id, owner=owner, depth=0)).status == "awaiting_approval"
        await continue_delegation(
            restored,
            job.job_id,
            owner=owner,
            depth=0,
            expected_generation=0,
            operation=continuation,
        )
        await asyncio.wait_for(executed.wait(), 1)
        result = await restored.wait(job.job_id, owner=owner, depth=0)
        assert result.job.result == "approved"
    finally:
        await restored.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_shutdown_waiter", [False, True])
async def test_shutdown_drains_accepted_cancellation_before_releasing_storage(
    tmp_path: Path,
    cancel_shutdown_waiter: bool,
) -> None:
    """A replacement runtime cannot acquire storage while the previous owner still writes cleanup."""
    cleanup_started, release_cleanup = asyncio.Event(), asyncio.Event()

    async def cleanup(child: DelegationChild) -> None:
        cleanup_started.set()
        await release_cleanup.wait()
        child.status = "cancelled"

    runtime = tool_job_runtime(tmp_path, cancel=partial(reconcile_delegation, cleanup=cleanup))

    async def approval() -> BackgroundOutcome:
        return BackgroundOutcome("awaiting_approval")

    job = await start_delegation_job(runtime, job_child(), owner=job_owner(), operation=approval)
    waiting = await runtime.wait(job.job_id, owner=job_owner(), depth=0)
    await runtime.release_wait(job.job_id, waiting.claim)
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
        with pytest.raises(BlockingIOError):
            tool_job_runtime(tmp_path)
    finally:
        release_cleanup.set()
        await cancelling
        if cancel_shutdown_waiter:
            with pytest.raises(asyncio.CancelledError):
                await stopping
        else:
            await stopping
    restored = tool_job_runtime(tmp_path)
    try:
        await restored.recover()
        assert (await restored.lookup(job.job_id, owner=job_owner(), depth=0)).status == "cancelled"
    finally:
        await restored.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation_name",
    ["acknowledge_wait", "lookup", "wait", "cancel"],
)
async def test_closed_runtime_rejects_stale_parent_operations(tmp_path: Path, operation_name: str) -> None:
    """Old response callbacks cannot write after a replacement acquires runtime storage."""
    runtime = tool_job_runtime(tmp_path)

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "Saved")

    job = await start_delegation_job(runtime, job_child(), owner=job_owner(), operation=operation)
    waiting = await runtime.wait(job.job_id, owner=job_owner(), depth=0)
    second = await start_delegation_job(runtime, job_child("c" * 32), owner=job_owner(), operation=operation)
    second_wait = await runtime.wait(second.job_id, owner=job_owner(), depth=0)
    await runtime.release_wait(second.job_id, second_wait.claim)
    await runtime.shutdown()
    restored = tool_job_runtime(tmp_path)
    try:
        await restored.recover()
        operations = {
            "acknowledge_wait": lambda: runtime.acknowledge_wait(job.job_id, waiting.claim),
            "lookup": lambda: runtime.lookup(job.job_id, owner=job_owner(), depth=0),
            "wait": lambda: runtime.wait(job.job_id, owner=job_owner(), depth=0),
            "cancel": lambda: runtime.cancel(job.job_id, owner=job_owner(), depth=0),
        }
        with pytest.raises(ValueError, match="closed"):
            await operations[operation_name]()
        assert await runtime.outcome(job.job_id, waiting.job.generation) is None
        assert await runtime.pending_outcomes() == []
        assert len(await restored.pending_outcomes()) == 2
    finally:
        await restored.shutdown()


@pytest.mark.asyncio
async def test_shutdown_rejects_new_cancel_while_draining_execution(tmp_path: Path) -> None:
    """Shutdown's task drain cannot be bypassed by a newly admitted public cancellation."""
    runtime = tool_job_runtime(tmp_path)
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
        return BackgroundOutcome("awaiting_approval")

    await start_delegation_job(runtime, job_child(), owner=job_owner(), operation=operation)
    await executing.wait()
    second = await start_delegation_job(runtime, job_child("c" * 32), owner=job_owner(), operation=approval)
    waiting = await runtime.wait(second.job_id, owner=job_owner(), depth=0)
    await runtime.release_wait(second.job_id, waiting.claim)
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
    runtime = tool_job_runtime(tmp_path)
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
    runtime = tool_job_runtime(paths.storage_root)
    child = job_child()

    async def operation() -> BackgroundOutcome:
        await asyncio.Event().wait()
        raise AssertionError

    async def earlier_approval() -> BackgroundOutcome:
        return BackgroundOutcome("awaiting_approval")

    earlier = await start_delegation_job(runtime, job_child("0" * 32), owner=job_owner(), operation=earlier_approval)
    waiting = await runtime.wait(earlier.job_id, owner=job_owner(), depth=0)
    await runtime.release_wait(earlier.job_id, waiting.claim)
    await start_delegation_job(runtime, child, owner=job_owner(), operation=operation)
    await runtime.shutdown()
    path = paths.storage_root / "tool_jobs" / f"{child.delegation_id}.json"
    payload = json.loads(path.read_text())
    payload["status"] = "running"
    background.write_json_file_durable(path, payload)
    calls = 0

    async def cleanup(retained: DelegationChild) -> None:
        nonlocal calls
        calls += 1
        retained.status = "completed"
        retained.result = "Native durable answer"

    restored = tool_job_runtime(
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
        job = await restored.lookup(child.delegation_id, owner=job_owner(), depth=0)
        assert job.status == "completed"
        assert job.result == "Native durable answer"
        assert calls == 1
    finally:
        await restored.shutdown()
