"""Focused unit tests for ResponseRunner and ResponseAttemptRunner.

These pin the response-execution seam directly (lifecycle lock, attempt
mechanics, cancellation, streaming vs non-streaming delivery, queued-notice
state, and post-response effects) with mocked collaborators instead of a full
orchestrator/bot boot, so shrinking ``response_runner.py`` has a safety net.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import nio
import pytest
from agno.agent import Agent as AgnoAgent
from agno.db.base import SessionType
from agno.db.sqlite import SqliteDb
from agno.models.response import ToolExecution
from agno.run.agent import RunOutput
from agno.run.base import RunContext, RunStatus
from agno.run.requirement import RunRequirement
from agno.tools.function import Function
from agno.tools.toolkit import Toolkit

from mindroom import agents as agents_module
from mindroom import background_tasks as background_tasks_module
from mindroom import response_runner
from mindroom.background_tasks import wait_for_background_tasks
from mindroom.cancellation import request_task_cancel
from mindroom.config.approval import ApprovalRuleConfig
from mindroom.config.auth import AgentReplyPermission, AuthorizationConfig
from mindroom.constants import (
    DURABLE_FINAL_OUTCOME_KEY,
    STREAM_STATUS_APPROVAL_PENDING,
    STREAM_STATUS_KEY,
    STREAM_STATUS_PENDING,
)
from mindroom.conversation_resolver import ConversationResolver, MessageContext
from mindroom.delivery_gateway import (
    DeliveryGateway,
    EditTextRequest,
    FinalizeStreamedResponseRequest,
    SendTextRequest,
)
from mindroom.dispatch_source import ScheduledHistoryBudget
from mindroom.entity_resolution import current_internal_sender_ids
from mindroom.event_journal import (
    ApprovalCall,
    ApprovalContinuation,
    ApprovalDecision,
    DeliveryStage,
    EventClass,
    EventKind,
    InboundEvent,
    PrincipalStore,
    ProjectedEvent,
)
from mindroom.final_delivery import FinalDeliveryOutcome, StreamTransportOutcome
from mindroom.handled_turns import TurnRecord
from mindroom.history.turn_recorder import TurnRecorder
from mindroom.logging_config import get_logger
from mindroom.matrix.client import DeliveredMatrixEvent
from mindroom.matrix.state import MatrixState
from mindroom.matrix.thread_history_result import ThreadHistoryResult
from mindroom.message_target import MessageTarget, ResponseLifecycleKey
from mindroom.post_response_effects import PostResponseEffectsDeps, ResponseOutcome, apply_post_response_effects
from mindroom.response_attempt import ResponseAttemptDeps, ResponseAttemptRequest, ResponseAttemptRunner
from mindroom.response_lifecycle import ResponseLifecycleCoordinator, response_lifecycle_reservation_context
from mindroom.response_payload_preparation import (
    DispatchPayloadInputs,
    ResponsePayloadPreparation,
    ResponsePayloadPreparer,
)
from mindroom.response_runner import (
    PostLockRequestPreparationError,
    ResponseRequest,
    ResponseRunner,
    _ResponseGenerationOutcome,
    prepare_memory_and_model_context,
)
from mindroom.response_turn import CompletedApprovalRun, PausedAttempt, ResponsePausedForApproval
from mindroom.stop import StopManager
from mindroom.streaming import (
    INTERRUPTED_RESPONSE_NOTE,
    RESTART_INTERRUPTED_RESPONSE_NOTE,
    StreamingDeliveryError,
    StreamingResponse,
)
from mindroom.synthetic_model import SyntheticModel
from mindroom.thread_summary import thread_summary_message_count_hint
from mindroom.timing import DispatchPipelineTiming
from mindroom.tool_system.approval_exemptions import register_tool_approval_exemption
from mindroom.tool_system.runtime_context import ToolDispatchContext
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from mindroom.turn_policy import PreparedDispatch
from mindroom.turn_record import canonicalize_turn_record
from tests.conftest import (
    make_matrix_client_mock,
    make_visible_message,
    patch_response_runner_module,
    replace_response_runner_deps,
    request_envelope,
    unwrap_extracted_collaborator,
)
from tests.response_runner_helpers import (
    _bot,
    _config,
    _envelope,
    _noop_typing,
    _plain_request,
    _target,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Coroutine
    from pathlib import Path
    from typing import Literal

    from nio import AsyncClient

    from mindroom.hooks import MessageEnvelope


def _preparation(target: MessageTarget, envelope: MessageEnvelope) -> ResponsePayloadPreparation:
    return ResponsePayloadPreparation(
        dispatch=PreparedDispatch(
            requester_user_id="@user:localhost",
            context=MessageContext(
                am_i_mentioned=True,
                is_thread=target.resolved_thread_id is not None,
                thread_id=target.resolved_thread_id,
                thread_history=[],
                mentioned_agents=[],
                has_non_agent_mentions=False,
            ),
            target=target,
            correlation_id=envelope.source_event_id,
            envelope=envelope,
        ),
        prompt="hello",
        action_kind="individual",
        payload_inputs=DispatchPayloadInputs(
            message_attachment_ids=(),
            trusted_attachment_ids=(),
            media_events=(),
        ),
        target_member_names=None,
        dispatch_started_at=1.0,
        context_ready_monotonic=2.0,
    )


def _completed_outcome(event_id: str = "$response", body: str = "ok") -> FinalDeliveryOutcome:
    return FinalDeliveryOutcome(
        terminal_status="completed",
        event_id=event_id,
        is_visible_response=True,
        final_visible_body=body,
        delivery_kind="sent",
    )


async def _admit_approval_source(store: PrincipalStore, *, event_id: str = "$source") -> None:
    """Admit one actionable source for journal-owned approval tests."""
    await store.admit(
        InboundEvent(
            event_id=event_id,
            room_id="!room:localhost",
            thread_id="$thread",
            kind=EventKind.MESSAGE,
            event_class=EventClass.ACTIONABLE,
            sender="@user:localhost",
            origin_server_ts=1,
            source={"event_id": event_id, "content": {"body": "run it"}},
        ),
        ProjectedEvent(
            event_id=event_id,
            room_id="!room:localhost",
            thread_id="$thread",
            sender="@user:localhost",
            origin_server_ts=1,
            content={"body": "run it"},
            replaces_event_id=None,
            redacts_event_id=None,
        ),
    )


@pytest.mark.asyncio
async def test_repeated_inbox_drains_keep_failed_recovery_proof_fail_closed() -> None:
    """Later recoverable and empty drains must not erase an earlier unsafe cancellation."""
    runner = ResponseRunner(deps=MagicMock())
    response_started = asyncio.Event()

    async def interrupted_response() -> None:
        response_started.set()
        await asyncio.Event().wait()

    response_task = runner.track_inbox_response(
        interrupted_response(),
        name="test_unrecoverable_interrupted_response",
        recovery_proof_ready=lambda: False,
    )
    await response_started.wait()

    assert await runner.drain_inbox_responses(cancel_after_seconds=0) is False
    assert runner.incomplete_inbox_responses_recoverable is False
    await asyncio.gather(response_task, return_exceptions=True)
    await asyncio.sleep(0)

    recoverable_response_started = asyncio.Event()

    async def recoverable_interrupted_response() -> None:
        recoverable_response_started.set()
        await asyncio.Event().wait()

    recoverable_response_task = runner.track_inbox_response(
        recoverable_interrupted_response(),
        name="test_recoverable_interrupted_response",
        recovery_proof_ready=lambda: True,
    )
    await recoverable_response_started.wait()

    assert await runner.drain_inbox_responses(cancel_after_seconds=0.01) is False
    assert runner.incomplete_inbox_responses_recoverable is False
    await asyncio.gather(recoverable_response_task, return_exceptions=True)
    await asyncio.sleep(0)

    assert await runner.drain_inbox_responses(cancel_after_seconds=0) is True
    assert runner.incomplete_inbox_responses_recoverable is False


@pytest.mark.asyncio
async def test_failed_detached_inbox_response_returns_sources_to_retry_owner() -> None:
    """A post-handoff failure must trigger autonomous dispatch retry immediately."""
    runner = ResponseRunner(deps=MagicMock())
    on_failure = MagicMock()

    async def fail_after_handoff() -> None:
        msg = "delivery failed"
        raise RuntimeError(msg)

    response_task = runner.track_inbox_response(
        fail_after_handoff(),
        name="test_failed_detached_inbox_response",
        recovery_proof_ready=lambda: False,
        on_failure=on_failure,
    )

    await asyncio.gather(response_task, return_exceptions=True)
    await asyncio.sleep(0)

    on_failure.assert_called_once_with()


@pytest.mark.asyncio
async def test_detached_inbox_response_owns_source_until_task_finishes() -> None:
    """Journal replay must not reclaim a source while its response task is alive."""
    runner = ResponseRunner(deps=MagicMock())
    response_started = asyncio.Event()
    release_response = asyncio.Event()

    async def parked_response() -> None:
        response_started.set()
        await release_response.wait()

    response_task = runner.track_inbox_response(
        parked_response(),
        name="test_source_owned_inbox_response",
        recovery_proof_ready=lambda: False,
        source_event_ids=("$reaction",),
    )

    assert runner.has_live_inbox_response("$reaction")
    await response_started.wait()
    release_response.set()
    await response_task
    await asyncio.sleep(0)

    assert not runner.has_live_inbox_response("$reaction")


class RecordingStopManager(StopManager):
    """Real StopManager whose deferred clear is made immediate and observable."""

    def __init__(self) -> None:
        super().__init__()
        self.cleared: list[str] = []

    def clear_message(
        self,
        message_id: str,
        client: AsyncClient,
        remove_button: bool = True,
        delay: float = 5.0,
    ) -> None:
        """Record the clear request and drop tracking without the production delay."""
        del client, remove_button, delay
        self.cleared.append(message_id)
        self.tracked_messages.pop(message_id, None)


def _attempt_runner(tmp_path: Path, stop_manager: StopManager) -> ResponseAttemptRunner:
    return ResponseAttemptRunner(
        ResponseAttemptDeps(
            client=make_matrix_client_mock(),
            stop_manager=stop_manager,
            logger=get_logger("tests.response_attempt"),
            show_stop_button=lambda: False,
            config=_config(tmp_path),
        ),
    )


# ---------------------------------------------------------------------------
# 1. Lifecycle lock: serialization, post-lock history refresh, prepare ordering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_requests_serialize_and_refresh_history_under_lock(tmp_path: Path) -> None:
    """Two requests for one thread serialize; each refreshes history under lock, then prepares the payload."""
    bot = _bot(tmp_path)
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    events: list[str] = []
    refreshed = [ThreadHistoryResult([], is_full_history=True), ThreadHistoryResult([], is_full_history=True)]
    prepare_history_by_turn: dict[int, object] = {}
    gate = asyncio.Event()
    first_turn_started = asyncio.Event()

    def _turn(request: ResponseRequest) -> int:
        return 1 if request.response_envelope.source_event_id == "$event1" else 2

    refresh_calls = 0

    async def fake_fetch(room_id: str, thread_id: str) -> ThreadHistoryResult:
        nonlocal refresh_calls
        assert (room_id, thread_id) == ("!room:localhost", "$thread")
        refresh_calls += 1
        events.append(f"refresh:{refresh_calls}")
        return refreshed[refresh_calls - 1]

    async def spy_prepare(request: ResponseRequest) -> ResponseRequest:
        turn = _turn(request)
        events.append(f"prepare:{turn}")
        prepare_history_by_turn[turn] = request.thread_history
        return replace(request, payload_preparation=None, requires_model_history_refresh=False)

    async def fake_send_placeholder(request: SendTextRequest) -> str:
        assert request.target.reply_to_event_id is not None
        turn = request.target.reply_to_event_id[-1]
        events.append(f"placeholder:{turn}")
        return f"$placeholder{turn}"

    async def fake_run_cancellable_response(**kwargs: object) -> str:
        response_function = kwargs["response_function"]
        await response_function(None)  # type: ignore[operator]
        return "$response"

    async def fake_process_and_respond(request: ResponseRequest, **_kwargs: object) -> _ResponseGenerationOutcome:
        turn = _turn(request)
        events.append(f"respond_start:{turn}")
        if turn == 1:
            first_turn_started.set()
            await gate.wait()
        events.append(f"respond_end:{turn}")
        return _ResponseGenerationOutcome(delivery=_completed_outcome(), run_succeeded=True)

    def _request_for(turn: int) -> ResponseRequest:
        target = _target(thread_id="$thread", reply_to_event_id=f"$event{turn}")
        envelope = _envelope(target, source_event_id=f"$event{turn}")
        return ResponseRequest(
            thread_history=[],
            prompt="hello",
            user_id="@user:localhost",
            response_envelope=envelope,
            payload_preparation=_preparation(target, envelope),
            on_lifecycle_lock_acquired=lambda turn=turn: events.append(f"lock:{turn}"),
        )

    with (
        patch.object(ConversationResolver, "fetch_thread_history", new=AsyncMock(side_effect=fake_fetch)),
        patch.object(bot._request_payload_preparer, "prepare", new=AsyncMock(side_effect=spy_prepare)),
        patch(
            "mindroom.delivery_gateway.DeliveryGateway.send_text",
            new=AsyncMock(side_effect=fake_send_placeholder),
        ),
        patch.object(
            coordinator,
            "_run_cancellable_response",
            new=AsyncMock(side_effect=fake_run_cancellable_response),
        ),
        patch.object(coordinator, "_process_and_respond", new=AsyncMock(side_effect=fake_process_and_respond)),
        patch_response_runner_module(
            should_use_streaming=AsyncMock(return_value=False),
            apply_post_response_effects=AsyncMock(),
        ),
    ):
        first = asyncio.create_task(coordinator.generate_response(_request_for(1)))
        await asyncio.wait_for(first_turn_started.wait(), timeout=2)
        second = asyncio.create_task(coordinator.generate_response(_request_for(2)))
        for _ in range(20):
            await asyncio.sleep(0)
        # The second turn must not enter the locked section while the first is in flight.
        assert "lock:2" not in events
        gate.set()
        assert await asyncio.wait_for(first, timeout=2) == "$response"
        assert await asyncio.wait_for(second, timeout=2) == "$response"

    assert events == [
        "lock:1",
        "placeholder:1",
        "refresh:1",
        "prepare:1",
        "respond_start:1",
        "respond_end:1",
        "lock:2",
        "placeholder:2",
        "refresh:2",
        "prepare:2",
        "respond_start:2",
        "respond_end:2",
    ]
    # Each turn's payload preparation consumed the history refreshed under its own lock.
    assert prepare_history_by_turn[1] is refreshed[0]
    assert prepare_history_by_turn[2] is refreshed[1]


@pytest.mark.asyncio
async def test_queued_response_rechecks_room_membership_after_acquiring_lifecycle_lock(tmp_path: Path) -> None:
    """A room-backed grant revoked during lock wait must prevent model execution."""
    bot = _bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    config = runner.deps.runtime.config
    config.authorization = AuthorizationConfig(
        default_room_access=True,
        agent_reply_permissions={
            "general": AgentReplyPermission(joined_rooms=["grant"]),
        },
    )
    grant_room_id = "!grant:localhost"
    state = MatrixState.load(runtime_paths=runner.deps.runtime_paths)
    state.add_room("grant", grant_room_id, "#grant:localhost", "Grant")
    state.save(runtime_paths=runner.deps.runtime_paths)
    membership_client = AsyncMock()
    membership_client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=[grant_room_id])
    membership_client.joined_members.return_value = nio.JoinedMembersResponse(
        members=[nio.RoomMember("@user:localhost", None, None)],
        room_id=grant_room_id,
    )
    memberships = runner.deps.runtime.agent_reply_memberships
    await memberships.refresh(config, runner.deps.runtime_paths, membership_client)

    first_started = asyncio.Event()
    release_first = asyncio.Event()
    model_sources: list[str] = []
    second_suppressed = AsyncMock()

    async def fake_run_cancellable_response(**kwargs: object) -> str:
        response_function = kwargs["response_function"]
        await response_function(None)  # type: ignore[operator]
        return "$response"

    async def fake_process_and_respond(request: ResponseRequest, **_kwargs: object) -> _ResponseGenerationOutcome:
        model_sources.append(request.response_envelope.source_event_id)
        if request.response_envelope.source_event_id == "$first":
            first_started.set()
            await release_first.wait()
        return _ResponseGenerationOutcome(delivery=_completed_outcome(), run_succeeded=True)

    first_request = _plain_request(_target(thread_id="$thread"), source_event_id="$first")
    second_request = replace(
        _plain_request(_target(thread_id="$thread"), source_event_id="$second"),
        on_source_turn_suppressed=second_suppressed,
    )
    with (
        patch(
            "mindroom.delivery_gateway.DeliveryGateway.send_text",
            new=AsyncMock(return_value="$placeholder"),
        ),
        patch.object(runner, "_run_cancellable_response", new=AsyncMock(side_effect=fake_run_cancellable_response)),
        patch.object(runner, "_process_and_respond", new=AsyncMock(side_effect=fake_process_and_respond)),
        patch_response_runner_module(
            should_use_streaming=AsyncMock(return_value=False),
            apply_post_response_effects=AsyncMock(),
        ),
    ):
        first = asyncio.create_task(runner.generate_response(first_request))
        await asyncio.wait_for(first_started.wait(), timeout=2)
        second = asyncio.create_task(runner.generate_response(second_request))
        await asyncio.sleep(0)
        leave = nio.RoomMemberEvent.from_dict(
            {
                "type": "m.room.member",
                "event_id": "$leave",
                "sender": "@user:localhost",
                "state_key": "@user:localhost",
                "origin_server_ts": 1,
                "content": {"membership": "leave"},
                "unsigned": {"prev_content": {"membership": "join"}},
            },
        )
        assert isinstance(leave, nio.RoomMemberEvent)
        memberships.apply_member_event(
            config,
            grant_room_id,
            leave,
            control_user_id="@mindroom_router:localhost",
        )
        release_first.set()

        assert await asyncio.wait_for(first, timeout=2) == "$response"
        assert await asyncio.wait_for(second, timeout=2) is None

    assert model_sources == ["$first"]
    second_suppressed.assert_awaited_once_with()


def _async_callback[**Args](callback: Callable[Args, object]) -> Callable[Args, Coroutine[Any, Any, None]]:
    """Adapt a recording callback to the awaitable outcome-callback contract."""

    async def invoke(*args: Args.args, **kwargs: Args.kwargs) -> None:
        callback(*args, **kwargs)

    return invoke


async def _suppress_source_turn() -> bool:
    """Report the source terminal, the way a redaction tombstone does under the lock."""
    return True


@pytest.mark.asyncio
async def test_begin_locked_turn_suppresses_source_redacted_before_response_registration(tmp_path: Path) -> None:
    """A durable tombstone observed under the lock must prevent every persistence side effect."""
    bot = _bot(tmp_path)
    target = _target(thread_id="$thread", reply_to_event_id="$event")
    envelope = _envelope(target, source_event_id="$event")
    delivery_gateway = MagicMock(spec=DeliveryGateway)
    delivery_gateway.send_text = AsyncMock(return_value="$placeholder")
    request_preparer = MagicMock(spec=ResponsePayloadPreparer)
    request_preparer.prepare = AsyncMock()
    runner = ResponseRunner(
        replace(
            unwrap_extracted_collaborator(bot._response_runner).deps,
            delivery_gateway=delivery_gateway,
            request_preparer=request_preparer,
        ),
    )
    preparations = 0
    on_source_turn_suppressed = AsyncMock()

    async def prepare_source_turn() -> bool:
        nonlocal preparations
        preparations += 1
        return True

    request = ResponseRequest(
        thread_history=[],
        prompt="REDACTED_SECRET",
        user_id="@user:localhost",
        response_envelope=envelope,
        payload_preparation=_preparation(target, envelope),
        prepare_source_turn=prepare_source_turn,
        on_source_turn_suppressed=on_source_turn_suppressed,
    )

    prepared_request = await runner._begin_locked_turn(
        request,
        resolved_target=target,
        history_scope=runner.deps.state_writer.history_scope(),
        execution_identity=runner.deps.tool_runtime.build_execution_identity(
            target=target,
            user_id=request.user_id,
        ),
        placeholder_message="Thinking...",
    )

    assert prepared_request is None
    assert preparations == 1
    delivery_gateway.send_text.assert_not_awaited()
    request_preparer.prepare.assert_not_awaited()
    on_source_turn_suppressed.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_begin_locked_turn_waits_for_cancelled_source_preparation(tmp_path: Path) -> None:
    """Cancellation must not release the lifecycle lock while cleanup still mutates storage."""
    bot = _bot(tmp_path)
    target = _target(thread_id="$thread", reply_to_event_id="$event")
    runner = unwrap_extracted_collaborator(bot._response_runner)
    preparation_started = asyncio.Event()
    allow_preparation_finish = asyncio.Event()
    retries: list[str] = []

    async def prepare_source_turn() -> bool:
        preparation_started.set()
        await allow_preparation_finish.wait()
        return False

    request = ResponseRequest(
        thread_history=[],
        prompt="prompt",
        user_id="@user:localhost",
        response_envelope=_envelope(target, source_event_id="$event"),
        prepare_source_turn=prepare_source_turn,
        on_interrupted_response_recoverable=lambda: retries.append("retry"),
    )
    preparation_task = asyncio.create_task(
        runner._begin_locked_turn(
            request,
            resolved_target=target,
            history_scope=runner.deps.state_writer.history_scope(),
            execution_identity=runner.deps.tool_runtime.build_execution_identity(
                target=target,
                user_id=request.user_id,
            ),
        ),
    )
    await asyncio.wait_for(preparation_started.wait(), timeout=2)

    request_task_cancel(preparation_task, cancel_source="sync_restart")
    await asyncio.sleep(0)

    assert preparation_task.done() is False
    allow_preparation_finish.set()
    with pytest.raises(asyncio.CancelledError):
        await preparation_task
    assert retries == []


@pytest.mark.asyncio
async def test_user_stop_cancels_live_response_before_terminalizing_under_its_lock(tmp_path: Path) -> None:
    """STOP must cancel the lock owner before it records the durable terminal turn."""
    bot = _bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    target = _target(thread_id="$thread", reply_to_event_id="$event")
    lifecycle_lock = runner._lifecycle_coordinator._response_lifecycle_lock(target)
    await lifecycle_lock.acquire()
    response_task = asyncio.create_task(asyncio.Event().wait())
    bot.stop_manager.set_current("$response", target, response_task)
    finalize = AsyncMock(return_value=True)

    stop_task = asyncio.create_task(
        runner.finalize_user_stop("$response", "$source", target, 7, Mock(return_value=True), finalize),
    )
    await asyncio.gather(response_task, return_exceptions=True)

    finalize.assert_not_awaited()
    lifecycle_lock.release()

    assert await stop_task is True
    finalize.assert_awaited_once_with(False)


@pytest.mark.asyncio
async def test_user_stop_guard_and_cancellation_do_not_yield_between_each_other(tmp_path: Path) -> None:
    """A later tracked edit cannot replace the guarded task before cancellation."""
    bot = _bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    target = _target(thread_id="$thread", reply_to_event_id="$event")
    lifecycle_lock = runner._lifecycle_coordinator._response_lifecycle_lock(target)
    await lifecycle_lock.acquire()
    old_response_task = asyncio.create_task(asyncio.Event().wait())
    later_edit_task = asyncio.create_task(asyncio.Event().wait())
    bot.stop_manager.set_current("$response", target, old_response_task)

    def should_cancel() -> bool:
        asyncio.get_running_loop().call_soon(
            bot.stop_manager.set_current,
            "$response",
            target,
            later_edit_task,
        )
        return True

    stop_task = asyncio.create_task(
        runner.finalize_user_stop("$response", "$source", target, 2, should_cancel, AsyncMock(return_value=True)),
    )
    await asyncio.gather(old_response_task, return_exceptions=True)
    await asyncio.sleep(0)

    assert later_edit_task.done() is False
    assert bot.stop_manager.tracked_messages["$response"].task is later_edit_task
    lifecycle_lock.release()
    assert await stop_task is True
    later_edit_task.cancel()
    await asyncio.gather(later_edit_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_settled_stop_retry_does_not_cancel_later_live_response(tmp_path: Path) -> None:
    """A STOP superseded by a later edit must not cancel that edit while waiting."""
    bot = _bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    target = _target(thread_id="$thread", reply_to_event_id="$event")
    lifecycle_lock = runner._lifecycle_coordinator._response_lifecycle_lock(target)
    await lifecycle_lock.acquire()
    response_task = asyncio.create_task(asyncio.Event().wait())
    bot.stop_manager.set_current("$response", target, response_task)
    should_cancel = Mock(return_value=False)
    finalize = AsyncMock(return_value=True)

    stop_task = asyncio.create_task(
        runner.finalize_user_stop("$response", "$source", target, 2, should_cancel, finalize),
    )
    await asyncio.sleep(0)

    assert response_task.done() is False
    lifecycle_lock.release()

    assert await stop_task is True
    should_cancel.assert_called()
    finalize.assert_awaited_once_with(False)
    assert response_task.done() is False
    response_task.cancel()
    await asyncio.gather(response_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_user_stop_fences_waiting_approval_before_terminal_turn_record(tmp_path: Path) -> None:
    """STOP settles the paused-run owner instead of leaving its cards executable."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    await _admit_approval_source(runner.deps.approval_store)
    waiting = ApprovalContinuation(
        approval_id="approval-stop",
        run_id="run-paused",
        session_id="session-1",
        entity_kind="agent",
        entity_name="general",
        room_id="!room:localhost",
        thread_id="$thread",
        requester_id="@user:localhost",
        response_event_id="$waiting",
        source_event_ids=("$source",),
        calls=(
            ApprovalCall(
                tool_call_id="call-1",
                tool_name="dangerous",
                invoking_agent="general",
                expires_at_ns=9_000_000_000_000_000_000,
            ),
        ),
        state="waiting",
    )
    assert await runner.deps.approval_store.create_approval_continuation(waiting) == waiting

    async def acknowledge_stop_edit(request: EditTextRequest) -> bool:
        assert request.delivery_turn_id == "$source"
        await runner.deps.approval_store.enqueue_delivery(
            turn_id="$source",
            stage=DeliveryStage.FINAL,
            room_id="!room:localhost",
            thread_id="$thread",
            payload={"body": "Stopped by user."},
            edits_event_id="$waiting",
        )
        await runner.deps.approval_store.claim_delivery(turn_id="$source", stage=DeliveryStage.FINAL)
        await runner.deps.approval_store.acknowledge_delivery(
            turn_id="$source",
            stage=DeliveryStage.FINAL,
            event_id="$waiting",
            delivered_projections=(),
        )
        return True

    async def finalize(approval_settled: bool) -> bool:
        assert approval_settled
        assert await runner.deps.approval_store.approval_continuation("approval-stop") is None
        assert not await runner.deps.approval_store.is_pending("$source")
        return True

    with (
        patch("mindroom.approval_response.expire_continuation_approval_cards", new=AsyncMock(return_value=True)),
        patch.object(DeliveryGateway, "edit_text", new=AsyncMock(side_effect=acknowledge_stop_edit)),
    ):
        stopped = await runner.finalize_user_stop(
            "$waiting",
            "$source",
            _target(thread_id="$thread"),
            7,
            Mock(return_value=True),
            finalize,
        )

    assert stopped
    assert await runner.deps.approval_store.approval_continuation("approval-stop") is None
    assert not await runner.deps.approval_store.is_pending("$source")


@pytest.mark.asyncio
async def test_user_stop_preserves_a_claimed_frozen_final_until_success_recovery(tmp_path: Path) -> None:
    """STOP cannot reclassify an attempted successful FINAL while its acknowledgement is unresolved."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    store = runner.deps.approval_store
    await _admit_approval_source(store)
    continuation = ApprovalContinuation(
        approval_id="approval-stop-final",
        run_id="run-1",
        session_id="session-1",
        entity_kind="agent",
        entity_name="general",
        room_id="!room:localhost",
        thread_id="$thread",
        requester_id="@user:localhost",
        response_event_id="$waiting",
        source_event_ids=("$source",),
        calls=(),
        state="ready",
    )
    assert await store.create_approval_continuation(continuation) == continuation
    claimed = await store.claim_approval_continuation(
        continuation.approval_id,
        runtime_generation=runner.deps.approval_runtime_generation,
    )
    assert claimed is not None
    await store.enqueue_delivery(
        turn_id="$source",
        stage=DeliveryStage.FINAL,
        room_id="!room:localhost",
        thread_id="$thread",
        payload={"body": "finished", "formatted_body": "finished"},
        edits_event_id="$waiting",
    )
    assert await store.claim_delivery(turn_id="$source", stage=DeliveryStage.FINAL) is not None

    acknowledge_final = False

    async def recover_final() -> None:
        if acknowledge_final:
            await store.acknowledge_delivery(
                turn_id="$source",
                stage=DeliveryStage.FINAL,
                event_id="$final",
                delivered_projections=(),
            )

    lifecycle = MagicMock(finalize=AsyncMock())
    finalize_stop = AsyncMock(return_value=True)
    expire_cards = AsyncMock(return_value=True)
    with (
        patch.object(DeliveryGateway, "recover_deliveries", new=AsyncMock(side_effect=recover_final)),
        patch.object(runner, "_build_lifecycle", return_value=lifecycle),
        patch("mindroom.approval_response.expire_continuation_approval_cards", new=expire_cards),
    ):
        assert not await runner.finalize_user_stop(
            "$waiting",
            "$source",
            _target(thread_id="$thread"),
            7,
            Mock(return_value=True),
            finalize_stop,
        )
        still_claimed = await store.approval_continuation(continuation.approval_id)
        assert still_claimed is not None
        assert still_claimed.state == "claimed"
        finalize_stop.assert_not_awaited()
        lifecycle.finalize.assert_not_awaited()

        acknowledge_final = True
        assert await runner.finalize_user_stop(
            "$waiting",
            "$source",
            _target(thread_id="$thread"),
            7,
            Mock(return_value=True),
            finalize_stop,
        )

    finalize_stop.assert_awaited_once_with(True)
    lifecycle.finalize.assert_awaited_once()
    expire_cards.assert_not_awaited()
    assert await store.approval_continuation(continuation.approval_id) is None
    assert not await store.is_pending("$source")


@pytest.mark.asyncio
async def test_user_stop_retry_preserves_success_completed_by_source_worker(tmp_path: Path) -> None:
    """A STOP retry cannot overwrite a frozen FINAL that another worker finished between attempts."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    store = runner.deps.approval_store
    await _admit_approval_source(store)
    continuation = ApprovalContinuation(
        approval_id="approval-stop-worker-final",
        run_id="run-1",
        session_id="session-1",
        entity_kind="agent",
        entity_name="general",
        room_id="!room:localhost",
        thread_id="$thread",
        requester_id="@user:localhost",
        response_event_id="$waiting",
        source_event_ids=("$source",),
        calls=(),
        state="ready",
    )
    assert await store.create_approval_continuation(continuation) == continuation
    claimed = await store.claim_approval_continuation(
        continuation.approval_id,
        runtime_generation=runner.deps.approval_runtime_generation,
    )
    assert claimed is not None
    await store.enqueue_delivery(
        turn_id="$source",
        stage=DeliveryStage.FINAL,
        room_id="!room:localhost",
        thread_id="$thread",
        payload={"body": "finished", "formatted_body": "finished"},
        edits_event_id="$waiting",
    )
    assert await store.claim_delivery(turn_id="$source", stage=DeliveryStage.FINAL) is not None

    finalize_stop = AsyncMock(return_value=True)
    lifecycle = MagicMock(finalize=AsyncMock())
    with (
        patch.object(DeliveryGateway, "recover_deliveries", new=AsyncMock()),
        patch.object(runner, "_build_lifecycle", return_value=lifecycle),
    ):
        assert not await runner.finalize_user_stop(
            "$waiting",
            "$source",
            _target(thread_id="$thread"),
            7,
            Mock(return_value=True),
            finalize_stop,
        )
        await store.acknowledge_delivery(
            turn_id="$source",
            stage=DeliveryStage.FINAL,
            event_id="$final",
            delivered_projections=(),
        )
        assert (
            await runner._recover_claimed_approval_lifecycle(
                claimed,
                target=_target(thread_id="$thread"),
            )
            == "$waiting"
        )
        assert await runner.finalize_user_stop(
            "$waiting",
            "$source",
            _target(thread_id="$thread"),
            7,
            Mock(return_value=True),
            finalize_stop,
        )

    finalize_stop.assert_awaited_once_with(True)
    lifecycle.finalize.assert_awaited_once()
    assert await store.approval_continuation(continuation.approval_id) is None


@pytest.mark.asyncio
async def test_user_stop_retry_keeps_turn_owner_after_frozen_final_recovery(tmp_path: Path) -> None:
    """A recovered edit remains owned by the response bubble targeted by the pending STOP."""
    bot = _bot(tmp_path)
    await bot._turn_store.warm()
    runner = unwrap_extracted_collaborator(bot._response_runner)
    turn_store = unwrap_extracted_collaborator(bot._turn_store)
    store = runner.deps.approval_store
    target = _target(thread_id="$thread", reply_to_event_id="$source")
    pending_turn = TurnRecord.create(
        ("$source",),
        response_event_id="$waiting",
        completed=False,
        response_owner="general",
        requester_id="@user:localhost",
        conversation_target=target,
    )
    await turn_store.record_pending_turn(pending_turn)
    await _admit_approval_source(store)
    continuation = ApprovalContinuation(
        approval_id="approval-stop-turn-owner",
        run_id="run-1",
        session_id="session-1",
        entity_kind="agent",
        entity_name="general",
        room_id="!room:localhost",
        thread_id="$thread",
        requester_id="@user:localhost",
        response_event_id="$waiting",
        source_event_ids=("$source",),
        calls=(),
        state="ready",
    )
    assert await store.create_approval_continuation(continuation) == continuation
    claimed = await store.claim_approval_continuation(
        continuation.approval_id,
        runtime_generation=runner.deps.approval_runtime_generation,
    )
    assert claimed is not None
    await store.enqueue_delivery(
        turn_id="$source",
        stage=DeliveryStage.FINAL,
        room_id="!room:localhost",
        thread_id="$thread",
        payload={"body": "finished", "formatted_body": "finished"},
        edits_event_id="$waiting",
    )
    assert await store.claim_delivery(turn_id="$source", stage=DeliveryStage.FINAL) is not None

    lifecycle = MagicMock(finalize=AsyncMock())
    finalize_stopped_response = AsyncMock(return_value=True)
    on_current_stop_finalized = AsyncMock()
    with (
        patch.object(DeliveryGateway, "recover_deliveries", new=AsyncMock()),
        patch.object(runner, "_build_lifecycle", return_value=lifecycle),
        patch.object(
            DeliveryGateway,
            "finalize_user_stopped_response",
            new=finalize_stopped_response,
        ),
    ):
        with pytest.raises(RuntimeError, match="did not become durable"):
            await bot._user_stop_reconciler.finalize(
                "$waiting",
                7,
                on_current_stop_finalized,
            )

        await store.acknowledge_delivery(
            turn_id="$source",
            stage=DeliveryStage.FINAL,
            event_id="$final-edit",
            delivered_projections=(),
        )
        recovered_response_event_id = await runner._recover_claimed_approval_lifecycle(
            claimed,
            target=target,
        )
        assert recovered_response_event_id == "$waiting"
        await turn_store.record_responded_turn(
            canonicalize_turn_record(
                pending_turn,
                response_event_id=recovered_response_event_id,
                completed=True,
            ),
        )

        assert await bot._user_stop_reconciler.finalize(
            "$waiting",
            7,
            on_current_stop_finalized,
        )

    finalize_stopped_response.assert_not_awaited()
    on_current_stop_finalized.assert_awaited_once()
    stopped_turn = turn_store.get_turn_record("$source")
    assert stopped_turn is not None
    assert stopped_turn.response_event_id == "$waiting"
    assert stopped_turn.user_stop_settled_receipt_order == 7


@pytest.mark.asyncio
async def test_failing_continuation_recovers_frozen_success_before_failure_settlement(tmp_path: Path) -> None:
    """A failure fence racing a generated answer cannot retire that answer as a denial."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    store = runner.deps.approval_store
    await _admit_approval_source(store)
    continuation = ApprovalContinuation(
        approval_id="approval-failing-final",
        run_id="run-1",
        session_id="session-1",
        entity_kind="agent",
        entity_name="general",
        room_id="!room:localhost",
        thread_id="$thread",
        requester_id="@user:localhost",
        response_event_id="$waiting",
        source_event_ids=("$source",),
        calls=(),
        state="ready",
    )
    assert await store.create_approval_continuation(continuation) == continuation
    claimed = await store.claim_approval_continuation(
        continuation.approval_id,
        runtime_generation=runner.deps.approval_runtime_generation,
    )
    assert claimed is not None
    failing = await store.request_approval_failure(
        claimed.approval_id,
        "entity removed",
        expected_state="claimed",
        expected_generation=claimed.generation,
        expected_runtime_generation=claimed.runtime_generation,
    )
    assert failing is not None
    await store.enqueue_delivery(
        turn_id="$source",
        stage=DeliveryStage.FINAL,
        room_id="!room:localhost",
        thread_id="$thread",
        payload={
            "body": "* finished",
            "m.new_content": {
                "body": "finished",
                DURABLE_FINAL_OUTCOME_KEY: {"body": "finished", "interactive": None},
            },
        },
        edits_event_id="$waiting",
    )
    assert await store.claim_delivery(turn_id="$source", stage=DeliveryStage.FINAL) is not None
    await store.acknowledge_delivery(
        turn_id="$source",
        stage=DeliveryStage.FINAL,
        event_id="$final-edit",
        delivered_projections=(),
    )
    lifecycle = MagicMock(finalize=AsyncMock())

    with patch.object(runner, "_build_lifecycle", return_value=lifecycle):
        recovered, event_id = await runner._recover_nonready_approval(
            failing,
            target=_target(thread_id="$thread", reply_to_event_id="$source"),
        )

    assert recovered
    assert event_id == "$waiting"
    lifecycle.finalize.assert_awaited_once()
    assert await store.approval_continuation(continuation.approval_id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True], ids=("error", "cancellation"))
async def test_final_recovery_error_fences_current_claim(tmp_path: Path, *, cancelled: bool) -> None:
    """A failed outbox read cannot hide a same-runtime claim until restart."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    store = runner.deps.approval_store
    await _admit_approval_source(store)
    continuation = ApprovalContinuation(
        approval_id=f"approval-recovery-error-{cancelled}",
        run_id="run-1",
        session_id="session-1",
        entity_kind="agent",
        entity_name="general",
        room_id="!room:localhost",
        thread_id="$thread",
        requester_id="@user:localhost",
        response_event_id="$waiting",
        source_event_ids=("$source",),
        calls=(),
        state="ready",
    )
    assert await store.create_approval_continuation(continuation) == continuation
    failure = asyncio.CancelledError() if cancelled else RuntimeError("Agno continuation failed")

    with (
        patch.object(runner, "_run_claimed_approval_lifecycle", new=AsyncMock(side_effect=failure)),
        patch.object(
            runner._approval_responses,
            "final_delivery",
            new=AsyncMock(side_effect=RuntimeError("outbox temporarily unavailable")),
        ),
        patch.object(runner._approval_responses, "settle_failure", new=AsyncMock(return_value=False)),
    ):
        if cancelled:
            with pytest.raises(asyncio.CancelledError):
                await runner._run_owned_or_locked_response(
                    _plain_request(_target(thread_id="$thread"), source_event_id="$source"),
                    target=_target(thread_id="$thread"),
                    early_placeholder=response_runner._EarlyPlaceholderState(),
                    locked_operation=AsyncMock(),
                )
        else:
            assert (
                await runner._run_owned_or_locked_response(
                    _plain_request(_target(thread_id="$thread"), source_event_id="$source"),
                    target=_target(thread_id="$thread"),
                    early_placeholder=response_runner._EarlyPlaceholderState(),
                    locked_operation=AsyncMock(),
                )
                is None
            )

    retained = await store.approval_continuation(continuation.approval_id)
    assert retained is not None
    assert retained.state == "failing"


@pytest.mark.asyncio
async def test_begin_locked_turn_settles_external_placeholder_when_source_is_redacted(tmp_path: Path) -> None:
    """Suppression must not leave an interactive acknowledgement stuck on Processing."""
    bot = _bot(tmp_path)
    target = _target(thread_id="$thread", reply_to_event_id="$event")
    envelope = _envelope(target, source_event_id="$event")
    delivery_gateway = MagicMock(spec=DeliveryGateway)
    delivery_gateway.deliver_cancelled_visible_note = AsyncMock(
        return_value=FinalDeliveryOutcome(terminal_status="cancelled", event_id="$ack"),
    )
    runner = ResponseRunner(
        replace(
            unwrap_extracted_collaborator(bot._response_runner).deps,
            delivery_gateway=delivery_gateway,
        ),
    )
    on_source_turn_suppressed = AsyncMock()
    request = ResponseRequest(
        thread_history=[],
        prompt="REDACTED_SECRET",
        user_id="@user:localhost",
        response_envelope=envelope,
        existing_event_id="$ack",
        existing_event_is_placeholder=True,
        prepare_source_turn=_suppress_source_turn,
        on_source_turn_suppressed=on_source_turn_suppressed,
    )

    prepared_request = await runner._begin_locked_turn(
        request,
        resolved_target=target,
        history_scope=runner.deps.state_writer.history_scope(),
        execution_identity=runner.deps.tool_runtime.build_execution_identity(
            target=target,
            user_id=request.user_id,
        ),
    )

    assert prepared_request is None
    delivery_gateway.deliver_cancelled_visible_note.assert_awaited_once()
    cancellation_request = delivery_gateway.deliver_cancelled_visible_note.await_args.args[0]
    assert cancellation_request.event_id == "$ack"
    assert cancellation_request.existing_event_is_placeholder is True
    on_source_turn_suppressed.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_begin_locked_turn_excludes_early_placeholder_from_refreshed_history(tmp_path: Path) -> None:
    """The early placeholder must not re-enter payload, memory, or summary inputs through refresh."""
    bot = _bot(tmp_path)
    target = _target(thread_id="$thread", reply_to_event_id="$event")
    envelope = _envelope(target, source_event_id="$event")
    refreshed_history = ThreadHistoryResult(
        [
            make_visible_message(sender="@user:localhost", body="history", event_id="$history"),
            make_visible_message(
                sender="@agent:localhost",
                body="Thinking...",
                event_id="$placeholder",
                content={STREAM_STATUS_KEY: STREAM_STATUS_PENDING},
            ),
        ],
        is_full_history=True,
        diagnostics={"cache_status": "fresh"},
    )
    resolver = MagicMock(spec=ConversationResolver)
    resolver.fetch_thread_history = AsyncMock(return_value=refreshed_history)
    request_preparer = MagicMock(spec=ResponsePayloadPreparer)
    request_preparer.prepare = AsyncMock(side_effect=lambda request: replace(request, payload_preparation=None))
    delivery_gateway = MagicMock(spec=DeliveryGateway)
    delivery_gateway.send_text = AsyncMock(return_value="$placeholder")
    on_visible_response = AsyncMock()
    runner = ResponseRunner(
        replace(
            unwrap_extracted_collaborator(bot._response_runner).deps,
            resolver=resolver,
            request_preparer=request_preparer,
            delivery_gateway=delivery_gateway,
        ),
    )
    request = ResponseRequest(
        thread_history=[],
        prompt="hello",
        user_id="@user:localhost",
        response_envelope=envelope,
        payload_preparation=_preparation(target, envelope),
        on_visible_response=on_visible_response,
    )

    prepared_request = await runner._begin_locked_turn(
        request,
        resolved_target=target,
        history_scope=runner.deps.state_writer.history_scope(),
        execution_identity=runner.deps.tool_runtime.build_execution_identity(
            target=target,
            user_id=request.user_id,
        ),
        placeholder_message="Thinking...",
    )

    assert prepared_request is not None
    assert isinstance(prepared_request.thread_history, ThreadHistoryResult)
    assert [message.event_id for message in prepared_request.thread_history] == ["$history"]
    assert prepared_request.thread_history.is_full_history is True
    assert prepared_request.thread_history.diagnostics == {"cache_status": "fresh"}
    assert prepared_request.existing_event_id == "$placeholder"
    assert prepared_request.existing_event_is_placeholder is True
    on_visible_response.assert_awaited_once_with("$placeholder")


@pytest.mark.asyncio
async def test_setup_cancellation_preserves_cancel_when_placeholder_cleanup_fails(tmp_path: Path) -> None:
    """Placeholder cleanup failure must not replace the original setup cancellation."""
    bot = _bot(tmp_path)
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    setup_started = asyncio.Event()

    async def blocked_streaming_check(*_args: object, **_kwargs: object) -> bool:
        setup_started.set()
        await asyncio.Event().wait()
        return False

    cancelled_note = AsyncMock(side_effect=RuntimeError("Matrix unavailable"))
    with (
        patch(
            "mindroom.delivery_gateway.DeliveryGateway.send_text",
            new=AsyncMock(return_value="$placeholder"),
        ),
        patch_response_runner_module(should_use_streaming=AsyncMock(side_effect=blocked_streaming_check)),
        patch(
            "mindroom.delivery_gateway.DeliveryGateway.deliver_cancelled_visible_note",
            new=cancelled_note,
        ),
    ):
        response = asyncio.create_task(coordinator.generate_response(_plain_request(_target())))
        await asyncio.wait_for(setup_started.wait(), timeout=1.0)
        response.cancel("sync_restart")
        with pytest.raises(asyncio.CancelledError, match="sync_restart"):
            await response

    cancelled_note.assert_awaited_once()


@pytest.mark.asyncio
async def test_early_placeholder_failure_preserves_non_preparation_error_cause(tmp_path: Path) -> None:
    """Only the preparation wrapper is unwrapped when linking an early placeholder failure."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    underlying_error = ValueError("underlying failure")
    proximate_error = RuntimeError("proximate setup failure")
    proximate_error.__cause__ = underlying_error

    async def fail_after_placeholder(
        _target: MessageTarget,
        early_placeholder: response_runner._EarlyPlaceholderState,
    ) -> str | None:
        early_placeholder.placeholder_event_id = "$placeholder"
        raise proximate_error

    with pytest.raises(PostLockRequestPreparationError) as exc_info:
        await runner._run_locked_response_lifecycle(
            _plain_request(_target()),
            response_kind="agent",
            locked_operation=fail_after_placeholder,
        )

    assert exc_info.value.placeholder_event_id == "$placeholder"
    assert exc_info.value.__cause__ is proximate_error
    assert exc_info.value.__cause__.__cause__ is underlying_error


@pytest.mark.asyncio
async def test_replayed_source_adopts_journal_owned_approval_continuation(tmp_path: Path) -> None:
    """A crash after suspension persistence must not execute the inbound source a second time."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    request = _plain_request(_target(thread_id="$thread"), source_event_id="$source")
    await _admit_approval_source(runner.deps.approval_store)
    continuation = ApprovalContinuation(
        approval_id="approval-replay",
        run_id="run-paused",
        session_id="session-1",
        entity_kind="agent",
        entity_name="general",
        room_id=request.room_id,
        thread_id=request.thread_id,
        requester_id="@user:localhost",
        response_event_id="$waiting",
        calls=(
            ApprovalCall(
                tool_call_id="call-1",
                tool_name="dangerous",
                invoking_agent="general",
                expires_at_ns=9_000_000_000_000_000_000,
            ),
        ),
        execution_identity={
            "channel": "matrix",
            "agent_name": "general",
            "requester_id": "@user:localhost",
            "room_id": "!room:localhost",
            "thread_id": "$thread",
            "resolved_thread_id": "$thread",
            "session_id": "session-1",
        },
        source_event_ids=(request.response_envelope.source_event_id,),
        state="waiting",
    )
    assert await runner.deps.approval_store.create_approval_continuation(continuation) == continuation
    locked_operation = AsyncMock(return_value="$duplicate")

    event_id = await runner._run_owned_or_locked_response(
        request,
        target=request.response_envelope.target,
        early_placeholder=response_runner._EarlyPlaceholderState(),
        locked_operation=locked_operation,
    )

    assert event_id is None
    locked_operation.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("revoked_layer", ["room", "entity"])
async def test_ready_approval_replay_rechecks_current_authorization(
    tmp_path: Path,
    revoked_layer: str,
) -> None:
    """A ready continuation must fail safely instead of executing after revocation."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    request = _plain_request(_target(thread_id="$thread"), source_event_id="$source")
    await _admit_approval_source(runner.deps.approval_store)
    continuation = ApprovalContinuation(
        approval_id="approval-revoked",
        run_id="run-paused",
        session_id="session-1",
        entity_kind="agent",
        entity_name="general",
        room_id=request.room_id,
        thread_id=request.thread_id,
        requester_id="@user:localhost",
        response_event_id="$waiting",
        source_event_ids=("$source",),
        calls=(),
        state="ready",
    )
    assert await runner.deps.approval_store.create_approval_continuation(continuation) == continuation
    if revoked_layer == "room":
        runner.deps.runtime.config.authorization = AuthorizationConfig(
            default_room_access=False,
            room_permissions={request.room_id: []},
            agent_reply_permissions={
                "general": AgentReplyPermission(users=[continuation.requester_id]),
            },
        )
    else:
        runner.deps.runtime.config.authorization = AuthorizationConfig(
            default_room_access=True,
            agent_reply_permissions={
                "general": AgentReplyPermission(users=[]),
            },
        )
    failing = replace(
        continuation,
        state="failing",
        failure_reason="Current authorization no longer permits this tool approval continuation.",
    )

    with (
        patch.object(
            runner._approval_responses,
            "request_failure",
            new=AsyncMock(return_value=failing),
        ) as request_failure,
        patch.object(runner._approval_responses, "settle_failure", new=AsyncMock(return_value=True)) as settle_failure,
        patch.object(
            runner,
            "_run_claimed_approval_lifecycle",
            new=AsyncMock(side_effect=AssertionError("revoked continuation executed")),
        ) as execute,
    ):
        event_id = await runner._run_owned_or_locked_response(
            request,
            target=request.response_envelope.target,
            early_placeholder=response_runner._EarlyPlaceholderState(),
            locked_operation=AsyncMock(return_value="$duplicate"),
        )

    assert event_id == "$waiting"
    request_failure.assert_awaited_once()
    settle_failure.assert_awaited_once_with(
        failing,
        "Current authorization no longer permits this tool approval continuation.",
    )
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_incomplete_resume_failure_keeps_the_source_unhandled(tmp_path: Path) -> None:
    """A visible error is not terminal while its continuation still owns the source."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    request = _plain_request(_target(thread_id="$thread"), source_event_id="$source")
    await _admit_approval_source(runner.deps.approval_store)
    continuation = ApprovalContinuation(
        approval_id="approval-incomplete-failure",
        run_id="run-paused",
        session_id="session-1",
        entity_kind="agent",
        entity_name="general",
        room_id=request.room_id,
        thread_id=request.thread_id,
        requester_id="@user:localhost",
        response_event_id="$waiting",
        source_event_ids=("$source",),
        calls=(
            ApprovalCall(
                tool_call_id="call-1",
                tool_name="dangerous",
                invoking_agent="general",
                expires_at_ns=9_000_000_000_000_000_000,
                decision=ApprovalDecision.APPROVED,
            ),
        ),
        state="ready",
    )
    assert await runner.deps.approval_store.create_approval_continuation(continuation) == continuation
    incomplete = FinalDeliveryOutcome(
        terminal_status="error",
        event_id="$waiting",
        is_visible_response=True,
        failure_reason="Matrix unavailable",
    )

    with patch.object(
        runner,
        "_run_claimed_approval_lifecycle",
        new=AsyncMock(return_value=incomplete),
    ):
        event_id = await runner._run_owned_or_locked_response(
            request,
            target=request.response_envelope.target,
            early_placeholder=response_runner._EarlyPlaceholderState(),
            locked_operation=AsyncMock(return_value="$duplicate"),
        )

    retained = await runner.deps.approval_store.approval_continuation(continuation.approval_id)
    assert event_id is None
    assert retained is not None
    assert retained.state == "claimed"
    assert await runner.deps.approval_store.is_pending("$source")


@pytest.mark.asyncio
async def test_waiting_message_without_continuation_replays_the_safe_paused_turn(tmp_path: Path) -> None:
    """A crash before row creation replays because the paused tool has not executed."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    request = _plain_request(_target(thread_id="$thread"), source_event_id="$source")
    await _admit_approval_source(runner.deps.approval_store)

    paused = PausedAttempt(
        session_id="session-1",
        run_id="run-paused",
        tools=(ToolExecution(tool_call_id="call-1", tool_name="dangerous", requires_confirmation=True),),
    )
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@user:localhost",
        room_id=request.room_id,
        thread_id=request.thread_id,
        resolved_thread_id=request.response_envelope.target.resolved_thread_id,
        session_id=paused.session_id,
    )
    with (
        patch.object(
            DeliveryGateway,
            "send_text",
            new=AsyncMock(return_value="$waiting"),
        ),
        patch("mindroom.response_runner.uuid4", return_value=MagicMock(hex="approval-cancel")),
        patch("mindroom.approval_response.resolve_tool_approval_approver", return_value="@user:localhost"),
        patch("mindroom.approval_response.evaluate_tool_approval", new=AsyncMock(return_value=(True, 60.0))),
        patch(
            "mindroom.approval_response.ApprovalResponseCoordinator.create",
            new=AsyncMock(side_effect=RuntimeError("crash before continuation commit")),
        ),
        pytest.raises(RuntimeError, match="crash before continuation commit"),
    ):
        await runner._suspend_for_approval(
            paused,
            request=request,
            target=request.response_envelope.target,
            progress=response_runner._DeliveryProgress(),
            execution_identity=identity,
            entity_kind="agent",
            history_scope=runner.deps.state_writer.history_scope(),
        )

    assert await runner.deps.approval_store.approval_continuation("approval-cancel") is None
    pending = await runner.deps.approval_store.pending(runtime_generation=runner.deps.approval_runtime_generation)
    assert [event.event_id for event in pending] == ["$source"]


@pytest.mark.parametrize(("approved", "reason"), [(True, None), (False, "too dangerous")])
@pytest.mark.asyncio
async def test_agent_continuation_executes_real_agno_confirmation(
    tmp_path: Path,
    approved: bool,
    reason: str | None,
) -> None:
    """Exercise the real persisted Agno pause and continuation spine without mocking it."""
    executed: list[list[str]] = []
    observed_metadata: list[dict[str, object] | None] = []
    original_metadata = {
        "room_id": "!room:localhost",
        "thread_id": "$thread",
        "correlation_id": "approval-metadata",
    }

    def run_shell_command(args: list[str], run_context: RunContext) -> str:
        executed.append(args)
        observed_metadata.append(run_context.metadata)
        return "ok"

    agent = AgnoAgent(
        id="general",
        model=SyntheticModel(
            id="synthetic",
            seed=1,
            min_response_chars=20,
            max_response_chars=20,
            chars_per_second=0,
            tool_call_probability=1,
        ),
        tools=[
            Function(
                name="run_shell_command",
                entrypoint=run_shell_command,
                requires_confirmation=True,
            ),
        ],
        db=SqliteDb(db_file=str(tmp_path / "agent-continuation.db"), session_table="sessions"),
    )
    paused = await agent.arun(
        "exercise the tool",
        session_id="session-1",
        user_id="@user:localhost",
        metadata=original_metadata,
        stream=False,
    )
    requirement = (paused.requirements or [])[0]
    assert requirement.tool_execution is not None
    tool_call_id = requirement.tool_execution.tool_call_id
    assert tool_call_id is not None
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@user:localhost",
        room_id="!room:localhost",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-1",
    )
    continuation = ApprovalContinuation(
        approval_id="approval-real-agent",
        run_id=paused.run_id,
        session_id="session-1",
        entity_kind="agent",
        entity_name="general",
        room_id="!room:localhost",
        thread_id="$thread",
        requester_id="@user:localhost",
        response_event_id="$waiting",
        calls=(),
        execution_identity={},
        source_event_ids=("$source",),
        state="claimed",
    )
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    continue_run = MagicMock(wraps=agent.acontinue_run)
    knowledge = MagicMock()
    refresh_scheduler = MagicMock()
    runner.deps.runtime.orchestrator = SimpleNamespace(knowledge_refresh_scheduler=refresh_scheduler)

    with (
        patch.object(
            runner.deps.knowledge_access,
            "for_agent",
            return_value=knowledge,
        ) as resolve_knowledge,
        patch("mindroom.approval_execution.create_agent", return_value=agent) as create_agent,
        patch.object(agent, "acontinue_run", new=continue_run),
        patch("mindroom.approval_execution.typing_indicator", _noop_typing),
        patch("mindroom.approval_execution.close_agent_runtime_state_dbs"),
        patch("mindroom.approval_execution.ai_runtime.install_queued_message_notice_hook") as install_notice,
        patch("mindroom.approval_execution.ai_runtime.register_queued_notice_storage") as register_notice,
    ):
        result = await runner._approval_execution.continue_run(
            continuation,
            execution_identity=identity,
            tool_dispatch=ToolDispatchContext(execution_identity=identity),
            decisions={tool_call_id: approved},
            denial_reasons={tool_call_id: reason},
            tool_trace_collector=[],
        )

    assert isinstance(result, CompletedApprovalRun)
    assert "io.mindroom.ai_run" in result.metadata_content
    assert bool(executed) is approved
    assert observed_metadata == ([original_metadata] if approved else [])
    resolve_knowledge.assert_called_once_with("general", execution_identity=identity)
    assert create_agent.call_args.kwargs["knowledge"] is knowledge
    assert create_agent.call_args.kwargs["refresh_scheduler"] is refresh_scheduler
    continued_requirement = continue_run.call_args.kwargs["requirements"][0]
    assert continue_run.call_args.kwargs["metadata"] == original_metadata
    assert continue_run.call_args.kwargs["metadata"] is not paused.metadata
    assert continued_requirement.tool_execution.confirmed is approved
    assert continued_requirement.tool_execution.confirmation_note == (None if approved else reason)
    install_notice.assert_called_once_with(
        agent.model,
        notice_text=runner.deps.runtime.config.get_prompt("QUEUED_MESSAGE_NOTICE_TEXT"),
    )
    register_notice.assert_called_once()
    assert register_notice.call_args.kwargs["session_id"] == "session-1"
    assert register_notice.call_args.kwargs["session_type"] is SessionType.AGENT
    assert register_notice.call_args.kwargs["entity_name"] == "general"
    assert callable(register_notice.call_args.kwargs["storage_factory"])


@pytest.mark.parametrize(
    ("persisted_call_ids", "decision_call_ids"),
    [
        pytest.param((None,), ("call-1",), id="missing"),
        pytest.param(("call-1", "call-1"), ("call-1",), id="duplicate"),
        pytest.param(("call-1",), ("call-1", "call-extra"), id="extra"),
        pytest.param(("call-new",), ("call-stale",), id="stale"),
    ],
)
@pytest.mark.asyncio
async def test_agent_continuation_rejects_non_exact_persisted_call_ids(
    tmp_path: Path,
    persisted_call_ids: tuple[str | None, ...],
    decision_call_ids: tuple[str, ...],
) -> None:
    """A malformed persisted pause must never reach Agno continuation execution."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    requirements = [
        RunRequirement(
            ToolExecution(
                tool_call_id=call_id,
                tool_name="dangerous",
                requires_confirmation=True,
            ),
        )
        for call_id in persisted_call_ids
    ]
    persisted = RunOutput(
        run_id="run-1",
        session_id="session-1",
        status=RunStatus.paused,
        requirements=requirements,
    )
    agent = MagicMock()
    agent.aget_session = AsyncMock(return_value=SimpleNamespace(get_run=lambda _run_id: persisted))
    agent.acontinue_run = AsyncMock(
        return_value=RunOutput(
            run_id="run-1",
            session_id="session-1",
            status=RunStatus.completed,
        ),
    )
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@user:localhost",
        room_id="!room:localhost",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-1",
    )
    continuation = ApprovalContinuation(
        approval_id="approval-invalid-agent",
        run_id="run-1",
        session_id="session-1",
        entity_kind="agent",
        entity_name="general",
        room_id="!room:localhost",
        thread_id="$thread",
        requester_id="@user:localhost",
        response_event_id="$waiting",
        calls=(),
        execution_identity={},
        source_event_ids=("$source",),
        state="claimed",
    )
    decisions = dict.fromkeys(decision_call_ids, True)
    denial_reasons = dict.fromkeys(decision_call_ids)

    with (
        patch.object(runner.deps.knowledge_access, "for_agent", return_value=MagicMock()),
        patch("mindroom.approval_execution.create_agent", return_value=agent),
        patch("mindroom.approval_execution.close_agent_runtime_state_dbs"),
        pytest.raises(RuntimeError, match="no longer match the approval continuation"),
    ):
        await runner._approval_execution.continue_run(
            continuation,
            execution_identity=identity,
            tool_dispatch=ToolDispatchContext(execution_identity=identity),
            decisions=decisions,
            denial_reasons=denial_reasons,
            tool_trace_collector=[],
        )

    agent.acontinue_run.assert_not_awaited()


@pytest.mark.asyncio
async def test_agent_continuation_closes_runtime_when_notice_hook_setup_fails(tmp_path: Path) -> None:
    """Reconstructed storage must close even if pre-continuation model setup raises."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    storage = MagicMock()
    agent = MagicMock()
    agent.model = MagicMock()
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@user:localhost",
        room_id="!room:localhost",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-1",
    )
    continuation = ApprovalContinuation(
        approval_id="approval-hook-error",
        run_id="run-1",
        session_id="session-1",
        entity_kind="agent",
        entity_name="general",
        room_id="!room:localhost",
        thread_id="$thread",
        requester_id="@user:localhost",
        response_event_id="$waiting",
        calls=(),
        execution_identity={},
        source_event_ids=("$source",),
        state="claimed",
    )

    with (
        patch.object(runner.deps.knowledge_access, "for_agent", return_value=MagicMock()),
        patch("mindroom.approval_execution.create_session_storage", return_value=storage),
        patch("mindroom.approval_execution.create_agent", return_value=agent),
        patch(
            "mindroom.approval_execution.ai_runtime.install_queued_message_notice_hook",
            side_effect=RuntimeError("hook setup failed"),
        ),
        patch("mindroom.approval_execution.close_agent_runtime_state_dbs") as close_runtime,
        pytest.raises(RuntimeError, match="hook setup failed"),
    ):
        await runner._approval_execution.continue_run(
            continuation,
            execution_identity=identity,
            tool_dispatch=ToolDispatchContext(execution_identity=identity),
            decisions={},
            denial_reasons={},
            tool_trace_collector=[],
        )

    close_runtime.assert_called_once_with(agent, shared_scope_storage=storage)
    storage.close.assert_called_once_with()


@pytest.mark.asyncio
async def test_approval_collaborators_read_live_config_after_hot_reload(tmp_path: Path) -> None:
    """Unchanged bots must apply reloaded approval policy and agent configuration."""
    bot = _bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    reloaded = bot.config.model_copy(deep=True)
    bot.config = reloaded
    tool = ToolExecution(tool_call_id="call-1", tool_name="dangerous", tool_args={})

    def resolve_approver(config: object, *_args: object) -> None:
        assert config is reloaded

    async def evaluate_policy(config: object, *_args: object) -> tuple[bool, float]:
        assert config is reloaded
        return False, 60.0

    with (
        patch("mindroom.approval_response.resolve_tool_approval_approver", side_effect=resolve_approver),
        patch("mindroom.approval_response.evaluate_tool_approval", side_effect=evaluate_policy),
    ):
        await runner._approval_responses.plan_pause(
            ((tool, "call-1", "dangerous", "general"),),
            requester_id="@user:localhost",
        )

    continuation = ApprovalContinuation(
        approval_id="approval-live-config",
        run_id="run-1",
        session_id="session-1",
        entity_kind="agent",
        entity_name="general",
        room_id="!room:localhost",
        thread_id="$thread",
        requester_id="@user:localhost",
        response_event_id="$waiting",
        calls=(),
        execution_identity={},
        source_event_ids=("$source",),
        state="claimed",
    )
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@user:localhost",
        room_id="!room:localhost",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-1",
    )

    def create_from_live_config(_name: str, config: object, *_args: object, **_kwargs: object) -> None:
        assert config is reloaded
        msg = "live config observed"
        raise RuntimeError(msg)

    with (
        patch("mindroom.approval_execution.create_agent", side_effect=create_from_live_config),
        pytest.raises(RuntimeError, match="live config observed"),
    ):
        await runner._approval_execution.continue_run(
            continuation,
            execution_identity=identity,
            tool_dispatch=ToolDispatchContext(execution_identity=identity),
            decisions={},
            denial_reasons={},
            tool_trace_collector=[],
        )


@pytest.mark.asyncio
async def test_missing_approver_records_explicit_fail_closed_reason(tmp_path: Path) -> None:
    """A policy denial must explain that no human approval recipient was available."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    tool = ToolExecution(tool_call_id="call-1", tool_name="dangerous", tool_args={})

    with (
        patch("mindroom.approval_response.resolve_tool_approval_approver", return_value=None),
        patch("mindroom.approval_response.evaluate_tool_approval", new=AsyncMock(return_value=(True, 60.0))),
    ):
        plan = await runner._approval_responses.plan_pause(
            ((tool, "call-1", "dangerous", "general"),),
            requester_id="@user:localhost",
        )

    assert plan.calls[0].decision is response_runner.ContinuationDecision.DENIED
    assert plan.calls[0].reason == "No approval recipient is configured; the tool was denied safely."


@pytest.mark.asyncio
async def test_mixed_pause_plan_publishes_only_human_gated_calls(tmp_path: Path) -> None:
    """An automatically decided sibling must stay out of both waiting text and approval cards."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    automatic = ToolExecution(tool_call_id="call-auto", tool_name="conditional_read", tool_args={})
    gated = ToolExecution(tool_call_id="call-gated", tool_name="conditional_write", tool_args={})

    async def evaluate(_config: object, _paths: object, tool_name: str, *_args: object) -> tuple[bool, float]:
        return tool_name == "conditional_write", 60.0

    with (
        patch(
            "mindroom.approval_response.resolve_tool_approval_approver",
            return_value="@user:localhost",
        ),
        patch("mindroom.approval_response.evaluate_tool_approval", side_effect=evaluate),
    ):
        plan = await runner._approval_responses.plan_pause(
            (
                (automatic, "call-auto", "conditional_read", "general"),
                (gated, "call-gated", "conditional_write", "general"),
            ),
            requester_id="@user:localhost",
        )

    continuation = ApprovalContinuation(
        approval_id="approval-mixed",
        run_id="run-1",
        session_id="session-1",
        entity_kind="agent",
        entity_name="general",
        room_id="!room:localhost",
        thread_id="$thread",
        requester_id="@user:localhost",
        response_event_id="$thinking",
        source_event_ids=("$source",),
        calls=plan.calls,
        state="waiting",
    )
    send_card = AsyncMock(return_value=object())
    with patch("mindroom.approval_response.send_suspended_tool_approval", new=send_card):
        await runner._approval_responses._publish_cards(
            continuation,
            plan,
            target=_target(thread_id="$thread"),
            failure_reason="card failed",
        )

    assert plan.waiting_text == "Waiting for approval: `conditional_write`"
    assert [call.decision for call in plan.calls] == [ApprovalDecision.APPROVED, None]
    send_card.assert_awaited_once()
    assert send_card.await_args.args[0].tool_name == "conditional_write"


@pytest.mark.asyncio
async def test_all_human_gated_pause_plan_keeps_waiting_text_and_cards(tmp_path: Path) -> None:
    """A fully gated batch must retain its current visible approval behavior."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    first = ToolExecution(tool_call_id="call-1", tool_name="dangerous_one", tool_args={})
    second = ToolExecution(tool_call_id="call-2", tool_name="dangerous_two", tool_args={})

    with (
        patch(
            "mindroom.approval_response.resolve_tool_approval_approver",
            return_value="@user:localhost",
        ),
        patch(
            "mindroom.approval_response.evaluate_tool_approval",
            new=AsyncMock(return_value=(True, 60.0)),
        ),
    ):
        plan = await runner._approval_responses.plan_pause(
            (
                (first, "call-1", "dangerous_one", "general"),
                (second, "call-2", "dangerous_two", "general"),
            ),
            requester_id="@user:localhost",
        )

    continuation = ApprovalContinuation(
        approval_id="approval-gated",
        run_id="run-1",
        session_id="session-1",
        entity_kind="agent",
        entity_name="general",
        room_id="!room:localhost",
        thread_id="$thread",
        requester_id="@user:localhost",
        response_event_id="$thinking",
        source_event_ids=("$source",),
        calls=plan.calls,
        state="waiting",
    )
    send_card = AsyncMock(return_value=object())
    with patch("mindroom.approval_response.send_suspended_tool_approval", new=send_card):
        await runner._approval_responses._publish_cards(
            continuation,
            plan,
            target=_target(thread_id="$thread"),
            failure_reason="card failed",
        )

    assert plan.waiting_text == "Waiting for approval: `dangerous_one`, `dangerous_two`"
    assert [call.decision for call in plan.calls] == [None, None]
    assert [awaited.args[0].tool_name for awaited in send_card.await_args_list] == ["dangerous_one", "dangerous_two"]


@pytest.mark.asyncio
async def test_automatic_pause_preserves_thinking_placeholder_and_wakes_continuation(tmp_path: Path) -> None:
    """A fully automatic pause must stay neutral while its durable continuation is scheduled."""
    script_path = tmp_path / "conditional_approval.py"
    script_path.write_text(
        "def check(tool_name, arguments, agent_name):\n    return arguments['requires_approval']\n",
        encoding="utf-8",
    )
    bot = _bot(tmp_path)
    bot.config.tool_approval = bot.config.tool_approval.model_copy(
        update={"rules": [ApprovalRuleConfig(match="conditional_tool", script=str(script_path))]},
    )
    runner = unwrap_extracted_collaborator(bot._response_runner)
    await _admit_approval_source(runner.deps.approval_store)
    request = _plain_request(_target(thread_id="$thread"), source_event_id="$source")
    paused = PausedAttempt(
        session_id="session-1",
        run_id="run-1",
        tools=(
            ToolExecution(
                tool_call_id="call-auto-1",
                tool_name="conditional_tool",
                tool_args={"requires_approval": False},
            ),
            ToolExecution(
                tool_call_id="call-auto-2",
                tool_name="conditional_tool",
                tool_args={"requires_approval": False},
            ),
        ),
    )
    identity = runner.deps.tool_runtime.build_execution_identity(
        target=request.response_envelope.target,
        user_id=request.user_id,
    )
    edit_text = AsyncMock(return_value=True)
    send_text = AsyncMock(return_value="$unexpected")
    send_card = AsyncMock(return_value=object())
    retry_sources = Mock()
    runner._approval_responses.retry_sources = retry_sources

    with (
        patch.object(DeliveryGateway, "edit_text", new=edit_text),
        patch.object(DeliveryGateway, "send_text", new=send_text),
        patch(
            "mindroom.approval_response.resolve_tool_approval_approver",
            return_value="@user:localhost",
        ),
        patch("mindroom.approval_response.send_suspended_tool_approval", new=send_card),
    ):
        outcome = await runner._suspend_for_approval(
            paused,
            request=request,
            target=request.response_envelope.target,
            progress=response_runner._DeliveryProgress(tracked_event_id="$thinking"),
            execution_identity=identity,
            entity_kind="agent",
            history_scope=runner.deps.state_writer.history_scope(),
        )

    edit_text.assert_not_awaited()
    send_text.assert_not_awaited()
    send_card.assert_not_awaited()
    retry_sources.assert_called_once_with(("$source",))
    assert outcome.final_visible_body is None
    assert outcome.delivery_kind is None
    assert outcome.extra_content == {STREAM_STATUS_KEY: STREAM_STATUS_PENDING}


@pytest.mark.asyncio
async def test_automatic_pause_without_visible_event_sends_neutral_placeholder(tmp_path: Path) -> None:
    """A direct automatic pause still needs neutral visible identity for durable final delivery."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    await _admit_approval_source(runner.deps.approval_store)
    request = _plain_request(_target(thread_id="$thread"), source_event_id="$source")
    paused = PausedAttempt(
        session_id="session-1",
        run_id="run-1",
        tools=(ToolExecution(tool_call_id="call-auto", tool_name="conditional_read", tool_args={}),),
    )
    identity = runner.deps.tool_runtime.build_execution_identity(
        target=request.response_envelope.target,
        user_id=request.user_id,
    )
    send_text = AsyncMock(return_value="$thinking")
    retry_sources = Mock()
    runner._approval_responses.retry_sources = retry_sources

    with (
        patch.object(DeliveryGateway, "send_text", new=send_text),
        patch(
            "mindroom.approval_response.resolve_tool_approval_approver",
            return_value="@user:localhost",
        ),
        patch(
            "mindroom.approval_response.evaluate_tool_approval",
            new=AsyncMock(return_value=(False, 60.0)),
        ),
    ):
        outcome = await runner._suspend_for_approval(
            paused,
            request=request,
            target=request.response_envelope.target,
            progress=response_runner._DeliveryProgress(),
            execution_identity=identity,
            entity_kind="agent",
            history_scope=runner.deps.state_writer.history_scope(),
        )

    send_request = send_text.await_args.args[0]
    assert send_request.response_text == "Thinking..."
    assert send_request.extra_content == {STREAM_STATUS_KEY: STREAM_STATUS_PENDING}
    retry_sources.assert_called_once_with(("$source",))
    assert outcome.final_visible_body == "Thinking..."
    assert outcome.delivery_kind == "sent"
    assert outcome.extra_content == {STREAM_STATUS_KEY: STREAM_STATUS_PENDING}


@pytest.mark.asyncio
async def test_missing_approver_denial_stays_neutral_and_wakes_continuation(tmp_path: Path) -> None:
    """Fail-closed automatic denial must not claim that a nonexistent recipient can approve it."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    await _admit_approval_source(runner.deps.approval_store)
    request = _plain_request(_target(thread_id="$thread"), source_event_id="$source")
    paused = PausedAttempt(
        session_id="session-1",
        run_id="run-1",
        tools=(ToolExecution(tool_call_id="call-denied", tool_name="dangerous", tool_args={}),),
    )
    identity = runner.deps.tool_runtime.build_execution_identity(
        target=request.response_envelope.target,
        user_id=request.user_id,
    )
    edit_text = AsyncMock(return_value=True)
    send_card = AsyncMock(return_value=object())
    retry_sources = Mock()
    runner._approval_responses.retry_sources = retry_sources

    with (
        patch.object(DeliveryGateway, "edit_text", new=edit_text),
        patch("mindroom.approval_response.resolve_tool_approval_approver", return_value=None),
        patch(
            "mindroom.approval_response.evaluate_tool_approval",
            new=AsyncMock(return_value=(True, 60.0)),
        ),
        patch("mindroom.approval_response.send_suspended_tool_approval", new=send_card),
    ):
        outcome = await runner._suspend_for_approval(
            paused,
            request=request,
            target=request.response_envelope.target,
            progress=response_runner._DeliveryProgress(tracked_event_id="$thinking"),
            execution_identity=identity,
            entity_kind="agent",
            history_scope=runner.deps.state_writer.history_scope(),
        )

    continuation = await runner.deps.approval_store.approval_continuation_for_source("$source")
    assert continuation is not None
    assert continuation.state == "ready"
    assert continuation.calls[0].decision is ApprovalDecision.DENIED
    edit_text.assert_not_awaited()
    send_card.assert_not_awaited()
    retry_sources.assert_called_once_with(("$source",))
    assert outcome.extra_content == {STREAM_STATUS_KEY: STREAM_STATUS_PENDING}


@pytest.mark.parametrize(
    ("gated_tools", "expected_text", "expected_state", "expected_cards"),
    [
        (set(), None, "ready", []),
        ({"conditional_write"}, "Waiting for approval: `conditional_write`", "waiting", ["conditional_write"]),
        (
            {"conditional_read", "conditional_write"},
            "Waiting for approval: `conditional_read`, `conditional_write`",
            "waiting",
            ["conditional_read", "conditional_write"],
        ),
    ],
    ids=["automatic", "mixed", "human"],
)
@pytest.mark.asyncio
async def test_chained_pause_persists_and_publishes_only_human_gated_calls(
    tmp_path: Path,
    gated_tools: set[str],
    expected_text: str | None,
    expected_state: str,
    expected_cards: list[str],
) -> None:
    """Every chained generation must durably expose only its unresolved calls."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    store = runner.deps.approval_store
    await _admit_approval_source(store)
    continuation = ApprovalContinuation(
        approval_id="approval-chain",
        run_id="run-1",
        session_id="session-1",
        entity_kind="agent",
        entity_name="general",
        room_id="!room:localhost",
        thread_id="$thread",
        requester_id="@user:localhost",
        response_event_id="$waiting",
        source_event_ids=("$source",),
        calls=(),
        state="ready",
    )
    assert await store.create_approval_continuation(continuation) == continuation
    current = await store.claim_approval_continuation(
        continuation.approval_id,
        runtime_generation=runner.deps.approval_runtime_generation,
    )
    assert current is not None
    paused = PausedAttempt(
        session_id="session-1",
        run_id="run-2",
        tools=(
            ToolExecution(tool_call_id="call-read", tool_name="conditional_read", tool_args={}),
            ToolExecution(tool_call_id="call-write", tool_name="conditional_write", tool_args={}),
        ),
    )
    edit_text = AsyncMock(return_value=True)
    send_card = AsyncMock(return_value=object())
    retry_sources = Mock()
    runner._approval_responses.retry_sources = retry_sources

    async def evaluate(_config: object, _paths: object, tool_name: str, *_args: object) -> tuple[bool, float]:
        return tool_name in gated_tools, 60.0

    with (
        patch.object(DeliveryGateway, "edit_text", new=edit_text),
        patch(
            "mindroom.approval_response.resolve_tool_approval_approver",
            return_value="@user:localhost",
        ),
        patch("mindroom.approval_response.evaluate_tool_approval", side_effect=evaluate),
        patch("mindroom.approval_response.send_suspended_tool_approval", new=send_card),
    ):
        waiting_text = await runner._approval_responses.advance_pause(
            current,
            paused,
            target=_target(thread_id="$thread"),
            tool_trace=[],
            pending_text="Thinking...",
        )

    persisted = await store.approval_continuation(continuation.approval_id)
    assert persisted is not None
    assert persisted.generation == 1
    assert persisted.state == expected_state
    assert waiting_text == expected_text
    edit_request = edit_text.await_args.args[0]
    assert edit_request.new_text == (expected_text or "Thinking...")
    assert edit_request.extra_content == {
        STREAM_STATUS_KEY: STREAM_STATUS_APPROVAL_PENDING if expected_text else STREAM_STATUS_PENDING,
    }
    assert [awaited.args[0].tool_name for awaited in send_card.await_args_list] == expected_cards
    if expected_state == "ready":
        retry_sources.assert_called_once_with(("$source",))
        restarted = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
        claimed = await restarted.deps.approval_store.claim_approval_continuation(
            continuation.approval_id,
            runtime_generation=restarted.deps.approval_runtime_generation,
        )
        assert claimed is not None
        assert (
            await store.claim_approval_continuation(
                continuation.approval_id,
                runtime_generation=runner.deps.approval_runtime_generation,
            )
            is None
        )
    else:
        retry_sources.assert_not_called()


@pytest.mark.asyncio
async def test_native_agno_confirmation_cannot_be_auto_approved_by_mindroom_default(tmp_path: Path) -> None:
    """An authored Agno confirmation still requires the requester when MindRoom has no gating rule."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    assert runner.deps.runtime.config.tool_approval.default == "auto_approve"
    tool = ToolExecution(
        tool_call_id="call-native",
        tool_name="native_confirmation",
        tool_args={},
        requires_confirmation=True,
    )

    with patch(
        "mindroom.approval_response.resolve_tool_approval_approver",
        return_value="@user:localhost",
    ):
        plan = await runner._approval_responses.plan_pause(
            ((tool, "call-native", "native_confirmation", "general"),),
            requester_id="@user:localhost",
        )

    assert plan.calls[0].decision is None


@pytest.mark.asyncio
async def test_policy_confirmation_honors_exact_argument_exemption(tmp_path: Path) -> None:
    """The synthetic Agno pause marker must not turn an exempt policy call into human approval."""
    register_tool_approval_exemption("synthetic_exempt", lambda arguments: arguments.get("dry_run") is True)
    bot = _bot(tmp_path)
    bot.config.tool_approval = bot.config.tool_approval.model_copy(update={"default": "require_approval"})
    runner = unwrap_extracted_collaborator(bot._response_runner)
    toolkit = Toolkit(
        name="approval-test",
        tools=[Function(name="synthetic_exempt", entrypoint=lambda: None)],
    )
    agents_module.apply_tool_approval_capability(
        toolkit,
        bot.config,
        supports_native_tool_approval=True,
    )
    function = toolkit.functions["synthetic_exempt"]
    tool = ToolExecution(
        tool_call_id="call-exempt",
        tool_name=function.name,
        tool_args={"dry_run": True},
        requires_confirmation=function.requires_confirmation,
        approval_type=function.approval_type,
    )

    with patch(
        "mindroom.approval_response.resolve_tool_approval_approver",
        return_value="@user:localhost",
    ):
        plan = await runner._approval_responses.plan_pause(
            ((tool, "call-exempt", function.name, "general"),),
            requester_id="@user:localhost",
        )

    assert plan.calls[0].decision is ApprovalDecision.APPROVED
    assert plan.waiting_text is None


@pytest.mark.asyncio
async def test_policy_confirmation_honors_script_auto_approval(tmp_path: Path) -> None:
    """An exact-call script decision remains authoritative after Agno pauses a potentially gated tool."""
    script_path = tmp_path / "approval.py"
    script_path.write_text(
        "def check(tool_name, arguments, agent_name):\n    return arguments['requires_approval']\n",
        encoding="utf-8",
    )
    bot = _bot(tmp_path)
    bot.config.tool_approval = bot.config.tool_approval.model_copy(
        update={
            "rules": [
                ApprovalRuleConfig(
                    match="scripted_tool",
                    script=str(script_path),
                ),
            ],
        },
    )
    runner = unwrap_extracted_collaborator(bot._response_runner)
    toolkit = Toolkit(
        name="approval-test",
        tools=[Function(name="scripted_tool", entrypoint=lambda: None)],
    )
    agents_module.apply_tool_approval_capability(
        toolkit,
        bot.config,
        supports_native_tool_approval=True,
    )
    function = toolkit.functions["scripted_tool"]
    tool = ToolExecution(
        tool_call_id="call-script",
        tool_name=function.name,
        tool_args={"requires_approval": False},
        requires_confirmation=function.requires_confirmation,
        approval_type=function.approval_type,
    )

    with patch(
        "mindroom.approval_response.resolve_tool_approval_approver",
        return_value="@user:localhost",
    ):
        plan = await runner._approval_responses.plan_pause(
            ((tool, "call-script", function.name, "general"),),
            requester_id="@user:localhost",
        )

    assert plan.calls[0].decision is ApprovalDecision.APPROVED
    assert plan.waiting_text is None


@pytest.mark.asyncio
async def test_recovered_claim_honors_acknowledged_final_outbox_delivery(tmp_path: Path) -> None:
    """An attempted FINAL is recovered and completed without invoking Agno twice."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    store = runner.deps.approval_store
    await _admit_approval_source(store)
    continuation = ApprovalContinuation(
        approval_id="approval-final-acked",
        run_id="run-1",
        session_id="session-1",
        entity_kind="agent",
        entity_name="general",
        room_id="!room:localhost",
        thread_id="$thread",
        requester_id="@user:localhost",
        response_event_id="$waiting",
        calls=(
            ApprovalCall(
                tool_call_id="call-1",
                tool_name="dangerous",
                invoking_agent="general",
                expires_at_ns=9_000_000_000_000_000_000,
                decision=ApprovalDecision.APPROVED,
            ),
        ),
        execution_identity={},
        source_event_ids=("$source",),
        state="ready",
    )
    assert await store.create_approval_continuation(continuation) == continuation
    claimed = await store.claim_approval_continuation(
        continuation.approval_id,
        runtime_generation=runner.deps.approval_runtime_generation,
    )
    assert claimed is not None
    await store.enqueue_delivery(
        turn_id="$source",
        stage=DeliveryStage.FINAL,
        room_id="!room:localhost",
        thread_id="$thread",
        payload={"body": "finished", "formatted_body": "finished"},
        edits_event_id="$waiting",
    )
    assert await store.claim_delivery(turn_id="$source", stage=DeliveryStage.FINAL) is not None

    async def acknowledge_frozen_final() -> None:
        await store.acknowledge_delivery(
            turn_id="$source",
            stage=DeliveryStage.FINAL,
            event_id="$final",
            delivered_projections=(),
        )

    with (
        patch.object(DeliveryGateway, "recover_deliveries", new=AsyncMock(side_effect=acknowledge_frozen_final)),
        patch.object(runner, "_continue_entity_call", new=AsyncMock()) as continue_entity,
        patch.object(runner, "_build_lifecycle", return_value=MagicMock(finalize=AsyncMock())) as build_lifecycle,
    ):
        event_id = await runner._recover_claimed_approval_lifecycle(
            claimed,
            target=_target(thread_id="$thread", reply_to_event_id="$source"),
        )

    assert event_id == "$waiting"
    continue_entity.assert_not_awaited()
    build_lifecycle.return_value.finalize.assert_awaited_once()
    assert await store.approval_continuation(continuation.approval_id) is None
    assert not await store.is_pending("$source")


@pytest.mark.asyncio
async def test_recovered_claim_restores_plain_body_and_interactive_metadata(tmp_path: Path) -> None:
    """Restart recovery must replay semantic final facts, not rendered Matrix HTML."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    store = runner.deps.approval_store
    await _admit_approval_source(store)
    continuation = ApprovalContinuation(
        approval_id="approval-final-semantic",
        run_id="run-1",
        session_id="session-1",
        entity_kind="agent",
        entity_name="general",
        room_id="!room:localhost",
        thread_id="$thread",
        requester_id="@user:localhost",
        response_event_id="$waiting",
        calls=(),
        execution_identity={},
        source_event_ids=("$source",),
        state="ready",
    )
    assert await store.create_approval_continuation(continuation) == continuation
    claimed = await store.claim_approval_continuation(
        continuation.approval_id,
        runtime_generation=runner.deps.approval_runtime_generation,
    )
    assert claimed is not None
    await store.enqueue_delivery(
        turn_id="$source",
        stage=DeliveryStage.FINAL,
        room_id="!room:localhost",
        thread_id="$thread",
        payload={
            "body": "plain fallback",
            "formatted_body": "<strong>rendered html</strong>",
            "io.mindroom.final_delivery": {
                "body": "plain final",
                "interactive": {
                    "question_text": "Pick",
                    "option_map": {"1": "yes", "✅": "yes"},
                    "option_labels": {"1": "Yes", "✅": "Yes"},
                    "options_list": [{"emoji": "✅", "label": "Yes", "value": "yes"}],
                },
            },
        },
        edits_event_id="$waiting",
    )
    assert await store.claim_delivery(turn_id="$source", stage=DeliveryStage.FINAL) is not None
    await store.acknowledge_delivery(
        turn_id="$source",
        stage=DeliveryStage.FINAL,
        event_id="$final",
        delivered_projections=(),
    )
    lifecycle = MagicMock(finalize=AsyncMock(side_effect=lambda outcome, **_kwargs: outcome))

    with patch.object(runner, "_build_lifecycle", return_value=lifecycle):
        event_id = await runner._recover_claimed_approval_lifecycle(
            claimed,
            target=_target(thread_id="$thread", reply_to_event_id="$source"),
        )

    assert event_id == "$waiting"
    final = lifecycle.finalize.await_args.args[0]
    assert final.final_visible_body == "plain final"
    assert final.interactive_metadata is not None
    assert final.interactive_metadata.question_text == "Pick"
    assert final.option_map == {"1": "yes", "✅": "yes"}


@pytest.mark.asyncio
async def test_original_owner_recovery_retires_acknowledged_failure_without_success_effects(tmp_path: Path) -> None:
    """A frozen failure FINAL must not be replayed through the successful response lifecycle."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    store = runner.deps.approval_store
    await _admit_approval_source(store)
    continuation = ApprovalContinuation(
        approval_id="approval-failure-final",
        run_id="run-1",
        session_id="session-1",
        entity_kind="agent",
        entity_name="general",
        room_id="!room:localhost",
        thread_id="$thread",
        requester_id="@user:localhost",
        response_event_id="$waiting",
        calls=(),
        execution_identity={},
        source_event_ids=("$source",),
        state="failing",
        failure_reason="Tool approval continuation failed safely.",
    )
    assert await store.create_approval_continuation(continuation) == continuation
    await store.enqueue_delivery(
        turn_id="$source",
        stage=DeliveryStage.FINAL,
        room_id="!room:localhost",
        thread_id="$thread",
        payload={"body": "Tool approval continuation failed safely."},
        edits_event_id="$waiting",
    )
    assert await store.claim_delivery(turn_id="$source", stage=DeliveryStage.FINAL) is not None
    await store.acknowledge_delivery(
        turn_id="$source",
        stage=DeliveryStage.FINAL,
        event_id="$failure-edit",
        delivered_projections=(),
    )

    with patch.object(runner, "_build_lifecycle", return_value=MagicMock(finalize=AsyncMock())) as build_lifecycle:
        assert await runner.recover_approval_final(continuation.approval_id)

    build_lifecycle.assert_not_called()
    assert await store.approval_continuation(continuation.approval_id) is None
    assert not await store.is_pending("$source")


@pytest.mark.asyncio
async def test_acknowledged_final_wins_cancellation_before_delivery_returns(tmp_path: Path) -> None:
    """A visible successful FINAL must complete even if the live caller is cancelled afterward."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    store = runner.deps.approval_store
    await _admit_approval_source(store)
    continuation = ApprovalContinuation(
        approval_id="approval-final-cancelled-return",
        run_id="run-1",
        session_id="session-1",
        entity_kind="agent",
        entity_name="general",
        room_id="!room:localhost",
        thread_id="$thread",
        requester_id="@user:localhost",
        response_event_id="$waiting",
        calls=(),
        execution_identity={},
        source_event_ids=("$source",),
        state="ready",
    )
    assert await store.create_approval_continuation(continuation) == continuation
    claimed = await store.claim_approval_continuation(
        continuation.approval_id,
        runtime_generation=runner.deps.approval_runtime_generation,
    )
    assert claimed is not None

    async def acknowledge_then_cancel(*_args: object, **_kwargs: object) -> tuple[object, object]:
        await store.enqueue_delivery(
            turn_id="$source",
            stage=DeliveryStage.FINAL,
            room_id="!room:localhost",
            thread_id="$thread",
            payload={
                "body": "plain final",
                "io.mindroom.final_delivery": {"body": "plain final", "interactive": None},
            },
            edits_event_id="$waiting",
        )
        assert await store.claim_delivery(turn_id="$source", stage=DeliveryStage.FINAL) is not None
        await store.acknowledge_delivery(
            turn_id="$source",
            stage=DeliveryStage.FINAL,
            event_id="$final",
            delivered_projections=(),
        )
        raise asyncio.CancelledError

    lifecycle = MagicMock(finalize=AsyncMock(side_effect=lambda outcome, **_kwargs: outcome))
    with (
        patch.object(runner, "_execute_claimed_approval", side_effect=acknowledge_then_cancel),
        patch.object(runner, "_build_lifecycle", return_value=lifecycle),
        patch.object(runner, "_approval_post_response_outcome", return_value=ResponseOutcome()),
    ):
        outcome = await runner._run_claimed_approval_lifecycle(
            claimed,
            target=_target(thread_id="$thread", reply_to_event_id="$source"),
        )

    assert outcome.terminal_status == "completed"
    assert outcome.event_id == "$waiting"
    assert await store.approval_continuation(continuation.approval_id) is None
    finalized = [call.args[0] for call in lifecycle.finalize.await_args_list]
    assert [item.terminal_status for item in finalized] == ["completed"]


@pytest.mark.asyncio
async def test_acknowledged_final_wins_cancellation_after_lifecycle_delivery(tmp_path: Path) -> None:
    """Late lifecycle cancellation must adopt the visible success before failure fencing."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    store = runner.deps.approval_store
    request = _plain_request(_target(thread_id="$thread"), source_event_id="$source")
    await _admit_approval_source(store)
    continuation = ApprovalContinuation(
        approval_id="approval-final-late-cancel",
        run_id="run-1",
        session_id="session-1",
        entity_kind="agent",
        entity_name="general",
        room_id="!room:localhost",
        thread_id="$thread",
        requester_id="@user:localhost",
        response_event_id="$waiting",
        calls=(),
        execution_identity={},
        source_event_ids=("$source",),
        state="ready",
    )
    assert await store.create_approval_continuation(continuation) == continuation

    async def acknowledge_then_cancel(claimed: ApprovalContinuation, **_kwargs: object) -> None:
        await store.enqueue_delivery(
            turn_id="$source",
            stage=DeliveryStage.FINAL,
            room_id="!room:localhost",
            thread_id="$thread",
            payload={
                "body": "plain final",
                "io.mindroom.final_delivery": {"body": "plain final", "interactive": None},
            },
            edits_event_id="$waiting",
        )
        assert claimed.state == "claimed"
        assert await store.claim_delivery(turn_id="$source", stage=DeliveryStage.FINAL) is not None
        await store.acknowledge_delivery(
            turn_id="$source",
            stage=DeliveryStage.FINAL,
            event_id="$final",
            delivered_projections=(),
        )
        raise asyncio.CancelledError

    lifecycle = MagicMock(finalize=AsyncMock(side_effect=lambda outcome, **_kwargs: outcome))
    with (
        patch.object(runner, "_run_claimed_approval_lifecycle", side_effect=acknowledge_then_cancel),
        patch.object(runner, "_build_lifecycle", return_value=lifecycle),
        patch.object(runner, "_approval_post_response_outcome", return_value=ResponseOutcome()),
    ):
        event_id = await runner._run_owned_or_locked_response(
            request,
            target=request.response_envelope.target,
            early_placeholder=response_runner._EarlyPlaceholderState(),
            locked_operation=AsyncMock(return_value="$duplicate"),
        )

    assert event_id == "$waiting"
    assert await store.approval_continuation(continuation.approval_id) is None
    assert not await store.is_pending("$source")


@pytest.mark.asyncio
async def test_recovered_claim_keeps_unacknowledged_final_recoverable(tmp_path: Path) -> None:
    """An inconclusive FINAL retry must retain the exact paused-run ownership."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    store = runner.deps.approval_store
    await _admit_approval_source(store)
    continuation = ApprovalContinuation(
        approval_id="approval-final-unacknowledged",
        run_id="run-1",
        session_id="session-1",
        entity_kind="agent",
        entity_name="general",
        room_id="!room:localhost",
        thread_id="$thread",
        requester_id="@user:localhost",
        response_event_id="$waiting",
        calls=(
            ApprovalCall(
                tool_call_id="call-1",
                tool_name="dangerous",
                invoking_agent="general",
                expires_at_ns=9_000_000_000_000_000_000,
                decision=ApprovalDecision.APPROVED,
            ),
        ),
        execution_identity={},
        source_event_ids=("$source",),
        state="ready",
    )
    assert await store.create_approval_continuation(continuation) == continuation
    claimed = await store.claim_approval_continuation(
        continuation.approval_id,
        runtime_generation=runner.deps.approval_runtime_generation,
    )
    assert claimed is not None
    await store.enqueue_delivery(
        turn_id="$source",
        stage=DeliveryStage.FINAL,
        room_id="!room:localhost",
        thread_id="$thread",
        payload={"body": "finished", "formatted_body": "finished"},
        edits_event_id="$waiting",
    )
    assert await store.claim_delivery(turn_id="$source", stage=DeliveryStage.FINAL) is not None

    with patch.object(DeliveryGateway, "recover_deliveries", new=AsyncMock()):
        event_id = await runner._recover_claimed_approval_lifecycle(
            claimed,
            target=_target(thread_id="$thread", reply_to_event_id="$source"),
        )

    retained = await store.approval_continuation(continuation.approval_id)
    assert event_id is None
    assert retained is not None
    assert retained.state == "claimed"
    assert retained.runtime_generation == runner.deps.approval_runtime_generation
    assert await store.is_pending("$source")
    final = await store.load_delivery(turn_id="$source", stage=DeliveryStage.FINAL)
    assert final is not None
    assert final.attempted
    assert final.acknowledged_event_id is None


@pytest.mark.asyncio
async def test_continuation_rejects_missing_persisted_execution_identity(tmp_path: Path) -> None:
    """Malformed durable identity must fail explicitly even when Python assertions are disabled."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    continuation = ApprovalContinuation(
        approval_id="approval-missing-identity",
        run_id="run-1",
        session_id="session-1",
        entity_kind="agent",
        entity_name="general",
        room_id="!room:localhost",
        thread_id="$thread",
        requester_id="@user:localhost",
        response_event_id="$waiting",
        calls=(),
        execution_identity={},
        source_event_ids=("$source",),
        state="claimed",
    )

    with (
        patch("mindroom.response_runner.parse_tool_execution_identity_payload", return_value=None),
        pytest.raises(RuntimeError, match=r"approval-missing-identity.*execution identity"),
    ):
        await runner._continue_entity_call(
            continuation,
            request=_plain_request(_target()),
            target=_target(),
            tool_trace_collector=[],
        )


@pytest.mark.asyncio
async def test_approval_request_restores_exact_hook_envelope_after_store_reload(tmp_path: Path) -> None:
    """Resume hooks must observe the same ingress identity and correlation as suspension."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    store = runner.deps.approval_store
    await _admit_approval_source(store)
    original_envelope = replace(
        _envelope(_target(thread_id="$thread"), source_event_id="$source"),
        mentioned_agents=("research", "general"),
        hook_source="plugin:message_received",
        dispatch_policy_source_kind="plugin",
        message_received_depth=3,
    )
    continuation = ApprovalContinuation(
        approval_id="approval-hook-context",
        run_id="run-1",
        session_id="session-1",
        entity_kind="agent",
        entity_name="general",
        room_id="!room:localhost",
        thread_id="$thread",
        requester_id="@user:localhost",
        response_event_id="$waiting",
        source_event_ids=("$source",),
        calls=(),
        state="ready",
        request_body=original_envelope.body,
        origin=original_envelope.origin,
        mentioned_agents=original_envelope.mentioned_agents,
        hook_source=original_envelope.hook_source,
        dispatch_policy_source_kind=original_envelope.dispatch_policy_source_kind,
        message_received_depth=original_envelope.message_received_depth,
        correlation_id="correlation-original",
    )
    assert await store.create_approval_continuation(continuation) == continuation
    reloaded = await store.approval_continuation(continuation.approval_id)
    assert reloaded is not None

    restored = runner._approval_response_request(
        reloaded,
        target=_target(thread_id="$thread", reply_to_event_id="$source"),
    )

    assert restored.correlation_id == "correlation-original"
    assert restored.response_envelope.mentioned_agents == ("research", "general")
    assert restored.response_envelope.hook_source == "plugin:message_received"
    assert restored.response_envelope.dispatch_policy_source_kind == "plugin"
    assert restored.response_envelope.message_received_depth == 3


@pytest.mark.asyncio
async def test_continuation_tool_dispatch_preserves_original_correlation_id(tmp_path: Path) -> None:
    """Resumed tool hooks and runtime events stay correlated with the originating Matrix turn."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@user:localhost",
        room_id="!room:localhost",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-1",
    )
    continuation = ApprovalContinuation(
        approval_id="approval-correlation",
        run_id="run-1",
        session_id="session-1",
        entity_kind="agent",
        entity_name="general",
        room_id="!room:localhost",
        thread_id="$thread",
        requester_id="@user:localhost",
        response_event_id="$waiting",
        source_event_ids=("$source",),
        calls=(),
        state="claimed",
        execution_identity={},
        correlation_id="correlation-original",
    )
    request = replace(
        _plain_request(_target(thread_id="$thread"), source_event_id="$source"),
        correlation_id="correlation-original",
    )
    observed: list[str | None] = []

    def build_dispatch_context(
        *_args: object,
        correlation_id: str | None = None,
        **_kwargs: object,
    ) -> ToolDispatchContext:
        observed.append(correlation_id)
        return ToolDispatchContext(execution_identity=identity)

    with (
        patch("mindroom.response_runner.parse_tool_execution_identity_payload", return_value=identity),
        patch.object(runner.deps.tool_runtime, "build_dispatch_context", side_effect=build_dispatch_context),
        patch(
            "mindroom.approval_execution.AgentApprovalExecution.continue_run",
            new=AsyncMock(return_value=CompletedApprovalRun(response_text="done", metadata_content={})),
        ),
    ):
        await runner._continue_entity_call(
            continuation,
            request=request,
            target=request.response_envelope.target,
            tool_trace_collector=[],
        )

    assert observed == ["correlation-original"]


@pytest.mark.asyncio
async def test_suspension_rejects_missing_requester_before_persistence(tmp_path: Path) -> None:
    """A continuation without the original session user cannot be resumed against the same Agno run."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    request = replace(_plain_request(_target()), user_id=None)
    paused = PausedAttempt(
        session_id="session-1",
        run_id="run-1",
        tools=(ToolExecution(tool_call_id="call-1", tool_name="dangerous", requires_confirmation=True),),
    )
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id=None,
        room_id="!room:localhost",
        thread_id=None,
        resolved_thread_id=None,
        session_id="session-1",
    )

    with pytest.raises(RuntimeError, match="requester identity"):
        await runner._suspend_for_approval(
            paused,
            request=request,
            target=_target(),
            progress=response_runner._DeliveryProgress(),
            execution_identity=identity,
            entity_kind="agent",
            history_scope=runner.deps.state_writer.history_scope(),
        )


@pytest.mark.asyncio
async def test_scheduled_history_limit_keeps_refreshed_history_for_payload_and_side_effects(tmp_path: Path) -> None:
    """The runner keeps full history until execution preparation builds model context."""
    bot = _bot(tmp_path)
    refreshed = ThreadHistoryResult(
        [
            make_visible_message(sender="@user:localhost", body=f"message {index}", event_id=f"$m{index}")
            for index in range(4)
        ],
        is_full_history=True,
    )
    prepared_histories: list[object] = []

    async def spy_prepare(request: ResponseRequest) -> ResponseRequest:
        prepared_histories.append(request.thread_history)
        return replace(request, payload_preparation=None, requires_model_history_refresh=False)

    resolver = MagicMock(spec=ConversationResolver)
    resolver.fetch_thread_history = AsyncMock(return_value=refreshed)
    request_preparer = MagicMock(spec=ResponsePayloadPreparer)
    request_preparer.prepare = AsyncMock(side_effect=spy_prepare)
    coordinator = ResponseRunner(
        replace(
            unwrap_extracted_collaborator(bot._response_runner).deps,
            resolver=resolver,
            request_preparer=request_preparer,
        ),
    )

    target = _target(thread_id="$thread", reply_to_event_id="$event1")
    envelope = _envelope(target, source_event_id="$event1")
    request = ResponseRequest(
        thread_history=[],
        prompt="poll the queue",
        user_id="@user:localhost",
        response_envelope=envelope,
        payload_preparation=_preparation(target, envelope),
        scheduled_history_budget=ScheduledHistoryBudget(limit=2, source_event_id="$event1"),
    )
    prepared_request = await coordinator._prepare_request_after_lock(request)
    _memory_prompt, memory_history, _model_prompt, _model_history = prepare_memory_and_model_context(
        prepared_request.prompt,
        prepared_request.thread_history,
        config=coordinator.deps.runtime.config,
        runtime_paths=coordinator.deps.runtime_paths,
        model_prompt=prepared_request.model_prompt,
    )

    assert len(prepared_histories) == 1
    assert prepared_histories == [refreshed]
    assert prepared_request.thread_history is refreshed
    assert prepared_request.scheduled_history_budget is request.scheduled_history_budget
    assert memory_history is refreshed
    assert (
        thread_summary_message_count_hint(
            prepared_request.thread_history,
            trusted_sender_ids=current_internal_sender_ids(
                coordinator.deps.runtime.config,
                coordinator.deps.runtime_paths,
            ),
        )
        == 5
    )


# ---------------------------------------------------------------------------
# 2. Attempt mechanics: placeholder, stop tracking on success/failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_adopted_placeholder_is_passed_to_the_response_function(tmp_path: Path) -> None:
    """The event the turn already made visible is what the attempt generates against."""
    stop_manager = RecordingStopManager()
    runner = _attempt_runner(tmp_path, stop_manager)
    seen: list[str | None] = []

    async def respond(message_id: str | None) -> None:
        seen.append(message_id)

    result = await runner.run(
        ResponseAttemptRequest(target=_target(), response_function=respond, existing_event_id="$placeholder"),
    )

    assert result == "$placeholder"
    assert seen == ["$placeholder"]


@pytest.mark.asyncio
async def test_stop_tracking_registered_during_run_and_cleared_on_success(tmp_path: Path) -> None:
    """The attempt is stop-trackable while generating and tracking clears after success."""
    stop_manager = RecordingStopManager()
    runner = _attempt_runner(tmp_path, stop_manager)
    target = _target()
    observed: list[tuple[MessageTarget, str | None, bool]] = []

    async def respond(message_id: str | None) -> None:
        assert message_id is not None
        tracked = stop_manager.tracked_messages[message_id]
        observed.append((tracked.target, tracked.run_id, tracked.task.done()))

    result = await runner.run(
        ResponseAttemptRequest(
            target=target,
            response_function=respond,
            existing_event_id="$placeholder",
            run_id="run-1",
        ),
    )

    assert result == "$placeholder"
    assert observed == [(target, "run-1", False)]
    assert stop_manager.cleared == ["$placeholder"]
    assert stop_manager.tracked_messages == {}


@pytest.mark.asyncio
async def test_stop_tracking_cleared_on_failure(tmp_path: Path) -> None:
    """Generation failures re-raise but never leave dangling stop tracking."""
    stop_manager = RecordingStopManager()
    runner = _attempt_runner(tmp_path, stop_manager)

    async def respond(_message_id: str | None) -> None:
        msg = "generation exploded"
        raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="generation exploded"):
        await runner.run(
            ResponseAttemptRequest(target=_target(), response_function=respond, existing_event_id="$placeholder"),
        )

    assert stop_manager.cleared == ["$placeholder"]
    assert stop_manager.tracked_messages == {}


@pytest.mark.asyncio
async def test_approval_suspension_is_not_logged_as_generation_failure(tmp_path: Path) -> None:
    """A native pause is a lifecycle handoff, not an exceptional generation failure."""
    stop_manager = RecordingStopManager()
    logger = MagicMock()
    runner = ResponseAttemptRunner(replace(_attempt_runner(tmp_path, stop_manager).deps, logger=logger))
    suspension = ResponsePausedForApproval(
        PausedAttempt(
            session_id="session-1",
            run_id="run-paused",
            tools=(ToolExecution(tool_call_id="call-1", tool_name="dangerous", requires_confirmation=True),),
        ),
    )

    async def respond(_message_id: str | None) -> None:
        raise suspension

    with pytest.raises(ResponsePausedForApproval) as raised:
        await runner.run(
            ResponseAttemptRequest(target=_target(), response_function=respond, existing_event_id="$placeholder"),
        )

    assert raised.value is suspension
    logger.exception.assert_not_called()
    assert stop_manager.cleared == ["$placeholder"]


@pytest.mark.asyncio
async def test_attempt_without_visible_message_tracks_synthetic_key(tmp_path: Path) -> None:
    """No placeholder and no existing event still produces stop-trackable state."""
    stop_manager = RecordingStopManager()
    runner = _attempt_runner(tmp_path, stop_manager)
    tracked_keys: list[str] = []

    async def respond(message_id: str | None) -> None:
        assert message_id is None
        tracked_keys.extend(stop_manager.tracked_messages)

    result = await runner.run(ResponseAttemptRequest(target=_target(), response_function=respond))

    assert result is None
    assert len(tracked_keys) == 1
    assert tracked_keys[0].startswith("__pending_response__:")
    assert stop_manager.cleared == tracked_keys
    assert stop_manager.tracked_messages == {}


# ---------------------------------------------------------------------------
# 3. Cancellation: user stop mid-generation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_stop_mid_generation_cancels_task_and_clears_tracking(tmp_path: Path) -> None:
    """A stop reaction mid-generation cancels the attempt, records the outcome, and clears tracking."""
    stop_manager = RecordingStopManager()
    runner = _attempt_runner(tmp_path, stop_manager)
    started = asyncio.Event()
    cancel_reasons: list[str] = []

    async def respond(_message_id: str | None) -> None:
        started.set()
        await asyncio.Event().wait()

    run_task = asyncio.create_task(
        runner.run(
            ResponseAttemptRequest(
                target=_target(),
                response_function=respond,
                existing_event_id="$placeholder",
                on_cancelled=cancel_reasons.append,
            ),
        ),
    )
    await asyncio.wait_for(started.wait(), timeout=2)
    tracked = stop_manager.tracked_messages["$placeholder"]

    assert stop_manager.request_stop_if("$placeholder", lambda: True) is True
    # The attempt survives the cancellation and still reports its visible event id.
    assert await asyncio.wait_for(run_task, timeout=2) == "$placeholder"

    assert tracked.task.cancelled()
    assert cancel_reasons == ["cancelled_by_user"]
    assert stop_manager.cleared == ["$placeholder"]
    assert stop_manager.tracked_messages == {}


# ---------------------------------------------------------------------------
# 4. Streaming vs non-streaming delivery through DeliveryGateway
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_streaming_response_delivers_through_deliver_final(tmp_path: Path) -> None:
    """The non-streaming path hands the generated text to DeliveryGateway.deliver_final once."""
    bot = _bot(tmp_path)
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    deliver_final = AsyncMock(return_value=_completed_outcome("$response", body="final text"))

    with (
        patch.object(DeliveryGateway, "deliver_final", new=deliver_final),
        patch_response_runner_module(
            ai_response=AsyncMock(return_value="final text"),
            typing_indicator=_noop_typing,
        ),
    ):
        generation = await coordinator._process_and_respond(_plain_request(_target()))

    assert generation.delivery.event_id == "$response"
    deliver_final.assert_awaited_once()
    final_request = deliver_final.await_args.args[0]
    assert final_request.response_text == "final text"
    assert final_request.target.room_id == "!room:localhost"
    assert final_request.identity.response_kind == "ai"
    assert final_request.existing_event_id is None


@pytest.mark.asyncio
async def test_non_streaming_invisible_delivery_does_not_mark_substantive_reply(tmp_path: Path) -> None:
    """A failed final delivery must not turn a thinking placeholder into a substantive reply."""
    bot = _bot(tmp_path)
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    delivery = FinalDeliveryOutcome(
        terminal_status="error",
        event_id=None,
        failure_reason="delivery_failed",
    )
    timing = DispatchPipelineTiming(source_event_id="$request", room_id="!room:localhost")
    timing.mark_first_visible_reply("placeholder")
    request = replace(_plain_request(_target()), pipeline_timing=timing)

    with (
        patch.object(DeliveryGateway, "deliver_final", new=AsyncMock(return_value=delivery)),
        patch_response_runner_module(
            ai_response=AsyncMock(return_value="final text"),
            typing_indicator=_noop_typing,
        ),
    ):
        generation = await coordinator._process_and_respond(request)

    assert generation.delivery is delivery
    assert timing.metadata["first_visible_kind"] == "placeholder"
    assert "first_substantive_reply" not in timing.marks
    assert "first_substantive_kind" not in timing.metadata


@pytest.mark.asyncio
async def test_non_streaming_failed_edit_preserving_old_body_does_not_mark_substantive_reply(
    tmp_path: Path,
) -> None:
    """A preserved old answer must not count as newly delivered substantive content."""
    bot = _bot(tmp_path)
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    delivery = FinalDeliveryOutcome(
        terminal_status="error",
        event_id="$existing",
        is_visible_response=True,
        failure_reason="delivery_failed",
    )
    timing = DispatchPipelineTiming(source_event_id="$request", room_id="!room:localhost")
    request = replace(
        _plain_request(_target()),
        existing_event_id="$existing",
        pipeline_timing=timing,
    )

    with (
        patch.object(DeliveryGateway, "deliver_final", new=AsyncMock(return_value=delivery)),
        patch_response_runner_module(
            ai_response=AsyncMock(return_value="replacement text"),
            typing_indicator=_noop_typing,
        ),
    ):
        generation = await coordinator._process_and_respond(request)

    assert generation.delivery is delivery
    assert "first_substantive_reply" not in timing.marks
    assert "first_substantive_kind" not in timing.metadata


@pytest.mark.asyncio
async def test_streaming_response_streams_then_finalizes_through_gateway(tmp_path: Path) -> None:
    """The streaming path delivers via deliver_stream, then finalizes the same transport outcome."""
    bot = _bot(tmp_path)
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    transport = StreamTransportOutcome(
        last_physical_stream_event_id="$stream",
        terminal_status="completed",
        rendered_body="streamed body",
        visible_body_state="visible_body",
    )
    deliver_stream = AsyncMock(return_value=transport)
    finalize = AsyncMock(return_value=_completed_outcome("$stream", body="streamed body"))

    async def fake_stream(*_args: object, **_kwargs: object) -> AsyncIterator[str]:
        yield "chunk"

    with (
        patch.object(DeliveryGateway, "deliver_stream", new=deliver_stream),
        patch.object(DeliveryGateway, "finalize_streamed_response", new=finalize),
        patch_response_runner_module(
            stream_agent_response=fake_stream,
            typing_indicator=_noop_typing,
        ),
    ):
        generation = await coordinator._process_and_respond_streaming(_plain_request(_target()))

    assert generation.delivery.event_id == "$stream"
    deliver_stream.assert_awaited_once()
    assert deliver_stream.await_args.args[0].existing_event_id is None
    finalize.assert_awaited_once()
    finalize_request = finalize.await_args.args[0]
    assert finalize_request.stream_transport_outcome is transport
    assert finalize_request.initial_delivery_kind == "sent"
    assert finalize_request.identity.response_kind == "ai"


@pytest.mark.asyncio
async def test_streaming_approval_pause_reaches_outer_lifecycle(tmp_path: Path) -> None:
    """The agent streaming wrapper must preserve the visible event when it propagates a pause."""
    coordinator = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    pause = ResponsePausedForApproval(
        PausedAttempt(
            session_id="session-1",
            run_id="run-paused",
            tools=(ToolExecution(tool_call_id="call-1", tool_name="dangerous", requires_confirmation=True),),
        ),
    )
    finalize = AsyncMock()
    visible_events: list[str] = []

    async def pause_after_visible_event(*_args: object, **kwargs: object) -> None:
        callback = cast("Callable[[str], None]", kwargs["visible_event_id_callback"])
        callback("$stream")
        raise pause

    with (
        patch.object(
            coordinator,
            "generate_streaming_ai_response",
            new=AsyncMock(side_effect=pause_after_visible_event),
        ),
        patch.object(DeliveryGateway, "finalize_streamed_response", new=finalize),
        pytest.raises(ResponsePausedForApproval) as raised,
    ):
        await coordinator._process_and_respond_streaming(
            _plain_request(_target()),
            on_delivery_started=visible_events.append,
        )

    assert raised.value is pause
    assert visible_events == ["$stream"]
    finalize.assert_not_awaited()


@pytest.mark.parametrize("is_team", [False, True])
@pytest.mark.asyncio
async def test_suspended_turn_is_never_persisted_as_failed(tmp_path: Path, *, is_team: bool) -> None:
    """Failure cleanup must not overwrite the Agno run that owns a native pause."""
    coordinator = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    recorder = TurnRecorder(user_message="Run it")
    recorder.mark_suspended()
    persist = AsyncMock()

    with patch.object(coordinator, "_persist_interrupted_recorder_off_loop", new=persist):
        await coordinator._persist_failed_turn(
            recorder,
            is_team=is_team,
            session_scope=coordinator.deps.state_writer.history_scope(),
            session_id="session-1",
            execution_identity=None,
            run_id="run-paused",
            response_event_id="$waiting",
        )

    persist.assert_not_awaited()


@pytest.mark.asyncio
async def test_streaming_placeholder_only_delivery_does_not_mark_substantive_reply(tmp_path: Path) -> None:
    """Placeholder-only stream finalization must not report visible answer text."""
    bot = _bot(tmp_path)
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    transport = StreamTransportOutcome(
        last_physical_stream_event_id="$placeholder",
        terminal_status="completed",
        rendered_body="Thinking...",
        visible_body_state="placeholder_only",
    )
    delivery = FinalDeliveryOutcome(
        terminal_status="completed",
        event_id=None,
    )
    timing = DispatchPipelineTiming(source_event_id="$request", room_id="!room:localhost")
    timing.mark_first_visible_reply("placeholder")
    request = replace(_plain_request(_target()), pipeline_timing=timing)

    async def fake_stream(*_args: object, **_kwargs: object) -> AsyncIterator[str]:
        yield ""

    with (
        patch.object(DeliveryGateway, "deliver_stream", new=AsyncMock(return_value=transport)),
        patch.object(DeliveryGateway, "finalize_streamed_response", new=AsyncMock(return_value=delivery)),
        patch_response_runner_module(
            stream_agent_response=fake_stream,
            typing_indicator=_noop_typing,
        ),
    ):
        generation = await coordinator._process_and_respond_streaming(request)

    assert generation.delivery is delivery
    assert timing.metadata["first_visible_kind"] == "placeholder"
    assert "first_substantive_reply" not in timing.marks
    assert "first_substantive_kind" not in timing.metadata


@pytest.mark.asyncio
async def test_streaming_midstream_failure_persists_partial_and_finalizes_error(tmp_path: Path) -> None:
    """A mid-stream delivery failure persists the partial turn and finalizes the error transport outcome."""
    bot = _bot(tmp_path)
    # Mock the logger collaborator: the production rich traceback renderer is pathologically
    # slow on mock-laden tracebacks, and the log call itself is part of the pinned fallback.
    coordinator = replace_response_runner_deps(bot, logger=MagicMock())
    error_transport = StreamTransportOutcome(
        last_physical_stream_event_id="$stream",
        terminal_status="error",
        rendered_body="partial body",
        visible_body_state="visible_body",
        failure_reason="boom",
    )
    error_outcome = FinalDeliveryOutcome(
        terminal_status="error",
        event_id="$stream",
        is_visible_response=True,
        final_visible_body="partial body",
        failure_reason="boom",
    )
    deliver_stream = AsyncMock(
        side_effect=StreamingDeliveryError(
            RuntimeError("boom"),
            event_id="$stream",
            accumulated_text="partial body",
            tool_trace=[],
            transport_outcome=error_transport,
        ),
    )
    finalize = AsyncMock(return_value=error_outcome)
    persist = MagicMock()

    async def fake_stream(*_args: object, **_kwargs: object) -> AsyncIterator[str]:
        yield "chunk"

    with (
        patch.object(DeliveryGateway, "deliver_stream", new=deliver_stream),
        patch.object(DeliveryGateway, "finalize_streamed_response", new=finalize),
        patch("mindroom.response_runner.persist_interrupted_replay_snapshot", new=persist),
        patch_response_runner_module(
            stream_agent_response=fake_stream,
            typing_indicator=_noop_typing,
        ),
    ):
        request = replace(
            _plain_request(_target()),
            model_prompt="hello\n\n<mindroom_message_context>persist me</mindroom_message_context>",
        )
        generation = await coordinator._process_and_respond_streaming(request)

    # The failure does not propagate: it is logged and becomes a finalized error outcome.
    assert generation.delivery is error_outcome
    coordinator.deps.logger.exception.assert_called_once_with("Error in streaming response", error="boom")
    finalize.assert_awaited_once()
    assert finalize.await_args.args[0].stream_transport_outcome is error_transport
    # The partial reply was captured as an interrupted-replay snapshot exactly once.
    persist.assert_called_once()
    snapshot = persist.call_args.kwargs["snapshot"]
    assert snapshot.user_message == "hello\n\n<mindroom_message_context>persist me</mindroom_message_context>"
    assert snapshot.partial_text == "partial body"
    assert snapshot.run_metadata["matrix_response_event_id"] == "$stream"
    assert persist.call_args.kwargs["is_team"] is False


@pytest.mark.asyncio
async def test_streaming_midstream_failure_persists_partial_off_event_loop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Interrupted replay snapshot persistence should run through the thread offload boundary."""
    bot = _bot(tmp_path)
    coordinator = replace_response_runner_deps(bot, logger=MagicMock())
    error_transport = StreamTransportOutcome(
        last_physical_stream_event_id="$stream",
        terminal_status="error",
        rendered_body="partial body",
        visible_body_state="visible_body",
        failure_reason="boom",
    )
    error_outcome = FinalDeliveryOutcome(
        terminal_status="error",
        event_id="$stream",
        is_visible_response=True,
        final_visible_body="partial body",
        failure_reason="boom",
    )
    in_worker = False

    async def fake_to_thread(function: object, *args: object, **kwargs: object) -> object:
        nonlocal in_worker
        in_worker = True
        try:
            return function(*args, **kwargs)  # type: ignore[misc]
        finally:
            in_worker = False

    def persist(**_kwargs: object) -> None:
        assert in_worker

    async def fake_stream(*_args: object, **_kwargs: object) -> AsyncIterator[str]:
        yield "chunk"

    monkeypatch.setattr(response_runner.asyncio, "to_thread", fake_to_thread)
    with (
        patch.object(
            DeliveryGateway,
            "deliver_stream",
            new=AsyncMock(
                side_effect=StreamingDeliveryError(
                    RuntimeError("boom"),
                    event_id="$stream",
                    accumulated_text="partial body",
                    tool_trace=[],
                    transport_outcome=error_transport,
                ),
            ),
        ),
        patch.object(DeliveryGateway, "finalize_streamed_response", new=AsyncMock(return_value=error_outcome)),
        patch("mindroom.response_runner.persist_interrupted_replay_snapshot", new=persist),
        patch_response_runner_module(
            stream_agent_response=fake_stream,
            typing_indicator=_noop_typing,
        ),
    ):
        await coordinator._process_and_respond_streaming(_plain_request(_target()))


@pytest.mark.asyncio
async def test_agent_streaming_sync_restart_cancelled_outcome_registers_retry(tmp_path: Path) -> None:
    """A visible stream cancelled by sync restart should be retried even when no outer task cancel fired."""
    bot = _bot(tmp_path)
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    retries: list[str] = []
    cancelled_outcome = FinalDeliveryOutcome(
        terminal_status="cancelled",
        event_id="$stream",
        is_visible_response=True,
        final_visible_body=f"partial\n\n{RESTART_INTERRUPTED_RESPONSE_NOTE}",
        delivery_kind="edited",
        failure_reason="sync_restart_cancelled",
    )

    async def fake_run_cancellable_response(**kwargs: object) -> str:
        response_function = kwargs["response_function"]
        await response_function("$thinking")  # type: ignore[operator]
        return "$thinking"

    with (
        patch.object(
            coordinator,
            "_run_cancellable_response",
            new=AsyncMock(side_effect=fake_run_cancellable_response),
        ),
        patch.object(
            coordinator,
            "_process_and_respond_streaming",
            new=AsyncMock(return_value=_ResponseGenerationOutcome(delivery=cancelled_outcome, run_succeeded=False)),
        ),
        patch_response_runner_module(
            should_use_streaming=AsyncMock(return_value=True),
            apply_post_response_effects=AsyncMock(),
        ),
    ):
        result = await coordinator.generate_response(
            replace(
                _plain_request(_target(thread_id="$thread")),
                on_interrupted_response_recoverable=lambda: retries.append("retry"),
            ),
        )

    assert result == "$stream"
    assert retries == ["retry"]


@pytest.mark.asyncio
async def test_cancelled_interrupted_persistence_offload_keeps_running(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A second cancellation should not cancel the in-flight persistence worker."""
    bot = _bot(tmp_path)
    coordinator = replace_response_runner_deps(bot)
    started = asyncio.Event()
    release = asyncio.Event()
    persisted: list[str] = []

    async def fake_to_thread(function: object, *args: object, **kwargs: object) -> object:
        started.set()
        await release.wait()
        return function(*args, **kwargs)  # type: ignore[misc]

    def persist(**kwargs: object) -> None:
        persisted.append(str(kwargs["session_id"]))

    monkeypatch.setattr(response_runner.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(coordinator, "_persist_interrupted_recorder", persist)

    task = asyncio.create_task(
        coordinator._persist_interrupted_recorder_off_loop(
            recorder=TurnRecorder(user_message="hello"),
            session_scope=coordinator.deps.state_writer.history_scope(),
            session_id="session",
            execution_identity=None,
            run_id="run",
            is_team=False,
            response_event_id="$response",
        ),
    )
    await started.wait()

    registered_tasks = background_tasks_module._tasks_for_owner(coordinator.deps.runtime)
    assert len(registered_tasks) == 1
    assert registered_tasks[0].get_name() == "persist_interrupted_recorder"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    release.set()
    await wait_for_background_tasks(timeout=1.0, owner=coordinator.deps.runtime)

    assert persisted == ["session"]


# ---------------------------------------------------------------------------
# 5. Queued-notice state (response_lifecycle.py)
# ---------------------------------------------------------------------------


def _queued_envelope(source_event_id: str) -> MessageEnvelope:
    return request_envelope(
        room_id="!room:localhost",
        thread_id="$thread",
        reply_to_event_id=source_event_id,
        agent_name="general",
    )


def test_reserve_waiting_human_message_requires_active_turn() -> None:
    """No queued-human notice is reserved when the conversation is idle."""
    coordinator = ResponseLifecycleCoordinator()
    envelope = _queued_envelope("$first")

    assert coordinator.reserve_waiting_human_message(target=envelope.target, response_envelope=envelope) is None


@pytest.mark.asyncio
async def test_response_lifecycle_reservation_is_not_its_own_queued_human_turn() -> None:
    """An early reservation must not signal the interactive response about itself."""
    coordinator = ResponseLifecycleCoordinator()
    envelope = _queued_envelope("$interactive")
    reservation = await coordinator.reserve_response_lifecycle(envelope)
    observed_pending: list[int] = []
    observed_active_turns: list[int] = []

    async def locked_operation(_target: MessageTarget) -> str:
        queued_signal = coordinator._get_or_create_queued_signal(envelope.target)
        observed_pending.append(queued_signal.pending_human_messages)
        observed_active_turns.append(queued_signal._active_response_turns)
        return "interactive"

    with response_lifecycle_reservation_context(reservation):
        result = await coordinator.run_locked_response(
            target=envelope.target,
            response_envelope=envelope,
            pipeline_timing=None,
            locked_operation=locked_operation,
        )

    assert result == "interactive"
    assert observed_pending == [0]
    assert observed_active_turns == [1]
    assert not coordinator.has_active_response_for_target(envelope.target)


@pytest.mark.asyncio
async def test_queued_lifecycle_reservation_preserves_notice_for_older_active_response() -> None:
    """A reserved human response queued behind an older turn still signals that turn."""
    coordinator = ResponseLifecycleCoordinator()
    first_envelope = _queued_envelope("$first")
    second_envelope = _queued_envelope("$second")
    first_entered = asyncio.Event()
    release_first = asyncio.Event()

    async def first_operation(_target: MessageTarget) -> str:
        first_entered.set()
        await release_first.wait()
        return "first"

    first = asyncio.create_task(
        coordinator.run_locked_response(
            target=first_envelope.target,
            response_envelope=first_envelope,
            pipeline_timing=None,
            locked_operation=first_operation,
        ),
    )
    await first_entered.wait()
    reservation = await coordinator.reserve_response_lifecycle(second_envelope)
    queued_signal = coordinator._get_or_create_queued_signal(second_envelope.target)
    assert queued_signal.pending_human_message_event_ids == {"$second"}

    with response_lifecycle_reservation_context(reservation):
        second = asyncio.create_task(
            coordinator.run_locked_response(
                target=second_envelope.target,
                response_envelope=second_envelope,
                pipeline_timing=None,
                locked_operation=lambda _target: asyncio.sleep(0, result="second"),
            ),
        )

        release_first.set()
        assert await first == "first"
        assert await second == "second"


@pytest.mark.asyncio
async def test_queued_response_lifecycle_reservation_cancellation_does_not_leak_lock() -> None:
    """Cancelling a queued reservation removes it without stealing the released lock."""
    coordinator = ResponseLifecycleCoordinator()
    envelope = _queued_envelope("$interactive")
    lifecycle_lock = coordinator._response_lifecycle_lock(envelope.target)
    await lifecycle_lock.acquire()
    reservation = await coordinator.reserve_response_lifecycle(envelope)
    queued_signal = coordinator._get_or_create_queued_signal(envelope.target)
    assert queued_signal.pending_human_message_event_ids == {envelope.source_event_id}
    assert queued_signal.has_active_response_turn()

    await reservation.release()
    await reservation.release()
    assert queued_signal.pending_human_message_event_ids == set()
    assert not queued_signal.has_active_response_turn()
    lifecycle_lock.release()

    assert (
        await asyncio.wait_for(
            coordinator.run_locked_response(
                target=envelope.target,
                response_envelope=envelope,
                pipeline_timing=None,
                locked_operation=lambda _target: asyncio.sleep(0, result="next"),
            ),
            timeout=1.0,
        )
        == "next"
    )


@pytest.mark.asyncio
async def test_response_lifecycle_reservation_surfaces_lock_acquire_failure() -> None:
    """A failed acquire task must wake reservation startup and remain observable."""
    coordinator = ResponseLifecycleCoordinator()
    envelope = _queued_envelope("$interactive")
    lifecycle_lock = coordinator._response_lifecycle_lock(envelope.target)

    async def fail_acquire() -> bool:
        msg = "lock acquire failed"
        raise RuntimeError(msg)

    lifecycle_lock.acquire = fail_acquire  # type: ignore[method-assign]
    reservation = await asyncio.wait_for(
        coordinator.reserve_response_lifecycle(envelope),
        timeout=0.1,
    )
    try:
        with pytest.raises(RuntimeError, match="lock acquire failed"):
            await reservation.wait_until_acquired()
    finally:
        await reservation.release()


@pytest.mark.asyncio
async def test_response_lifecycle_reservation_rejects_wrong_target_and_coordinator() -> None:
    """A reservation can only enter its creating coordinator and lifecycle key."""
    coordinator = ResponseLifecycleCoordinator()
    other_coordinator = ResponseLifecycleCoordinator()
    envelope = _queued_envelope("$interactive")
    other_target = MessageTarget.resolve("!room:localhost", "$other-thread", "$other")
    reservation = await coordinator.reserve_response_lifecycle(envelope)

    try:
        with pytest.raises(ValueError, match="different coordinator"):
            reservation.consume(other_coordinator, envelope.target)
        with pytest.raises(ValueError, match="target does not match"):
            reservation.consume(coordinator, other_target)
    finally:
        await reservation.release()


@pytest.mark.asyncio
async def test_queued_human_notice_is_registered_exactly_once() -> None:
    """A request arriving mid-turn registers one queued notice that drains when it owns the lock."""
    coordinator = ResponseLifecycleCoordinator()
    first_envelope = _queued_envelope("$first")
    second_envelope = _queued_envelope("$second")
    gate = asyncio.Event()
    in_first_turn = asyncio.Event()
    pending_during_second_turn: list[int] = []

    async def first_turn(_target: MessageTarget) -> str:
        in_first_turn.set()
        await gate.wait()
        return "first"

    first = asyncio.create_task(
        coordinator.run_locked_response(
            target=first_envelope.target,
            response_envelope=first_envelope,
            pipeline_timing=None,
            locked_operation=first_turn,
        ),
    )
    await asyncio.wait_for(in_first_turn.wait(), timeout=2)
    assert coordinator.has_active_response_for_target(first_envelope.target)

    reservation = coordinator.reserve_waiting_human_message(
        target=second_envelope.target,
        response_envelope=second_envelope,
    )
    assert reservation is not None
    queued_signal = coordinator._thread_queued_signals[
        ResponseLifecycleKey(room_id="!room:localhost", thread_id="$thread")
    ]
    assert queued_signal.pending_human_messages == 1
    # A duplicate reservation for the same queued event must not double-register.
    assert (
        coordinator.reserve_waiting_human_message(target=second_envelope.target, response_envelope=second_envelope)
        is None
    )
    assert queued_signal.pending_human_messages == 1
    reservation.consume()
    assert queued_signal.pending_human_messages == 0

    async def second_turn(_target: MessageTarget) -> str:
        pending_during_second_turn.append(queued_signal.pending_human_messages)
        return "second"

    second = asyncio.create_task(
        coordinator.run_locked_response(
            target=second_envelope.target,
            response_envelope=second_envelope,
            pipeline_timing=None,
            locked_operation=second_turn,
        ),
    )
    for _ in range(10):
        await asyncio.sleep(0)
    # While the queued turn waits for the lock the notice stays pending for the in-flight turn.
    assert queued_signal.pending_human_messages == 1

    gate.set()
    assert await asyncio.wait_for(first, timeout=2) == "first"
    assert await asyncio.wait_for(second, timeout=2) == "second"
    # The lifecycle consumes its own waiting notice when the queued request becomes active.
    assert pending_during_second_turn == [0]
    assert queued_signal.pending_human_messages == 0
    assert not coordinator.has_active_response_for_target(first_envelope.target)


@pytest.mark.asyncio
async def test_duplicate_queued_request_without_reservation_registers_one_notice() -> None:
    """Re-dispatching the same queued event while a turn runs never double-counts the notice."""
    coordinator = ResponseLifecycleCoordinator()
    first_envelope = _queued_envelope("$first")
    second_envelope = _queued_envelope("$second")
    gate = asyncio.Event()
    in_first_turn = asyncio.Event()

    async def first_turn(_target: MessageTarget) -> str:
        in_first_turn.set()
        await gate.wait()
        return "first"

    async def queued_turn(_target: MessageTarget) -> str:
        return "queued"

    def run_queued() -> asyncio.Task[str]:
        return asyncio.create_task(
            coordinator.run_locked_response(
                target=second_envelope.target,
                response_envelope=second_envelope,
                pipeline_timing=None,
                locked_operation=queued_turn,
            ),
        )

    first = asyncio.create_task(
        coordinator.run_locked_response(
            target=first_envelope.target,
            response_envelope=first_envelope,
            pipeline_timing=None,
            locked_operation=first_turn,
        ),
    )
    await asyncio.wait_for(in_first_turn.wait(), timeout=2)

    queued_one = run_queued()
    queued_two = run_queued()
    for _ in range(10):
        await asyncio.sleep(0)
    queued_signal = coordinator._thread_queued_signals[
        ResponseLifecycleKey(room_id="!room:localhost", thread_id="$thread")
    ]
    assert queued_signal.pending_human_messages == 1

    gate.set()
    assert await asyncio.wait_for(first, timeout=2) == "first"
    assert await asyncio.wait_for(queued_one, timeout=2) == "queued"
    assert await asyncio.wait_for(queued_two, timeout=2) == "queued"
    assert queued_signal.pending_human_messages == 0


# ---------------------------------------------------------------------------
# 6. Post-response effects ordering and gating
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("delivery_outcome", "hands_off"),
    [
        (_completed_outcome(), False),
        (
            FinalDeliveryOutcome(
                terminal_status="cancelled",
                event_id="$response",
                is_visible_response=True,
                failure_reason="cancelled_by_user",
            ),
            False,
        ),
        (
            FinalDeliveryOutcome(
                terminal_status="error",
                event_id="$response",
                is_visible_response=True,
                failure_reason="delivery_failed",
            ),
            False,
        ),
        (
            FinalDeliveryOutcome(
                terminal_status="suspended",
                event_id="$response",
                is_visible_response=True,
            ),
            True,
        ),
    ],
    ids=["completed", "cancelled", "error", "suspended"],
)
async def test_response_settlement_finalizes_and_transfers_ownership_once(
    tmp_path: Path,
    delivery_outcome: FinalDeliveryOutcome,
    hands_off: bool,
) -> None:
    """Each outcome finalizes once and only a durable pause transfers source ownership."""
    bot = _bot(tmp_path)
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    request = replace(
        _plain_request(_target()),
        source_handoff=asyncio.Event(),
    )
    progress = response_runner._DeliveryProgress()
    post_effects = AsyncMock()
    build_post_outcome = MagicMock(return_value=ResponseOutcome())

    async def generate(_message_id: str | None) -> None:
        progress.settle(delivery_outcome)

    async def run_cancellable_response(**kwargs: object) -> str:
        response_function = kwargs["response_function"]
        await response_function("$response")  # type: ignore[operator]
        return "$response"

    lifecycle = coordinator._build_lifecycle(
        identity=coordinator._response_identity(request, response_kind="ai"),
        request=request,
    )
    finalize = AsyncMock(wraps=lifecycle.finalize)
    with (
        patch.object(
            coordinator,
            "_run_cancellable_response",
            new=AsyncMock(side_effect=run_cancellable_response),
        ),
        patch.object(lifecycle, "finalize", new=finalize),
        patch_response_runner_module(apply_post_response_effects=post_effects),
    ):
        result = await coordinator._run_and_settle_locked_response(
            request,
            target=request.response_envelope.target,
            lifecycle=lifecycle,
            progress=progress,
            response_function=generate,
            user_id=request.user_id,
            run_id="run-1",
            build_post_response_outcome=build_post_outcome,
            post_response_deps=PostResponseEffectsDeps(logger=get_logger("tests.post_response")),
        )

    assert result == (None if hands_off else "$response")
    assert progress.delivery_outcome is delivery_outcome
    build_post_outcome.assert_called_once_with(delivery_outcome)
    finalize.assert_awaited_once()
    if hands_off:
        post_effects.assert_not_awaited()
    else:
        post_effects.assert_awaited_once()
    assert request.source_handoff is not None
    assert request.source_handoff.is_set() is hands_off


@pytest.mark.asyncio
async def test_terminal_settlement_registers_retry_before_rethrowing_cancel(tmp_path: Path) -> None:
    """A deferred sync-restart cancel should finalize once, register its retry, then re-raise."""
    bot = _bot(tmp_path)
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    order: list[str] = []
    request = replace(
        _plain_request(_target(thread_id="$thread")),
        on_interrupted_response_recoverable=lambda: order.append("retry"),
        on_deferred_outcome_handled=_async_callback(lambda event_id: order.append(f"handled:{event_id}")),
    )
    delivery_outcome = FinalDeliveryOutcome(
        terminal_status="cancelled",
        event_id="$response",
        is_visible_response=True,
        final_visible_body=RESTART_INTERRUPTED_RESPONSE_NOTE,
        delivery_kind="edited",
        failure_reason="sync_restart_cancelled",
    )
    progress = response_runner._DeliveryProgress()
    progress.note_delivery_started("$response")
    progress.settle(delivery_outcome)
    post_effects = AsyncMock(side_effect=lambda *_args: order.append("post_effects"))
    lifecycle = coordinator._build_lifecycle(
        identity=coordinator._response_identity(request, response_kind="ai"),
        request=request,
    )
    finalize = AsyncMock(wraps=lifecycle.finalize)

    with (
        patch.object(
            coordinator,
            "_run_cancellable_response",
            new=AsyncMock(side_effect=asyncio.CancelledError("sync_restart")),
        ),
        patch.object(lifecycle, "finalize", new=finalize),
        patch_response_runner_module(apply_post_response_effects=post_effects),
        pytest.raises(asyncio.CancelledError, match="sync_restart"),
    ):
        await coordinator._run_and_settle_locked_response(
            request,
            target=request.response_envelope.target,
            lifecycle=lifecycle,
            progress=progress,
            response_function=AsyncMock(),
            user_id=request.user_id,
            run_id="run-1",
            build_post_response_outcome=lambda _outcome: ResponseOutcome(),
            post_response_deps=PostResponseEffectsDeps(logger=get_logger("tests.post_response")),
        )

    assert order == ["post_effects", "retry", "handled:$response"]
    assert progress.delivery_outcome is delivery_outcome
    finalize.assert_awaited_once()
    post_effects.assert_awaited_once()


@pytest.mark.asyncio
async def test_uncommitted_interruption_rethrows_cancel_without_marking_source_handled(tmp_path: Path) -> None:
    """Checkpoint replay must remain actionable when no terminal recovery note landed."""
    coordinator = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    order: list[str] = []
    request = replace(
        _plain_request(_target(thread_id="$thread")),
        on_interrupted_response_recoverable=lambda: order.append("retry"),
        on_deferred_outcome_handled=_async_callback(lambda event_id: order.append(f"handled:{event_id}")),
    )
    progress = response_runner._DeliveryProgress()
    progress.note_delivery_started("$response")
    progress.settle(
        FinalDeliveryOutcome(
            terminal_status="cancelled",
            event_id="$response",
            is_visible_response=True,
            final_visible_body=RESTART_INTERRUPTED_RESPONSE_NOTE,
            failure_reason="sync_restart_cancelled",
        ),
    )
    lifecycle = coordinator._build_lifecycle(
        identity=coordinator._response_identity(request, response_kind="ai"),
        request=request,
    )
    post_effects = AsyncMock(side_effect=lambda *_args: order.append("post_effects"))

    with (
        patch.object(
            coordinator,
            "_run_cancellable_response",
            new=AsyncMock(side_effect=asyncio.CancelledError("sync_restart")),
        ),
        patch_response_runner_module(apply_post_response_effects=post_effects),
        pytest.raises(asyncio.CancelledError, match="sync_restart"),
    ):
        await coordinator._run_and_settle_locked_response(
            request,
            target=request.response_envelope.target,
            lifecycle=lifecycle,
            progress=progress,
            response_function=AsyncMock(),
            user_id=request.user_id,
            run_id="run-1",
            build_post_response_outcome=lambda _outcome: ResponseOutcome(),
            post_response_deps=PostResponseEffectsDeps(logger=get_logger("tests.post_response")),
        )

    assert order == ["post_effects"]


@pytest.mark.asyncio
async def test_cancel_cleanup_error_does_not_mark_source_handled(tmp_path: Path) -> None:
    """A failed cancellation cleanup must preserve replay instead of deduping the stale placeholder."""
    coordinator = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    callbacks: list[str] = []
    request = replace(
        _plain_request(_target(thread_id="$thread")),
        on_interrupted_response_recoverable=lambda: callbacks.append("recovery"),
        on_deferred_outcome_handled=_async_callback(lambda _event_id: callbacks.append("handled")),
    )
    progress = response_runner._DeliveryProgress()
    progress.settle(
        FinalDeliveryOutcome(
            terminal_status="error",
            event_id="$placeholder",
            is_visible_response=True,
            cancel_source="sync_restart",
            failure_reason="failed to redact cancelled placeholder",
        ),
    )
    lifecycle = coordinator._build_lifecycle(
        identity=coordinator._response_identity(request, response_kind="ai"),
        request=request,
    )

    with (
        patch.object(
            coordinator,
            "_run_cancellable_response",
            new=AsyncMock(return_value="$placeholder"),
        ),
        patch_response_runner_module(apply_post_response_effects=AsyncMock()),
    ):
        result = await coordinator._run_and_settle_locked_response(
            request,
            target=request.response_envelope.target,
            lifecycle=lifecycle,
            progress=progress,
            response_function=AsyncMock(),
            user_id=request.user_id,
            run_id="run-1",
            build_post_response_outcome=lambda _outcome: ResponseOutcome(),
            post_response_deps=PostResponseEffectsDeps(logger=get_logger("tests.post_response")),
        )

    assert result is None
    assert callbacks == []


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_error", [False, True], ids=["success", "error"])
async def test_terminal_send_cancellation_preserves_source_replay(
    tmp_path: Path,
    terminal_error: bool,
) -> None:
    """A restart cancel during a normal terminal edit must reach gateway and source settlement."""
    coordinator = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    target = _target(thread_id="$thread")
    callbacks: list[str] = []
    request = replace(
        _plain_request(target),
        on_interrupted_response_recoverable=lambda: callbacks.append("recovery"),
        on_deferred_outcome_handled=_async_callback(lambda _event_id: callbacks.append("handled")),
    )
    streaming = StreamingResponse(
        target=target,
        config=coordinator.deps.runtime.config,
        runtime_paths=coordinator.deps.runtime_paths,
    )
    streaming.event_id = "$response"
    streaming.accumulated_text = "partial answer"
    delivered = DeliveredMatrixEvent(
        event_id="$response",
        content_sent={"body": "partial answer"},
    )
    with patch("mindroom.streaming.edit_message_result", new=AsyncMock(return_value=delivered)):
        assert await streaming._send_or_edit_message(coordinator._client(), is_final=False)

    with patch(
        "mindroom.streaming.edit_message_result",
        new=AsyncMock(side_effect=asyncio.CancelledError("sync_restart")),
    ):
        transport_outcome = await streaming.finalize(
            coordinator._client(),
            error=RuntimeError("generation failed") if terminal_error else None,
        )

    final_outcome = await coordinator.deps.delivery_gateway.finalize_streamed_response(
        FinalizeStreamedResponseRequest(
            target=target,
            stream_transport_outcome=transport_outcome,
            initial_delivery_kind="sent",
            identity=coordinator._response_identity(request, response_kind="ai"),
            tool_trace=None,
            extra_content=None,
        ),
    )
    progress = response_runner._DeliveryProgress()
    progress.settle(final_outcome)
    lifecycle = coordinator._build_lifecycle(
        identity=coordinator._response_identity(request, response_kind="ai"),
        request=request,
    )
    with (
        patch.object(
            coordinator,
            "_run_cancellable_response",
            new=AsyncMock(return_value="$response"),
        ),
        patch_response_runner_module(apply_post_response_effects=AsyncMock()),
    ):
        result = await coordinator._run_and_settle_locked_response(
            request,
            target=target,
            lifecycle=lifecycle,
            progress=progress,
            response_function=AsyncMock(),
            user_id=request.user_id,
            run_id="run-1",
            build_post_response_outcome=lambda _outcome: ResponseOutcome(),
            post_response_deps=PostResponseEffectsDeps(logger=get_logger("tests.post_response")),
        )

    assert transport_outcome.terminal_status == "cancelled"
    assert transport_outcome.failure_reason == "sync_restart_cancelled"
    assert final_outcome.cancel_source == "sync_restart"
    assert result is None
    assert callbacks == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target", "delivery_kind"),
    [
        pytest.param(_target(thread_id="$thread"), None, id="terminal-update-not-committed"),
        pytest.param(_target(thread_id=None), "edited", id="threadless"),
    ],
)
async def test_unrecoverable_interruption_remains_unhandled_without_outer_cancel(
    tmp_path: Path,
    target: MessageTarget,
    delivery_kind: Literal["edited"] | None,
) -> None:
    """A cancelled outcome needs a landed, threaded recovery note before dedup."""
    coordinator = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    callbacks: list[str] = []
    request = replace(
        _plain_request(target),
        on_interrupted_response_recoverable=lambda: callbacks.append("recovery"),
        on_deferred_outcome_handled=_async_callback(lambda _event_id: callbacks.append("handled")),
    )
    progress = response_runner._DeliveryProgress()
    progress.settle(
        FinalDeliveryOutcome(
            terminal_status="cancelled",
            event_id="$response",
            is_visible_response=True,
            final_visible_body=RESTART_INTERRUPTED_RESPONSE_NOTE,
            delivery_kind=delivery_kind,
            failure_reason="sync_restart_cancelled",
        ),
    )
    lifecycle = coordinator._build_lifecycle(
        identity=coordinator._response_identity(request, response_kind="ai"),
        request=request,
    )

    with (
        patch.object(
            coordinator,
            "_run_cancellable_response",
            new=AsyncMock(return_value="$response"),
        ),
        patch_response_runner_module(apply_post_response_effects=AsyncMock()),
    ):
        result = await coordinator._run_and_settle_locked_response(
            request,
            target=target,
            lifecycle=lifecycle,
            progress=progress,
            response_function=AsyncMock(),
            user_id=request.user_id,
            run_id="run-1",
            build_post_response_outcome=lambda _outcome: ResponseOutcome(),
            post_response_deps=PostResponseEffectsDeps(logger=get_logger("tests.post_response")),
        )

    assert result is None
    assert callbacks == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_reason", "final_visible_body", "expected_recoveries", "expected_user_stops"),
    [
        ("interrupted", INTERRUPTED_RESPONSE_NOTE, ["recovery"], []),
        ("cancelled_by_user", "partial answer", [], [("$response", 7)]),
    ],
)
async def test_terminal_interruption_registers_recovery_unless_user_stopped(
    tmp_path: Path,
    failure_reason: str,
    final_visible_body: str,
    expected_recoveries: list[str],
    expected_user_stops: list[tuple[str, int]],
) -> None:
    """A visible terminal interruption remains recoverable except after an explicit user stop."""
    bot = _bot(tmp_path)
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    recoveries: list[str] = []
    user_stops: list[tuple[str, int]] = []
    coordinator._user_stop_receipt_orders["$response"] = {7}
    request = replace(
        _plain_request(_target(thread_id="$thread")),
        on_interrupted_response_recoverable=lambda: recoveries.append("recovery"),
        on_user_stop_handled=_async_callback(
            lambda event_id, receipt_order: user_stops.append((event_id, receipt_order)),
        ),
    )
    progress = response_runner._DeliveryProgress()
    progress.settle(
        FinalDeliveryOutcome(
            terminal_status="cancelled",
            event_id="$response",
            is_visible_response=True,
            final_visible_body=final_visible_body,
            delivery_kind="edited",
            failure_reason=failure_reason,
        ),
    )
    lifecycle = coordinator._build_lifecycle(
        identity=coordinator._response_identity(request, response_kind="ai"),
        request=request,
    )

    with (
        patch.object(
            coordinator,
            "_run_cancellable_response",
            new=AsyncMock(return_value="$response"),
        ),
        patch_response_runner_module(apply_post_response_effects=AsyncMock()),
    ):
        result = await coordinator._run_and_settle_locked_response(
            request,
            target=request.response_envelope.target,
            lifecycle=lifecycle,
            progress=progress,
            response_function=AsyncMock(),
            user_id=request.user_id,
            run_id="run-1",
            build_post_response_outcome=lambda _outcome: ResponseOutcome(),
            post_response_deps=PostResponseEffectsDeps(logger=get_logger("tests.post_response")),
        )

    assert result == "$response"
    assert recoveries == expected_recoveries
    assert user_stops == expected_user_stops


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "delivery_outcome",
    [
        _completed_outcome(),
        FinalDeliveryOutcome(
            terminal_status="error",
            event_id="$response",
            is_visible_response=True,
            failure_reason="delivery_failed",
        ),
    ],
    ids=["completed", "error"],
)
async def test_terminal_settlement_late_cancel_keeps_settled_outcome_canonical(
    tmp_path: Path,
    delivery_outcome: FinalDeliveryOutcome,
) -> None:
    """A late cancel records an existing terminal outcome without queueing a duplicate retry."""
    bot = _bot(tmp_path)
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    order: list[str] = []
    request = replace(
        _plain_request(_target()),
        on_interrupted_response_recoverable=lambda: order.append("retry"),
        on_deferred_outcome_handled=_async_callback(lambda event_id: order.append(f"handled:{event_id}")),
    )
    progress = response_runner._DeliveryProgress()
    progress.note_delivery_started("$response")
    progress.settle(delivery_outcome)
    post_effects = AsyncMock(side_effect=lambda *_args: order.append("post_effects"))
    lifecycle = coordinator._build_lifecycle(
        identity=coordinator._response_identity(request, response_kind="ai"),
        request=request,
    )
    finalize = AsyncMock(wraps=lifecycle.finalize)

    with (
        patch.object(
            coordinator,
            "_run_cancellable_response",
            new=AsyncMock(side_effect=asyncio.CancelledError("sync_restart")),
        ),
        patch.object(lifecycle, "finalize", new=finalize),
        patch_response_runner_module(apply_post_response_effects=post_effects),
        pytest.raises(asyncio.CancelledError, match="sync_restart"),
    ):
        await coordinator._run_and_settle_locked_response(
            request,
            target=request.response_envelope.target,
            lifecycle=lifecycle,
            progress=progress,
            response_function=AsyncMock(),
            user_id=request.user_id,
            run_id="run-1",
            build_post_response_outcome=lambda _outcome: ResponseOutcome(),
            post_response_deps=PostResponseEffectsDeps(logger=get_logger("tests.post_response")),
        )

    assert order == ["post_effects", "handled:$response"]
    assert progress.delivery_outcome is delivery_outcome
    finalize.assert_awaited_once()
    post_effects.assert_awaited_once()


@pytest.mark.asyncio
async def test_terminal_settlement_rethrows_generation_error_after_post_effects(tmp_path: Path) -> None:
    """A pre-delivery generation error should settle, finalize once, run effects, then re-raise."""
    bot = _bot(tmp_path)
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    request = _plain_request(_target())
    progress = response_runner._DeliveryProgress()
    post_effects = AsyncMock()
    build_post_outcome = MagicMock(return_value=ResponseOutcome())
    lifecycle = coordinator._build_lifecycle(
        identity=coordinator._response_identity(request, response_kind="ai"),
        request=request,
    )
    finalize = AsyncMock(wraps=lifecycle.finalize)

    with (
        patch.object(
            coordinator,
            "_run_cancellable_response",
            new=AsyncMock(side_effect=RuntimeError("generation failed")),
        ),
        patch.object(lifecycle, "finalize", new=finalize),
        patch_response_runner_module(apply_post_response_effects=post_effects),
        pytest.raises(RuntimeError, match="generation failed"),
    ):
        await coordinator._run_and_settle_locked_response(
            request,
            target=request.response_envelope.target,
            lifecycle=lifecycle,
            progress=progress,
            response_function=AsyncMock(),
            user_id=request.user_id,
            run_id="run-1",
            build_post_response_outcome=build_post_outcome,
            post_response_deps=PostResponseEffectsDeps(logger=get_logger("tests.post_response")),
        )

    assert progress.delivery_outcome is not None
    assert progress.delivery_outcome.terminal_status == "error"
    assert progress.delivery_outcome.failure_reason == "generation failed"
    build_post_outcome.assert_called_once_with(progress.delivery_outcome)
    finalize.assert_awaited_once()
    post_effects.assert_awaited_once()


@pytest.mark.asyncio
async def test_success_runs_post_response_effects_after_delivery(tmp_path: Path) -> None:
    """A successful turn applies post-response effects only after visible delivery completed."""
    bot = _bot(tmp_path)
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    order: list[str] = []
    effect_outcomes: list[FinalDeliveryOutcome] = []

    async def fake_send_text(_request: object) -> str:
        order.append("placeholder")
        return "$placeholder"

    async def fake_ai_response(*_args: object, **_kwargs: object) -> str:
        order.append("generate")
        return "final text"

    async def fake_deliver_final(_request: object) -> FinalDeliveryOutcome:
        order.append("deliver_final")
        return _completed_outcome("$response", body="final text")

    async def fake_post_effects(final_outcome: FinalDeliveryOutcome, *_args: object) -> None:
        order.append("post_effects")
        effect_outcomes.append(final_outcome)

    with (
        patch.object(DeliveryGateway, "send_text", new=AsyncMock(side_effect=fake_send_text)),
        patch.object(DeliveryGateway, "deliver_final", new=AsyncMock(side_effect=fake_deliver_final)),
        patch_response_runner_module(
            ai_response=AsyncMock(side_effect=fake_ai_response),
            should_use_streaming=AsyncMock(return_value=False),
            typing_indicator=_noop_typing,
            apply_post_response_effects=AsyncMock(side_effect=fake_post_effects),
        ),
    ):
        result = await coordinator.generate_response(_plain_request(_target()))

    assert result == "$response"
    assert order == ["placeholder", "generate", "deliver_final", "post_effects"]
    assert effect_outcomes[0].terminal_status == "completed"
    assert effect_outcomes[0].final_visible_event_id == "$response"


@pytest.mark.asyncio
async def test_delivery_failure_emits_cancelled_hook_and_passes_error_outcome_to_effects(tmp_path: Path) -> None:
    """A failed delivery emits the cancelled hook, skips after-response, and forwards the error outcome."""
    bot = _bot(tmp_path)
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    error_outcome = FinalDeliveryOutcome(
        terminal_status="error",
        event_id=None,
        failure_reason="delivery_failed",
    )
    effect_outcomes: list[FinalDeliveryOutcome] = []

    async def fake_post_effects(final_outcome: FinalDeliveryOutcome, *_args: object) -> None:
        effect_outcomes.append(final_outcome)

    with (
        patch.object(DeliveryGateway, "send_text", new=AsyncMock(return_value="$placeholder")),
        patch.object(DeliveryGateway, "deliver_final", new=AsyncMock(return_value=error_outcome)),
        patch.object(
            bot._delivery_gateway.deps.response_hooks,
            "emit_after_response",
            new=AsyncMock(),
        ) as mock_after,
        patch.object(
            bot._delivery_gateway.deps.response_hooks,
            "emit_cancelled_response",
            new=AsyncMock(),
        ) as mock_cancelled,
        patch_response_runner_module(
            ai_response=AsyncMock(return_value="final text"),
            should_use_streaming=AsyncMock(return_value=False),
            typing_indicator=_noop_typing,
            apply_post_response_effects=AsyncMock(side_effect=fake_post_effects),
        ),
    ):
        result = await coordinator.generate_response(_plain_request(_target()))

    assert result is None
    mock_after.assert_not_awaited()
    mock_cancelled.assert_awaited_once()
    assert mock_cancelled.await_args.kwargs["failure_reason"] == "delivery_failed"
    # The effects step still runs, but receives the error outcome so success effects are gated off.
    assert effect_outcomes == [error_outcome]


@pytest.mark.asyncio
async def test_apply_post_response_effects_gates_success_only_side_effects() -> None:
    """Memory persistence and run-event linkage run on success and stay off after a failed delivery."""
    memory_calls: list[str] = []
    persisted: list[tuple[str, str]] = []
    deps = PostResponseEffectsDeps(
        logger=get_logger("tests.post_effects"),
        queue_memory_persistence=lambda: memory_calls.append("memory"),
        persist_response_event_id=lambda run_id, event_id: persisted.append((run_id, event_id)),
    )

    await apply_post_response_effects(
        _completed_outcome("$response", body="ok"),
        ResponseOutcome(response_run_id="run-1", run_succeeded=True),
        deps,
    )
    assert memory_calls == ["memory"]
    assert persisted == [("run-1", "$response")]

    await apply_post_response_effects(
        FinalDeliveryOutcome(terminal_status="error", event_id=None, failure_reason="delivery_failed"),
        ResponseOutcome(response_run_id="run-1", run_succeeded=False),
        deps,
    )
    # The failed delivery added neither memory persistence nor run-event linkage.
    assert memory_calls == ["memory"]
    assert persisted == [("run-1", "$response")]
