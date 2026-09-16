"""Completion admission uses immutable claims and current consumption state."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

from tests.conftest import unwrap_extracted_collaborator
from tests.response_runner_helpers import _bot, _plain_request

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.message_target import MessageTarget
    from mindroom.response_runner import _EarlyPlaceholderState


import pytest

from mindroom.approval_response import continuation_target
from mindroom.event_journal import (
    ApprovalCall,
    ApprovalCardReservation,
    ApprovalContinuation,
    ApprovalDecision,
    ApprovalDecisionMetadata,
    DeliveryStage,
    PrincipalStore,
)
from mindroom.response_sources import ResponseSources
from mindroom.tool_jobs.completion import admit_job_completion, completion_envelope, completion_event
from mindroom.tool_jobs.runtime import BackgroundOutcome, JobSpec, ToolJobRuntime, register_background_runtime
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.conftest import test_runtime_paths
from tests.response_runner_helpers import _target


async def _persist_waiting_continuation(
    store: PrincipalStore,
    *,
    principal_id: str,
    continuation: ApprovalContinuation,
) -> None:
    """Persist one waiting continuation and its exact approval card."""
    assert await store.create_approval_continuation(continuation) == continuation
    assert await store.reserve_approval_card_deliveries(
        continuation_principal_id=principal_id,
        continuation_id=continuation.approval_id,
        expected_generation=0,
        cards=(
            ApprovalCardReservation(
                delivery_id="approval-card",
                tool_call_id="call-1",
                event_type="io.mindroom.tool_approval",
                payload={
                    "approval_id": "approval-card",
                    "continuation_id": continuation.approval_id,
                    "continuation_generation": 0,
                    "tool_call_id": "call-1",
                    "status": "pending",
                    "tool_name": "dangerous",
                },
            ),
        ),
    )
    assert await store.claim_matrix_delivery(delivery_id="approval-card", stage=DeliveryStage.INITIAL) is not None
    await store.acknowledge_matrix_delivery(
        delivery_id="approval-card",
        stage=DeliveryStage.INITIAL,
        event_id="$approval-card",
        delivered_projections=(),
    )


@pytest.mark.asyncio
async def test_completion_requires_current_unconsumed_exact_claim(tmp_path: Path) -> None:
    """Completion requires current unconsumed exact claim."""
    paths = test_runtime_paths(tmp_path)
    target = _target(thread_id="$thread")
    owner = ToolExecutionIdentity(
        "matrix",
        "general",
        "@human:localhost",
        target.room_id,
        "$thread",
        "$thread",
        target.session_id,
    )
    runtime = ToolJobRuntime(tmp_path)
    register_background_runtime(paths, runtime)

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "answer")

    try:
        await runtime.start(JobSpec("job", "tool", 0), owner=owner, operation=operation)
        waited = await runtime.wait("job", owner=owner, depth=0)
        await runtime.release_wait("job", waited.token)
        envelope = completion_envelope(waited.job, sender_id="@mindroom_general:localhost")
        assert await admit_job_completion(envelope, target=target, runtime_paths=paths)
        assert not await admit_job_completion(
            replace(envelope, tool_job_completion=replace(envelope.tool_job_completion, generation=999)),
            target=target,
            runtime_paths=paths,
        )
        waited = await runtime.wait("job", owner=owner, depth=0)
        await runtime.acknowledge_wait("job", waited.token)
        assert not await admit_job_completion(envelope, target=target, runtime_paths=paths)
    finally:
        register_background_runtime(paths, None)
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_consumed_completion_resumes_its_owned_approval_continuation(tmp_path: Path) -> None:
    """A consumed job notice still dispatches the ready continuation that owns it."""
    bot = _bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    store = runner.deps.approval_store
    paths = runner.deps.runtime_paths
    target = _target(thread_id="$thread")
    owner = ToolExecutionIdentity(
        "matrix",
        "general",
        "@user:localhost",
        target.room_id,
        "$thread",
        "$thread",
        target.session_id,
    )
    runtime = ToolJobRuntime(paths.storage_root)
    register_background_runtime(paths, runtime)

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome("awaiting_approval")

    try:
        await runtime.start(JobSpec("approval-job", "tool", 0), owner=owner, operation=operation)
        initial_wait = await runtime.wait("approval-job", owner=owner, depth=0)
        await runtime.release_wait("approval-job", initial_wait.token)
        completion_wait = await runtime.wait("approval-job", owner=owner, depth=0)
        event = completion_event(completion_wait.job, sender_id="@mindroom_general:localhost")
        source_event_id = event.event_id
        await store.admit(event)
        continuation = ApprovalContinuation(
            approval_id="approval-job-continuation",
            run_id="run-paused",
            session_id=target.session_id,
            entity_kind="agent",
            entity_name="general",
            room_id=target.room_id,
            thread_id=target.resolved_thread_id,
            requester_id=owner.requester_id,
            response_event_id="$waiting",
            sources=ResponseSources((source_event_id,), (source_event_id,)),
            calls=(
                ApprovalCall(
                    tool_call_id="call-1",
                    tool_name="dangerous",
                    invoking_agent="general",
                    expires_at_ns=2**62,
                ),
            ),
            state="waiting",
            runtime_generation=runner.deps.approval_runtime_generation,
            origin=completion_envelope(completion_wait.job, sender_id="@mindroom_general:localhost").origin,
            hook_source="tool_job_completion",
        )
        assert continuation_target(continuation, reply_to_event_id=source_event_id).reply_to_event_id is None
        await _persist_waiting_continuation(
            store,
            principal_id=bot._journal_principal_id,
            continuation=continuation,
        )
        await runtime.acknowledge_wait("approval-job", completion_wait.token)
        decision = await store.resolve_continuation_approval_card(
            card_event_id="$approval-card",
            requested_status="approved",
            reason=None,
            metadata=ApprovalDecisionMetadata(resolved_by="@user:localhost"),
        )
        assert decision.recorded
        assert decision.continuation_ready
        ready = await store.approval_continuation(continuation.approval_id)
        assert ready is not None
        assert ready.state == "ready"
        assert ready.calls[0].decision is ApprovalDecision.APPROVED
        request = _plain_request(target, source_event_id=source_event_id)
        settled: list[bool] = []

        async def settle() -> None:
            settled.append(True)

        request = replace(
            request,
            on_no_response_handled=settle,
            response_envelope=completion_envelope(completion_wait.job, sender_id="@mindroom_general:localhost"),
        )
        duplicate_response = AsyncMock(return_value="$duplicate")
        resume_continuation = AsyncMock(return_value="$waiting")

        with patch.object(runner, "_run_owned_approval_continuation", new=resume_continuation):
            event_id = await runner._run_locked_response_lifecycle(
                request,
                response_kind="test",
                locked_operation=duplicate_response,
                signal_queued_message=False,
            )

        assert event_id == "$waiting"
        duplicate_response.assert_not_awaited()
        resume_continuation.assert_awaited_once()
        claimed = resume_continuation.await_args.args[0]
        assert claimed.state == "claimed"
        assert (await store.approval_continuation(continuation.approval_id)) == claimed
        assert settled == []
    finally:
        register_background_runtime(paths, None)
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("consume", [False, True])
@pytest.mark.parametrize("member_owned", [False, True])
async def test_completion_rechecks_after_real_lifecycle_lock(tmp_path: Path, consume: bool, member_owned: bool) -> None:
    """Queued completion admission reads consumption only after the prior turn releases its lock."""
    bot = _bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    paths = runner.deps.runtime_paths
    target = _target(thread_id="$thread")
    owner = ToolExecutionIdentity(
        "matrix",
        "member" if member_owned else "general",
        "@human:localhost",
        target.room_id,
        "$thread",
        "$thread",
        target.session_id,
        transport_agent_name="general",
    )
    runtime = ToolJobRuntime(paths.storage_root)
    register_background_runtime(paths, runtime)
    first_started, release_first = asyncio.Event(), asyncio.Event()
    settled = []
    executed = []

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "answer")

    async def first_operation(_target: MessageTarget, _placeholder: _EarlyPlaceholderState) -> str:
        first_started.set()
        await release_first.wait()
        return "$first-result"

    async def second_operation(_target: MessageTarget, _placeholder: _EarlyPlaceholderState) -> str:
        executed.append(True)
        return "$second-result"

    async def settle() -> None:
        settled.append(True)

    try:
        await runtime.start(JobSpec("queued-job", "tool", 0), owner=owner, operation=operation)
        waited = await runtime.wait("queued-job", owner=owner, depth=0)
        await runtime.release_wait("queued-job", waited.token)
        request = _plain_request(target)
        envelope = completion_envelope(waited.job, sender_id="@mindroom_general:localhost")
        completion = replace(
            _plain_request(target, source_event_id=envelope.source_event_id),
            on_no_response_handled=settle,
            response_envelope=envelope,
        )
        first = asyncio.create_task(
            runner._run_locked_response_lifecycle(
                request,
                response_kind="test",
                locked_operation=first_operation,
                signal_queued_message=False,
            ),
        )
        await first_started.wait()
        second = asyncio.create_task(
            runner._run_locked_response_lifecycle(
                completion,
                response_kind="test",
                locked_operation=second_operation,
                signal_queued_message=False,
            ),
        )
        await asyncio.sleep(0)
        assert not second.done()
        if consume:
            waited = await runtime.wait("queued-job", owner=owner, depth=0)
            await runtime.acknowledge_wait("queued-job", waited.token)
        release_first.set()
        await first
        result = await second
        assert result == (None if consume else "$second-result")
        assert settled == ([True] if consume else [])
        assert executed == ([] if consume else [True])
    finally:
        release_first.set()
        register_background_runtime(paths, None)
        await runtime.shutdown()
