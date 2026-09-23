"""Foreground response ownership across automatic approval checkpoints."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from itertools import count
from typing import TYPE_CHECKING, Literal
from unittest.mock import AsyncMock, patch

import nio
import pytest
from agno.models.response import ToolExecution

from mindroom.constants import AI_RUN_METADATA_KEY
from mindroom.event_journal import (
    ApprovalCall,
    ApprovalContinuation,
    ApprovalDecision,
    EventClass,
    EventKind,
    InboundEvent,
    ProjectedEvent,
)
from mindroom.response_runner import _DeliveryProgress, _EarlyPlaceholderState
from mindroom.response_sources import ResponseSources
from mindroom.response_turn import CompletedApprovalRun, PausedAttempt
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, serialize_tool_execution_identity
from tests.conftest import unwrap_extracted_collaborator
from tests.response_runner_helpers import _bot, _plain_request, _target

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.approval_response import _ApprovalPausePlan
    from mindroom.message_target import MessageTarget
    from mindroom.response_runner import ResponseRequest, ResponseRunner
    from mindroom.tool_system.events import ToolTraceEntry


async def _runner_with_source(tmp_path: Path) -> ResponseRunner:
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    client = runner._client()
    event_ids = count(1)
    client.room_send.side_effect = lambda *_args, **_kwargs: nio.RoomSendResponse(
        event_id=f"$response-{next(event_ids)}",
        room_id="!room:localhost",
    )
    await runner.deps.approval_store.admit(
        InboundEvent(
            event_id="$source",
            room_id="!room:localhost",
            thread_id="$thread",
            kind=EventKind.MESSAGE,
            event_class=EventClass.ACTIONABLE,
            sender="@user:localhost",
            origin_server_ts=1,
            source={"event_id": "$source", "content": {"body": "Read the reports"}},
        ),
        ProjectedEvent(
            event_id="$source",
            room_id="!room:localhost",
            thread_id="$thread",
            sender="@user:localhost",
            origin_server_ts=1,
            content={"body": "Read the reports"},
            replaces_event_id=None,
            redacts_event_id=None,
        ),
    )
    return runner


def _pause(call_id: str) -> PausedAttempt:
    return PausedAttempt(
        session_id="session-1",
        run_id="run-1",
        tools=(ToolExecution(tool_call_id=call_id, tool_name="read_report", tool_args={}),),
        toolkit_owners={("general", "read_report"): "reports"},
        response_text="The first report is read; another remains.",
    )


async def _seed_ready_continuation(runner: ResponseRunner) -> None:
    continuation = ApprovalContinuation(
        approval_id="approval-1",
        continuation_count=2,
        run_id="run-1",
        session_id="session-1",
        entity_kind="agent",
        entity_name="general",
        room_id="!room:localhost",
        thread_id="$thread",
        requester_id="@user:localhost",
        response_event_id="$original-response",
        sources=ResponseSources(("$source",), ("$source",)),
        calls=(
            ApprovalCall(
                tool_call_id="call-1",
                tool_name="read_report",
                invoking_agent="general",
                toolkit_name="reports",
                expires_at_ns=2**62,
                decision=ApprovalDecision.APPROVED,
                human_approval_required=False,
            ),
        ),
        state="ready",
        show_tool_calls=False,
        execution_identity=serialize_tool_execution_identity(
            ToolExecutionIdentity(
                channel="matrix",
                agent_name="general",
                requester_id="@user:localhost",
                room_id="!room:localhost",
                thread_id="$thread",
                resolved_thread_id="$thread",
                session_id="session-1",
            ),
        ),
    )
    assert await runner.deps.approval_store.create_approval_continuation(continuation) == continuation


@pytest.mark.asyncio
@pytest.mark.parametrize("recover", [False, True])
@pytest.mark.parametrize("terminal_run_id", ["continued-run", None, "", 7])
async def test_final_approval_links_the_delivered_attempt(
    tmp_path: Path,
    terminal_run_id: str | int | None,
    *,
    recover: bool,
) -> None:
    """Live and recovered finals persist the attempt identified by their durable metadata."""
    runner = await _runner_with_source(tmp_path)
    await _seed_ready_continuation(runner)
    target = _target(thread_id="$thread", reply_to_event_id="$source")
    claimed = await runner.deps.approval_store.claim_approval_continuation(
        "approval-1",
        runtime_generation=runner.deps.approval_runtime_generation,
    )
    assert claimed is not None
    persist_event_id = AsyncMock()
    completed = CompletedApprovalRun(
        response_text="Finished after refreshing the tools.",
        metadata_content={AI_RUN_METADATA_KEY: {"run_id": terminal_run_id, "status": "completed"}},
    )
    with (
        patch.object(runner, "_continue_entity_call", new=AsyncMock(return_value=completed)),
        patch.object(runner, "_approval_response_event_persistence", return_value=persist_event_id),
    ):
        if recover:
            await runner._execute_claimed_approval(
                claimed,
                request=_plain_request(target, source_event_id="$source"),
                target=target,
            )
            await runner._recover_claimed_approval_lifecycle(claimed, target=target)
        else:
            await runner._run_claimed_approval_lifecycle(claimed, target=target)

    expected_run_id = terminal_run_id if isinstance(terminal_run_id, str) and terminal_run_id else "run-1"
    persist_event_id.assert_awaited_once_with(expected_run_id, "$original-response")


async def _drain_tasks(*tasks: asyncio.Task[object] | None) -> None:
    """Keep a failed assertion from leaving blocked test tasks behind."""
    started = [task for task in tasks if task is not None]
    for task in started:
        if not task.done():
            task.cancel()
    await asyncio.gather(*started, return_exceptions=True)


async def _run_follow_up(
    runner: ResponseRunner,
    target: MessageTarget,
    dispatched: asyncio.Event,
    order: list[str],
) -> str | None:
    async def answer(
        _target: MessageTarget,
        _early_placeholder: _EarlyPlaceholderState,
    ) -> str:
        order.append("follow-up")
        return "$follow-up-response"

    dispatched.set()
    return await runner._run_locked_response_lifecycle(
        _plain_request(target, source_event_id="$follow-up"),
        response_kind="ai",
        locked_operation=answer,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("checkpoint", ["initial", "chained"])
async def test_automatic_checkpoint_keeps_foreground_until_handoff(
    tmp_path: Path,
    checkpoint: Literal["initial", "chained"],
) -> None:
    """A follow-up cannot run between automatically approved generations."""
    runner = await _runner_with_source(tmp_path)
    target = _target(thread_id="$thread", reply_to_event_id="$source")
    request = _plain_request(target, source_event_id="$source")
    if checkpoint == "chained":
        await _seed_ready_continuation(runner)
    batch_started = asyncio.Event()
    release_batch = asyncio.Event()
    follow_up_dispatched = asyncio.Event()
    order: list[str] = []
    generations: list[int] = []
    lifecycle = runner._lifecycle_coordinator

    async def continue_model(
        continuation: ApprovalContinuation,
        *,
        request: ResponseRequest,
        target: MessageTarget,
        tool_trace_collector: list[ToolTraceEntry],
    ) -> CompletedApprovalRun | PausedAttempt:
        del request, tool_trace_collector
        generations.append(continuation.generation)
        assert continuation.continuation_count == 2
        if checkpoint == "chained" and len(generations) == 1:
            order.append("first batch")
            batch_started.set()
            await release_batch.wait()
            return _pause("call-2")
        assert lifecycle._get_or_create_queued_signal(target).has_pending_human_messages()
        order.append("original handoff")
        return CompletedApprovalRun(
            response_text="A newer message arrived. One report is read; one remains. I will resume after your update.",
            metadata_content={},
        )

    async def initial_model(
        resolved_target: MessageTarget,
        _early_placeholder: _EarlyPlaceholderState,
    ) -> str | None:
        assert checkpoint == "initial"
        order.append("initial model")
        batch_started.set()
        await release_batch.wait()
        await runner._suspend_for_approval(
            replace(_pause("call-1"), continuation_count=2),
            request=request,
            target=resolved_target,
            progress=_DeliveryProgress(),
            execution_identity=runner.deps.tool_runtime.build_execution_identity(
                target=resolved_target,
                user_id=request.user_id,
            ),
            entity_kind="agent",
            history_scope=runner.deps.state_writer.history_scope(),
            show_tool_calls=False,
        )
        return None

    with patch.object(runner, "_continue_entity_call", new=continue_model):
        original = asyncio.create_task(
            runner._run_locked_response_lifecycle(request, response_kind="ai", locked_operation=initial_model),
        )
        follow_up: asyncio.Task[str | None] | None = None
        try:
            await asyncio.wait_for(batch_started.wait(), timeout=5)
            follow_up = asyncio.create_task(
                _run_follow_up(runner, target, follow_up_dispatched, order),
            )
            await asyncio.wait_for(follow_up_dispatched.wait(), timeout=5)
            assert lifecycle._get_or_create_queued_signal(target).has_pending_human_messages()
            release_batch.set()
            await asyncio.wait_for(asyncio.gather(original, follow_up), timeout=5)
        finally:
            release_batch.set()
            await _drain_tasks(original, follow_up)

    assert order == ["initial model" if checkpoint == "initial" else "first batch", "original handoff", "follow-up"]
    assert generations == ([0] if checkpoint == "initial" else [0, 1])
    assert await runner.deps.approval_store.approval_continuation_for_source("$source") is None
    assert not await runner.deps.approval_store.is_pending("$source")
    assert not original.cancelled()
    assert follow_up is not None
    assert not follow_up.cancelled()


@pytest.mark.asyncio
async def test_human_approval_wait_releases_foreground_for_follow_up(tmp_path: Path) -> None:
    """An unresolved human decision must not block a newer conversation turn."""
    runner = await _runner_with_source(tmp_path)
    await _seed_ready_continuation(runner)
    target = _target(thread_id="$thread", reply_to_event_id="$source")
    batch_started = asyncio.Event()
    release_batch = asyncio.Event()
    follow_up_dispatched = asyncio.Event()
    order: list[str] = []

    async def continue_model(
        continuation: ApprovalContinuation,
        *,
        request: ResponseRequest,
        target: MessageTarget,
        tool_trace_collector: list[ToolTraceEntry],
    ) -> PausedAttempt:
        del continuation, request, target, tool_trace_collector
        order.append("first batch")
        batch_started.set()
        await release_batch.wait()
        return replace(
            _pause("call-needs-human"),
            tools=(
                ToolExecution(
                    tool_call_id="call-needs-human",
                    tool_name="read_report",
                    tool_args={},
                    requires_confirmation=True,
                ),
            ),
        )

    async def publish_cards(
        continuation: ApprovalContinuation,
        plan: _ApprovalPausePlan,
        *,
        target: MessageTarget,
        failure_reason: str,
    ) -> None:
        # Only external card publication is replaced: release its real journal
        # publication lease without granting the still-undecided tool call.
        del plan, target, failure_reason
        assert all(call.decision is None for call in continuation.calls)
        activated = await runner.deps.approval_store.activate_approval_continuation(
            continuation.approval_id,
            expected_generation=continuation.generation,
        )
        assert activated is not None
        assert activated.state == "waiting"

    with (
        patch.object(runner, "_continue_entity_call", new=continue_model),
        patch.object(runner._approval_responses, "_publish_cards", new=publish_cards),
    ):
        original = asyncio.create_task(runner._resume_approval_source("$source"))
        follow_up: asyncio.Task[str | None] | None = None
        try:
            await asyncio.wait_for(batch_started.wait(), timeout=5)
            follow_up = asyncio.create_task(_run_follow_up(runner, target, follow_up_dispatched, order))
            await asyncio.wait_for(follow_up_dispatched.wait(), timeout=5)
            assert runner._lifecycle_coordinator._get_or_create_queued_signal(target).has_pending_human_messages()
            release_batch.set()
            await asyncio.wait_for(asyncio.gather(original, follow_up), timeout=5)
        finally:
            release_batch.set()
            await _drain_tasks(original, follow_up)

    assert order == ["first batch", "follow-up"]
    waiting = await runner.deps.approval_store.approval_continuation_for_source("$source")
    assert waiting is not None
    assert waiting.state == "waiting"
    assert waiting.generation == 1
    assert waiting.continuation_count == 2
    assert waiting.calls[0].decision is None
    assert await runner.deps.approval_store.is_pending("$source")
    assert not runner.has_active_response_for_target(target)
    assert not original.cancelled()
    assert follow_up is not None
    assert not follow_up.cancelled()
