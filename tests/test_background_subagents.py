"""Behavioral checks for durable ownership of background delegation turns."""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from agno.tools.function import FunctionCall
from agno.tools.toolkit import Toolkit

from mindroom.delegation import background
from mindroom.delegation.background import BackgroundOutcome, BackgroundSubagentRuntime
from mindroom.delegation.control import (
    HumanMessageSignal,
    SubagentControl,
    subagent_control_context,
    subagent_tool_checkpoint,
)
from mindroom.delegation.state import DelegationChild
from mindroom.hooks import HookRegistry
from mindroom.message_target import MessageTarget
from mindroom.response_lifecycle import ResponseLifecycleCoordinator
from mindroom.tool_system import tool_hooks
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.test_queued_message_notify import _envelope

if TYPE_CHECKING:
    from pathlib import Path


def _owner() -> ToolExecutionIdentity:
    return ToolExecutionIdentity(
        channel="matrix",
        agent_name="parent",
        requester_id="@alice:test",
        room_id="!room:test",
        thread_id=None,
        resolved_thread_id="$root",
        session_id="parent-session",
    )


def _child(job_id: str = "a" * 32) -> DelegationChild:
    return DelegationChild(
        delegation_id=job_id,
        parent_tool_call_id="call",
        caller_agent_name="parent",
        child_agent_name="child",
        task="research",
        session_id="child-session",
        run_id="run",
        model_name="default",
        depth=1,
        execution_identity={},
        subagent_id="b" * 32,
    )


@pytest.mark.asyncio
async def test_timeout_and_cancelled_waiter_leave_one_child_alive(tmp_path: Path) -> None:
    """Foreground abandonment must never cancel or restart an owned operation."""
    runtime = BackgroundSubagentRuntime(tmp_path)
    started, finish = asyncio.Event(), asyncio.Event()
    calls = 0

    async def operation() -> BackgroundOutcome:
        nonlocal calls
        calls += 1
        started.set()
        await finish.wait()
        return BackgroundOutcome("completed", "answer")

    job = await runtime.start(_child(), owner=_owner(), operation=operation)
    await started.wait()
    first = await runtime.wait(job.job_id, owner=_owner(), depth=0, timeout=0)
    assert first.job.status == "running"
    waiter = asyncio.create_task(runtime.wait(job.job_id, owner=_owner(), depth=0))
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    finish.set()
    result = await runtime.wait(job.job_id, owner=_owner(), depth=0)
    assert result.job.result == "answer"
    assert calls == 1
    assert await runtime.pending_deliveries() == []
    await runtime.acknowledge_wait(job.job_id, result.token)
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_wait_claim_released_without_ack_keeps_delivery_pending(tmp_path: Path) -> None:
    """A result not yet persisted by its parent cannot consume its sole delivery."""
    runtime = BackgroundSubagentRuntime(tmp_path)

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "done")

    job = await runtime.start(_child(), owner=_owner(), operation=operation)
    result = await runtime.wait(job.job_id, owner=_owner(), depth=0)
    assert result.token is not None
    assert await runtime.pending_deliveries() == []
    await runtime.release_wait(job.job_id, result.token)
    assert [job.job_id for job in await runtime.pending_deliveries()] == [job.job_id]
    delivery = await runtime.claim_delivery(job.job_id, content={"body": "done"}, transaction_id="stable")
    assert delivery is not None
    waiting = await runtime.wait(job.job_id, owner=_owner(), depth=0)
    assert waiting.delivery_queued
    retry = await runtime.claim_delivery(job.job_id, content={"body": "changed"}, transaction_id="different")
    assert retry == delivery
    await runtime.acknowledge_delivery(job.job_id, "stable", event_id="$sent")
    assert await runtime.pending_deliveries() == []
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_human_pause_blocks_next_tool_until_explicit_resume(tmp_path: Path) -> None:
    """Already executing work may finish; the next tool waits for parent control."""
    runtime = BackgroundSubagentRuntime(tmp_path)
    human = HumanMessageSignal()
    started, next_tool, allow_checkpoint = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def operation() -> BackgroundOutcome:
        started.set()
        await allow_checkpoint.wait()
        await subagent_tool_checkpoint()
        next_tool.set()
        return BackgroundOutcome("completed", "finished")

    job = await runtime.start(_child(), owner=_owner(), operation=operation, human_signal=human)
    await started.wait()
    human.notify()
    paused = await runtime.wait(job.job_id, owner=_owner(), depth=0)
    assert paused.job.status == "paused_for_human"
    allow_checkpoint.set()
    assert not next_tool.is_set()
    await runtime.resume(job.job_id, owner=_owner(), depth=0)
    result = await runtime.wait(job.job_id, owner=_owner(), depth=0)
    assert result.job.result == "finished"
    assert next_tool.is_set()
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
    runtime = BackgroundSubagentRuntime(tmp_path)
    finish = asyncio.Event()

    async def operation() -> BackgroundOutcome:
        await finish.wait()
        return BackgroundOutcome("completed", "done")

    job = await runtime.start(_child(), owner=_owner(), operation=operation)
    with pytest.raises(ValueError, match="not available"):
        await runtime.lookup(job.job_id, owner=replace(_owner(), **change), depth=0)
    with pytest.raises(ValueError, match="not available"):
        await runtime.cancel(job.job_id, owner=replace(_owner(), **change), depth=0)
    assert (await runtime.cancel(job.job_id, owner=_owner(), depth=0)).status == "cancelled"
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_restart_retains_result_and_marks_live_work_interrupted(tmp_path: Path) -> None:
    """Restart returns stored exact outcomes without executing abandoned work again."""
    runtime = BackgroundSubagentRuntime(tmp_path)

    async def completed() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "durable")

    async def running() -> BackgroundOutcome:
        await asyncio.Event().wait()
        raise AssertionError

    first = await runtime.start(_child(), owner=_owner(), operation=completed)
    result = await runtime.wait(first.job_id, owner=_owner(), depth=0)
    await runtime.release_wait(first.job_id, result.token)
    second = await runtime.start(_child("c" * 32), owner=_owner(), operation=running)
    await runtime.shutdown()
    restored = BackgroundSubagentRuntime(tmp_path)
    await restored.recover()
    assert (await restored.lookup(first.job_id, owner=_owner(), depth=0)).result == "durable"
    assert (await restored.lookup(second.job_id, owner=_owner(), depth=0)).status == "interrupted"
    assert len(await restored.pending_deliveries()) == 2
    await restored.shutdown()


@pytest.mark.asyncio
async def test_approval_continuation_preserves_human_pause(tmp_path: Path) -> None:
    """Native approval continuation never implicitly grants human-pause resumption."""
    runtime = BackgroundSubagentRuntime(tmp_path)
    human = HumanMessageSignal()
    executed = asyncio.Event()

    async def approval() -> BackgroundOutcome:
        return BackgroundOutcome("awaiting_approval", approval_state={"toolkit_owners": [["call", "shell"]]})

    job = await runtime.start(_child(), owner=_owner(), operation=approval, human_signal=human)
    first = await runtime.wait(job.job_id, owner=_owner(), depth=0)
    assert first.job.status == "awaiting_approval"
    await runtime.acknowledge_wait(job.job_id, first.token)
    human.notify()

    async def continuation() -> BackgroundOutcome:
        await subagent_tool_checkpoint()
        executed.set()
        return BackgroundOutcome("completed", "approved")

    await runtime.continue_job(job.job_id, owner=_owner(), depth=0, operation=continuation)
    paused = await runtime.wait(job.job_id, owner=_owner(), depth=0)
    assert paused.job.status == "paused_for_human"
    assert not executed.is_set()
    await runtime.resume(job.job_id, owner=_owner(), depth=0)
    result = await runtime.wait(job.job_id, owner=_owner(), depth=0)
    assert result.job.result == "approved"
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_existing_queued_human_pauses_job_before_first_tool(tmp_path: Path) -> None:
    """A follow-up queued before delegation starts must not disappear during subscription."""
    runtime = BackgroundSubagentRuntime(tmp_path)
    human = HumanMessageSignal()
    human.notify()
    entered = asyncio.Event()

    async def operation() -> BackgroundOutcome:
        await subagent_tool_checkpoint()
        entered.set()
        return BackgroundOutcome("completed")

    job = await runtime.start(_child(), owner=_owner(), operation=operation, human_signal=human)
    assert (await runtime.wait(job.job_id, owner=_owner(), depth=0)).job.status == "paused_for_human"
    assert not entered.is_set()
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_idle_parent_human_ingress_still_pauses_background_job(tmp_path: Path) -> None:
    """Background jobs retain their conversation signal after parent lifecycle completion."""
    runtime = BackgroundSubagentRuntime(tmp_path)
    coordinator = ResponseLifecycleCoordinator()
    target = MessageTarget.resolve("!room:test", "$root", "$human")
    signal = coordinator._get_or_create_queued_signal(target)

    async def operation() -> BackgroundOutcome:
        await asyncio.Event().wait()
        raise AssertionError

    job = await runtime.start(_child(), owner=_owner(), operation=operation, human_signal=signal.human_signal)
    assert not coordinator.has_active_response_for_target(target)
    coordinator.reserve_waiting_human_message(target=target, response_envelope=_envelope(target=target))
    result = await runtime.wait(job.job_id, owner=_owner(), depth=0)
    assert result.job.status == "paused_for_human"
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_cancelled_active_wait_releases_result_claim(tmp_path: Path) -> None:
    """Cancellation after wait admission must release its lease without killing the child."""
    runtime = BackgroundSubagentRuntime(tmp_path)
    running, finish = asyncio.Event(), asyncio.Event()

    async def operation() -> BackgroundOutcome:
        running.set()
        await finish.wait()
        return BackgroundOutcome("completed", "survived")

    job = await runtime.start(_child(), owner=_owner(), operation=operation)
    await running.wait()
    waiter = asyncio.create_task(runtime.wait(job.job_id, owner=_owner(), depth=0))
    admitted = asyncio.Event()
    asyncio.get_running_loop().call_soon(admitted.set)
    await admitted.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    finish.set()
    result = await runtime.wait(job.job_id, owner=_owner(), depth=0)
    assert result.job.result == "survived"
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_cancellation_waits_for_native_approval_cleanup(tmp_path: Path) -> None:
    """A terminal job must never leave its child conversation locked in a paused approval."""
    runtime = BackgroundSubagentRuntime(tmp_path)
    cleaning, cleaned = asyncio.Event(), asyncio.Event()

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome("awaiting_approval", approval_state={"owners": [["tool", "shell"]]})

    async def cleanup(child: DelegationChild) -> None:
        cleaning.set()
        await cleaned.wait()
        child.status = "cancelled"

    job = await runtime.start(_child(), owner=_owner(), operation=operation, cancel=cleanup)
    result = await runtime.wait(job.job_id, owner=_owner(), depth=0)
    await runtime.acknowledge_wait(job.job_id, result.token)
    cancelling = asyncio.create_task(runtime.cancel(job.job_id, owner=_owner(), depth=0))
    await cleaning.wait()
    assert job.status == "awaiting_approval"
    cleaned.set()
    assert (await cancelling).child.status == "cancelled"
    assert job.status == "cancelled"
    assert len(await runtime.pending_deliveries()) == 1
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_restart_preserves_approval_owner_snapshot_and_human_hold(tmp_path: Path) -> None:
    """Restart can reconstruct approval authority but cannot resume a human hold implicitly."""
    runtime = BackgroundSubagentRuntime(tmp_path)
    human = HumanMessageSignal()

    async def approval() -> BackgroundOutcome:
        return BackgroundOutcome("awaiting_approval", approval_state={"owners": [["run", "tool", "shell"]]})

    job = await runtime.start(_child(), owner=_owner(), operation=approval, human_signal=human)
    waited = await runtime.wait(job.job_id, owner=_owner(), depth=0)
    await runtime.release_wait(job.job_id, waited.token)
    human.notify()
    await runtime.lookup(job.job_id, owner=_owner(), depth=0)
    await runtime.shutdown()
    restored = BackgroundSubagentRuntime(tmp_path)
    await restored.recover()
    saved = await restored.lookup(job.job_id, owner=_owner(), depth=0)
    assert saved.approval_state == {"owners": [["run", "tool", "shell"]]}
    assert saved.human_paused
    assert saved.status == "awaiting_approval"
    await restored.shutdown()


@pytest.mark.asyncio
async def test_sync_agno_tool_pause_uses_owning_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """A worker-thread FunctionCall must wait on its job's loop and execute once after resume."""
    control = SubagentControl()
    control.pause()
    owner_loop = asyncio.get_running_loop()
    bound = asyncio.Event()
    owner_wait = asyncio.create_task(control.checkpoint())
    owner_loop.call_soon(bound.set)
    await bound.wait()
    worker_checked = asyncio.Event()
    original_checkpoint = tool_hooks.subagent_tool_checkpoint

    async def checkpoint_with_barrier() -> None:
        waiting = asyncio.create_task(original_checkpoint())
        scheduled = asyncio.Event()
        asyncio.get_running_loop().call_soon(scheduled.set)
        await scheduled.wait()
        owner_loop.call_soon_threadsafe(worker_checked.set)
        await waiting

    monkeypatch.setattr(tool_hooks, "subagent_tool_checkpoint", checkpoint_with_barrier)
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
    with subagent_control_context(control):
        worker = asyncio.create_task(asyncio.to_thread(call.execute))
        await worker_checked.wait()
        assert not results
        control.resume()
        result = await worker
    await owner_wait
    assert result.status == "success"
    assert result.result == "done"
    assert results == ["executed"]


@pytest.mark.asyncio
async def test_retained_cleanup_can_cancel_after_authorization_revocation(tmp_path: Path) -> None:
    """Revocation blocks public control but cannot prevent exact persisted approval cleanup."""
    allowed = True
    runtime = BackgroundSubagentRuntime(tmp_path, authorize=lambda _job: allowed)

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome("awaiting_approval")

    job = await runtime.start(_child(), owner=_owner(), operation=operation)
    result = await runtime.wait(job.job_id, owner=_owner(), depth=0)
    await runtime.release_wait(job.job_id, result.token)
    allowed = False
    with pytest.raises(ValueError, match="not available"):
        await runtime.cancel(job.job_id, owner=_owner(), depth=0)
    assert not await runtime.cancel_retained(replace(job.child, run_id="other"))
    assert await runtime.cancel_retained(job.child)
    assert job.status == "cancelled"
    await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("continuation", [False, True])
async def test_cancelled_admission_still_launches_owned_operation_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    continuation: bool,
) -> None:
    """A cancelled parent cannot strand a durably accepted job between its write and launch."""
    runtime = BackgroundSubagentRuntime(tmp_path)
    child = _child()

    async def approval() -> BackgroundOutcome:
        return BackgroundOutcome("awaiting_approval")

    if continuation:
        await runtime.start(child, owner=_owner(), operation=approval)
        previous = await runtime.wait(child.delegation_id, owner=_owner(), depth=0)
        await runtime.acknowledge_wait(child.delegation_id, previous.token)
    written, executing, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
    release_writer = threading.Event()
    owner_loop = asyncio.get_running_loop()
    original_writer = background.write_json_file_durable

    def blocked_writer(path: Path, payload: object) -> None:
        original_writer(path, payload)
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
        runtime.continue_job(child.delegation_id, owner=_owner(), depth=0, operation=operation)
        if continuation
        else runtime.start(child, owner=_owner(), operation=operation),
    )
    try:
        await written.wait()
        admission.cancel()
        release_writer.set()
        with pytest.raises(asyncio.CancelledError):
            await admission
        assert executing.is_set()
        finish.set()
        result = await runtime.wait(child.delegation_id, owner=_owner(), depth=0)
        assert result.job.result == "survived admission cancellation"
        assert calls == 1
    finally:
        release_writer.set()
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_shutdown_keeps_native_result_committed_before_outcome_publication(tmp_path: Path) -> None:
    """Shutdown cannot replace a committed native answer with an interruption notice."""
    runtime = BackgroundSubagentRuntime(tmp_path)
    child = _child()
    committed = asyncio.Event()

    async def operation() -> BackgroundOutcome:
        child.status = "completed"
        child.result = "Exact durable completed answer"
        committed.set()
        await asyncio.Event().wait()
        raise AssertionError

    job = await runtime.start(child, owner=_owner(), operation=operation)
    await committed.wait()
    await runtime.shutdown()
    assert job.status == "completed"
    assert job.result == "Exact durable completed answer"


@pytest.mark.asyncio
async def test_restart_adopts_native_completion_found_by_reconciliation(tmp_path: Path) -> None:
    """Startup reconciliation is authoritative when native storage proves completion."""
    runtime = BackgroundSubagentRuntime(tmp_path)
    child = _child()

    async def operation() -> BackgroundOutcome:
        await asyncio.Event().wait()
        raise AssertionError

    await runtime.start(child, owner=_owner(), operation=operation)
    await runtime.shutdown()
    path = tmp_path / "background_subagents" / f"{child.delegation_id}.json"
    snapshot = json.loads(path.read_text())
    snapshot["status"] = "running"
    snapshot["result"] = None
    background.write_json_file_durable(path, snapshot)

    async def reconcile(retained: DelegationChild) -> None:
        retained.status = "completed"
        retained.result = "Exact durable completed answer"

    restored = BackgroundSubagentRuntime(tmp_path, cancel=reconcile)
    try:
        await restored.recover()
        job = await restored.lookup(child.delegation_id, owner=_owner(), depth=0)
        assert job.status == "completed"
        assert job.result == "Exact durable completed answer"
    finally:
        await restored.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("restart_again", [False, True])
async def test_recovered_approval_holds_human_followup_before_reattachment(
    tmp_path: Path,
    restart_again: bool,
) -> None:
    """Recovered approvals subscribe before ingress and retain human holds without a live execution task."""
    owner = replace(_owner(), transport_agent_name="team")
    runtime = BackgroundSubagentRuntime(tmp_path)

    async def approval() -> BackgroundOutcome:
        return BackgroundOutcome("awaiting_approval", approval_state={"owners": [["call", "shell"]]})

    job = await runtime.start(_child(), owner=owner, operation=approval)
    waiting = await runtime.wait(job.job_id, owner=owner, depth=0)
    await runtime.release_wait(job.job_id, waiting.token)
    await runtime.shutdown()
    restored = BackgroundSubagentRuntime(tmp_path)
    executed = asyncio.Event()

    async def continuation() -> BackgroundOutcome:
        await subagent_tool_checkpoint()
        executed.set()
        return BackgroundOutcome("completed", "Resumed explicitly")

    try:
        await restored.recover()
        signal = restored.human_signal_for("team", "!room:test", "$root")
        assert signal.has_subscribers
        restored.changed.clear()
        signal.notify()
        signal.clear()
        async with asyncio.timeout(5):
            await restored.changed.wait()
        # Inspect disk before any lookup or shutdown can persist the hold itself.
        path = tmp_path / "background_subagents" / f"{job.job_id}.json"
        assert json.loads(path.read_text())["human_paused"] is True
        if restart_again:
            await restored.shutdown()
            restored = BackgroundSubagentRuntime(tmp_path)
            await restored.recover()
        await restored.continue_job(job.job_id, owner=owner, depth=0, operation=continuation)
        paused = await restored.wait(job.job_id, owner=owner, depth=0)
        assert paused.job.status == "paused_for_human"
        assert not executed.is_set()
        await restored.resume(job.job_id, owner=owner, depth=0)
        result = await restored.wait(job.job_id, owner=owner, depth=0)
        assert result.job.result == "Resumed explicitly"
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

    runtime = BackgroundSubagentRuntime(tmp_path, cancel=cleanup)

    async def approval() -> BackgroundOutcome:
        return BackgroundOutcome("awaiting_approval")

    job = await runtime.start(_child(), owner=_owner(), operation=approval)
    waiting = await runtime.wait(job.job_id, owner=_owner(), depth=0)
    await runtime.release_wait(job.job_id, waiting.token)
    cancelling = asyncio.create_task(runtime.cancel(job.job_id, owner=_owner(), depth=0))
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
            BackgroundSubagentRuntime(tmp_path)
    finally:
        release_cleanup.set()
        await cancelling
        if cancel_shutdown_waiter:
            with pytest.raises(asyncio.CancelledError):
                await stopping
        else:
            await stopping
    restored = BackgroundSubagentRuntime(tmp_path)
    try:
        await restored.recover()
        assert (await restored.lookup(job.job_id, owner=_owner(), depth=0)).status == "cancelled"
    finally:
        await restored.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation_name",
    ["acknowledge_wait", "acknowledge_delivery", "lookup", "wait", "resume", "cancel"],
)
async def test_closed_runtime_rejects_stale_parent_operations(tmp_path: Path, operation_name: str) -> None:
    """Old response callbacks cannot write after a replacement acquires runtime storage."""
    runtime = BackgroundSubagentRuntime(tmp_path)

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "Saved")

    job = await runtime.start(_child(), owner=_owner(), operation=operation)
    waiting = await runtime.wait(job.job_id, owner=_owner(), depth=0)
    second = await runtime.start(_child("c" * 32), owner=_owner(), operation=operation)
    second_wait = await runtime.wait(second.job_id, owner=_owner(), depth=0)
    await runtime.release_wait(second.job_id, second_wait.token)
    await runtime.claim_delivery(second.job_id, content={"body": "Saved"}, transaction_id="delivery")
    await runtime.shutdown()
    restored = BackgroundSubagentRuntime(tmp_path)
    try:
        await restored.recover()
        operations = {
            "acknowledge_wait": lambda: runtime.acknowledge_wait(job.job_id, waiting.token),
            "acknowledge_delivery": lambda: runtime.acknowledge_delivery(second.job_id, "delivery"),
            "lookup": lambda: runtime.lookup(job.job_id, owner=_owner(), depth=0),
            "wait": lambda: runtime.wait(job.job_id, owner=_owner(), depth=0),
            "resume": lambda: runtime.resume(job.job_id, owner=_owner(), depth=0),
            "cancel": lambda: runtime.cancel(job.job_id, owner=_owner(), depth=0),
        }
        with pytest.raises(ValueError, match="closed"):
            await operations[operation_name]()
        assert await runtime.claim_delivery(job.job_id, content={"body": "Stale"}, transaction_id="stale") is None
        assert await runtime.pending_deliveries() == []
        assert len(await restored.pending_deliveries()) == 2
    finally:
        await restored.shutdown()


@pytest.mark.asyncio
async def test_shutdown_rejects_new_cancel_while_draining_execution(tmp_path: Path) -> None:
    """Shutdown's task drain cannot be bypassed by a newly admitted public cancellation."""
    runtime = BackgroundSubagentRuntime(tmp_path)
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

    await runtime.start(_child(), owner=_owner(), operation=operation)
    await executing.wait()
    second = await runtime.start(_child("c" * 32), owner=_owner(), operation=approval)
    waiting = await runtime.wait(second.job_id, owner=_owner(), depth=0)
    await runtime.release_wait(second.job_id, waiting.token)
    stopping = asyncio.create_task(runtime.shutdown())
    await draining.wait()
    try:
        with pytest.raises(ValueError, match="closed"):
            await runtime.cancel(second.job_id, owner=_owner(), depth=0)
    finally:
        release.set()
        await stopping


@pytest.mark.asyncio
async def test_shutdown_does_not_cancel_an_existing_execution_cleanup_twice(tmp_path: Path) -> None:
    """An accepted cancel already owns execution cancellation; shutdown only drains it."""
    runtime = BackgroundSubagentRuntime(tmp_path)
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

    job = await runtime.start(_child(), owner=_owner(), operation=operation)
    await executing.wait()
    cancelling = asyncio.create_task(runtime.cancel(job.job_id, owner=_owner(), depth=0))
    await cleaning.wait()
    stopping = asyncio.create_task(runtime.shutdown())
    admitted = asyncio.Event()
    asyncio.get_running_loop().call_soon(admitted.set)
    await admitted.wait()
    release.set()
    await cancelling
    await stopping
    assert cleaned.is_set()
