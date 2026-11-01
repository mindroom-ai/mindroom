"""Focused unit tests for ResponseRunner and ResponseAttemptRunner.

These pin the response-execution seam directly (lifecycle lock, attempt
mechanics, cancellation, streaming vs non-streaming delivery, queued-notice
state, and post-response effects) on a bot built in a ready test runtime,
without booting an orchestrator or syncing with Matrix.
"""

from __future__ import annotations

import asyncio
import html
import inspect
import json
from contextlib import asynccontextmanager, contextmanager, suppress
from dataclasses import dataclass, field, replace
from datetime import timedelta
from functools import partial
from itertools import count
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, Mock, create_autospec, patch

import nio
import pytest
from agno.tools.function import Function
from agno.tools.toolkit import Toolkit

from mindroom import interactive, response_runner
from mindroom import reply_lifecycle as rl
from mindroom.agent_cli import cli_approval_waits
from mindroom.agent_cli.events import stream_cli_events
from mindroom.agent_cli.lifetime import current_cli_lifetime, response_cli_lifetime
from mindroom.agent_cli.session import CliAuthenticationError, CliTurnOwner, TurnToolRegistry
from mindroom.ai_runtime import queued_message_signal_context
from mindroom.approval import receipt_readers as approval_receipt
from mindroom.approval import response as approval_response
from mindroom.approval.auto_approve_windows import start_auto_approve
from mindroom.approval.receipt import build_approval_receipt
from mindroom.approval.receipt_readers import take_unsent_receipt
from mindroom.approval.response import continuation_target, plan_approval_calls, require_ordered_pause_presentation
from mindroom.approval.tool_approval import NO_APPROVER_REASON
from mindroom.authorization import ReplyMembershipPendingError
from mindroom.background_tasks import wait_for_background_tasks
from mindroom.bot_runtime_view import BotRuntimeState
from mindroom.cancellation import SYNC_RESTART_CANCEL_MSG, current_task_is_process_shutdown, request_task_cancel
from mindroom.config.access import ResponderAccessConfig
from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.approval import ApprovalRuleConfig
from mindroom.config.cold_cache import ColdCacheConfig
from mindroom.config.main import Config
from mindroom.config.mid_turn import MidTurnConfig
from mindroom.config.models import ModelConfig
from mindroom.constants import (
    MATRIX_RESPONSE_EVENT_ID_METADATA_KEY,
    STREAM_STATUS_APPROVAL_PENDING,
    STREAM_STATUS_CANCELLED,
    STREAM_STATUS_COMPLETED,
    STREAM_STATUS_ERROR,
    STREAM_STATUS_KEY,
    STREAM_STATUS_PENDING,
    STREAM_STATUS_STREAMING,
)
from mindroom.conversation_resolver import ConversationResolver, MessageContext
from mindroom.delivery_gateway import (
    DeliveryGateway,
    SendTextRequest,
    StreamingDeliveryRequest,
)
from mindroom.dispatch_source import (
    HOOK_SOURCE_KIND,
    SCHEDULED_SOURCE_KIND,
    SILENT_SCHEDULE_SOURCE_KIND,
    ScheduledHistoryBudget,
)
from mindroom.entity_resolution import current_internal_sender_ids
from mindroom.event_journal import (
    ApprovalCall,
    ApprovalCardReservation,
    ApprovalContinuation,
    ApprovalDecision,
    ApprovalDecisionMetadata,
    ApprovalFailure,
    DeliveryStage,
    EventClass,
    EventKind,
    InboundEvent,
    PrincipalStore,
    ProjectedEvent,
)
from mindroom.event_journal.replies import ReplyStore
from mindroom.final_delivery import (
    AwaitingApproval,
    Cancelled,
    Completed,
    Failed,
    FailureCause,
    FinalDeliveryOutcome,
    Owed,
    StreamTransportOutcome,
    Suppressed,
    Suppression,
    TurnEnding,
    TurnProgress,
)
from mindroom.history.storage import new_scope_session
from mindroom.history.types import HistoryScope
from mindroom.llm.events import TextDelta
from mindroom.llm.media import Image
from mindroom.llm.runs import RunRecord, RunRequirement, RunStatus, TeamRunRecord, ToolExecution
from mindroom.logging_config import get_logger
from mindroom.matrix import typing as typing_module
from mindroom.matrix.client_delivery import DeliveredMatrixEvent
from mindroom.matrix.state import MatrixState
from mindroom.matrix.thread_history_result import ThreadHistoryResult
from mindroom.media_inputs import MediaInputs
from mindroom.message_target import MessageTarget, ResponseLifecycleKey
from mindroom.mid_turn import MidTurnGate, QueuedMessage
from mindroom.post_response_effects import PostResponseEffectsDeps, ResponseOutcome, apply_post_response_effects
from mindroom.reply_presentation import Presentation, Segment, encode_presentation
from mindroom.reply_turn.enrichment import prepare_memory_and_model_context
from mindroom.reply_turn.inbox import InboxResponses
from mindroom.reply_turn.locked_turn import LockedTurns
from mindroom.reply_turn.request import PreparedResponseRuntime, ResponseRequest, response_typing_indicator
from mindroom.response_admission import ResponseAdmissionRefusedError
from mindroom.response_attempt import ResponseAttemptDeps, ResponseAttemptRequest, ResponseAttemptRunner, SpanAttempt
from mindroom.response_lifecycle import ResponseLifecycleCoordinator, response_lifecycle_reservation_context
from mindroom.response_payload_preparation import (
    DispatchPayloadInputs,
    ResponsePayloadPreparation,
    ResponsePayloadPreparer,
)
from mindroom.response_sources import Owned, ResponseSources, Unclaimed, UnclaimedReason
from mindroom.response_turn import (
    AttemptResolved,
    CompletedApprovalRun,
    CompletedAttempt,
    PausedAnswer,
    PausedAttempt,
    ResponsePausedForApproval,
    ResponseTurnContext,
    TurnSinks,
    stream_response_turn,
)
from mindroom.room_model_overrides import set_room_model_override
from mindroom.runtime_shutdown import (
    ENTITY_REMOVED_SHUTDOWN,
    ORDERLY_SHUTDOWN,
    SYNC_RESTART_SHUTDOWN,
    ResponseShutdownTimeoutError,
    RuntimeShutdownIntent,
)
from mindroom.stop import SpanRegistry
from mindroom.streaming import (
    INTERRUPTED_RESPONSE_NOTE,
    RESTART_INTERRUPTED_RESPONSE_NOTE,
    StreamingDeliveryError,
    StreamingPresentation,
    StreamingResponse,
    UnfinishedStreamedReply,
)
from mindroom.teams.presentation import TeamStreamPresentation
from mindroom.teams.resume import continue_paused_team_run
from mindroom.thread_models import set_thread_model_override
from mindroom.thread_summary.schedule import thread_summary_message_count_hint
from mindroom.timing import DispatchPipelineTiming
from mindroom.tool_jobs.conversation import ConversationKey
from mindroom.tool_jobs.follow_ups import follow_up_envelope
from mindroom.tool_system.agno_toolkits import adapt_callable
from mindroom.tool_system.approval_exemptions import register_tool_approval_exemption
from mindroom.tool_system.events import StructuredStreamChunk, ToolTraceEntry, format_tool_started_event
from mindroom.tool_system.invocation import ToolCallContext, execute_tool_call
from mindroom.tool_system.output_files import ToolOutputFilePolicy
from mindroom.tool_system.tool_function import ToolGate
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from mindroom.turn_model import TurnModel
from mindroom.turn_policy import PreparedDispatch
from mindroom.turn_record import TurnRecord
from mindroom.turn_store import SourcePreparation
from tests.approval_card_helpers import resolve_card_event
from tests.approval_continuation_helpers import (
    advance_continuation,
    approval_continuation,
    claim_continuation,
    dispatch_approval,
    freeze_resume_final,
    resumed_approval,
)
from tests.bot_helpers import unique_room_send_responses
from tests.conftest import (
    SETTLE_TIMEOUT_SECONDS,
    FakeApprovalTransport,
    initialize_approval_store_for_test,
    install_model_providers,
    live_turn_claim,
    live_turn_tools_for_test,
    make_matrix_client_mock,
    make_visible_message,
    message_origin,
    patch_response_runner_module,
    replace_response_runner_deps,
    request_envelope,
    seed_session,
    turn_claim_is_live,
    unwrap_extracted_collaborator,
)
from tests.delegation_helpers import turn_tool_context
from tests.fakes.scripted_model import Await, ScriptedModel, answer
from tests.matrix_state_helpers import save_matrix_state
from tests.reply_span_helpers import paused_for_approval, response_span
from tests.reply_turn_helpers import runner_for
from tests.response_runner_helpers import (
    _bot,
    _envelope,
    _noop_typing,
    _plain_request,
    _target,
    attempt_in_place,
    completed_outcome,
    stream_transport,
    streamed_answer,
)
from tests.scripted_agent_helpers import stored_runs
from tests.streaming_helpers import FakeReplyRecords
from tests.test_agent_tool_calls import _catalog
from tests.test_response_attempt import _Span
from tests.test_response_turn import (
    _continuation,
    _ctx,
    _model_switch_execution,
    _streaming_adapter,
)
from tests.tool_approval_helpers import gated_functions

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Coroutine, Iterator, Mapping, Sequence
    from pathlib import Path
    from typing import Literal

    from nio import AsyncClient

    from mindroom.bot import AgentBot
    from mindroom.bot_runtime_view import BotRuntimeView
    from mindroom.hooks import MessageEnvelope
    from mindroom.judgment.state import JudgmentRequest
    from mindroom.reply_scope import ReplyRuntime, SpanHandle
    from mindroom.response_lifecycle import ResponseLifecycle, _QueuedMessageState
    from mindroom.response_runner import ResponseRunner
    from mindroom.response_sources import SourceFate
    from mindroom.streaming import ProgressPublisher
    from mindroom.tool_system.runtime_context import ToolRuntimeContext


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


def _inbox(
    *,
    replies: ReplyRuntime | None = None,
    retry_approval_sources: Callable[[str, tuple[str, ...]], None] | None = None,
    runtime: BotRuntimeView | None = None,
) -> InboxResponses:
    """Return an inbox whose collaborators not given are mocks."""
    return InboxResponses(
        logger=MagicMock(),
        replies=MagicMock() if replies is None else replies,
        retry_approval_sources=MagicMock() if retry_approval_sources is None else retry_approval_sources,
        runtime=MagicMock() if runtime is None else runtime,
    )


@pytest.mark.asyncio
async def test_process_shutdown_refuses_new_response_owners_for_good() -> None:
    """Orderly shutdown must close the owner set before concurrent producers run, and nothing reopens it."""
    runtime = BotRuntimeState(orchestrator=MagicMock())
    runner = runner_for(MagicMock(runtime=runtime))
    runtime.begin_process_shutdown()
    refused_response = asyncio.sleep(0)
    accidentally_owned: asyncio.Task[None] | None = None

    try:
        with pytest.raises(ResponseAdmissionRefusedError):
            accidentally_owned = runner.inbox.track_inbox_response(
                refused_response,
                name="test_late_process_shutdown_response",
                recovery_proof_ready=lambda: True,
                room_id=_target().room_id,
            )
    finally:
        if accidentally_owned is not None:
            accidentally_owned.cancel()
            await asyncio.gather(accidentally_owned, return_exceptions=True)

    assert refused_response.cr_frame is None
    assert runner.inbox.pending_inbox_response_count == 0

    assert runtime.resume() is False
    late_response = asyncio.sleep(0)
    with pytest.raises(ResponseAdmissionRefusedError):
        runner.inbox.track_inbox_response(
            late_response,
            name="test_response_after_resume_attempt",
            recovery_proof_ready=lambda: True,
            room_id=_target().room_id,
        )
    assert late_response.cr_frame is None
    assert runner.inbox.pending_inbox_response_count == 0


@pytest.mark.asyncio
async def test_process_shutdown_releases_claim_when_response_never_starts() -> None:
    """Task-lifetime cleanup must release a claim before recovery proof."""
    inbox = _inbox()
    response_started = False
    claim = live_turn_claim("$source")
    proof_claim_states: list[bool] = []

    async def never_started_response() -> None:
        nonlocal response_started
        response_started = True

    def recovery_proof_ready() -> bool:
        proof_claim_states.append(turn_claim_is_live(claim))
        return not turn_claim_is_live(claim)

    response = never_started_response()
    try:
        response_task = inbox.track_inbox_response(
            response,
            name="test_never_started_claimed_response",
            recovery_proof_ready=recovery_proof_ready,
            claim=claim,
            room_id=_target().room_id,
        )
    except BaseException:
        response.close()
        raise
    inbox.begin_process_shutdown()

    assert await inbox.drain_inbox_responses(
        cancel_after_seconds=0.01,
        shutdown_intent=ORDERLY_SHUTDOWN,
    )
    await asyncio.gather(response_task, return_exceptions=True)

    assert response_started is False
    assert not turn_claim_is_live(claim)
    assert proof_claim_states == [False]
    assert inbox.pending_inbox_response_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_before_start", [False, True])
async def test_inbox_response_releases_its_claim_before_its_sources_return_to_the_journal(
    *,
    cancel_before_start: bool,
) -> None:
    """A retried source must not meet the claim of the response that just ended, however early that response ended."""
    replies = MagicMock()
    replies.waits_to_claim.return_value = False
    claim = live_turn_claim("$source")
    claim_live_at_retry: list[bool] = []
    retry_sources = MagicMock()
    retry_sources.side_effect = lambda _room_id, _sources: claim_live_at_retry.append(
        turn_claim_is_live(claim),
    )
    inbox = _inbox(replies=replies, retry_approval_sources=retry_sources)
    held = asyncio.Event()

    async def response() -> None:
        assert turn_claim_is_live(claim), "a running response owns its claim"
        held.set()

    task = inbox.track_inbox_response(
        response(),
        name="inbox_response:$source",
        recovery_proof_ready=lambda: True,
        claim=claim,
        source_event_ids=("$source",),
        room_id=_target().room_id,
    )
    if cancel_before_start:
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0)

    assert held.is_set() is not cancel_before_start
    assert claim_live_at_retry == [False]


@pytest.mark.asyncio
async def test_inbox_response_refused_at_shutdown_releases_its_claim() -> None:
    """A response refused because shutdown began never runs, so the claim handed to it goes back at once."""
    runtime = BotRuntimeState(orchestrator=MagicMock())
    inbox = _inbox(runtime=runtime)
    runtime.begin_process_shutdown()
    claim = live_turn_claim("$source")
    response = asyncio.sleep(0)

    with pytest.raises(ResponseAdmissionRefusedError):
        inbox.track_inbox_response(
            response,
            name="inbox_response:$source",
            recovery_proof_ready=lambda: True,
            claim=claim,
            room_id=_target().room_id,
        )

    assert not turn_claim_is_live(claim)


@pytest.mark.asyncio
async def test_response_typing_supplies_complete_attribution(monkeypatch: pytest.MonkeyPatch) -> None:
    """Response typing should supply every stable attribution field it owns."""
    client = AsyncMock()
    request = replace(
        _plain_request(_target(thread_id="$thread:example.org")),
        correlation_id="$correlation:example.org",
    )
    logger = MagicMock()
    monkeypatch.setattr(typing_module, "logger", logger)

    async with response_typing_indicator(
        client,
        request,
        response_run_id="response-run-1",
        quiet=False,
    ):
        pass

    typing_logs = [call.kwargs for call in logger.debug.call_args_list if call.args == ("Set typing status",)]
    expected_attribution = {
        "agent_id": "general",
        "requester_id": "@user:localhost",
        "room_id": "!room:localhost",
        "thread_id": "$thread:example.org",
        "session_id": "!room:localhost:$thread:example.org",
        "reply_to_event_id": "$event",
        "correlation_id": "$correlation:example.org",
        "response_run_id": "response-run-1",
    }
    assert [entry["typing"] for entry in typing_logs] == [True, False]
    for entry in typing_logs:
        assert {key: entry[key] for key in expected_attribution} == expected_attribution


@contextmanager
def _through_the_gateway(runner: ResponseRunner) -> Iterator[tuple[AsyncMock, AsyncMock]]:
    """Record the edits and sends the runner makes, which still go through the real gateway to a patched Matrix."""
    gateway = unwrap_extracted_collaborator(runner.deps.delivery_gateway)

    async def transport(_client: object, _room_id: str, content: dict[str, Any], **_kwargs: object) -> object:
        replaced = content.get("m.relates_to", {}).get("event_id")
        return DeliveredMatrixEvent(replaced or "$sent", content)

    edit_text = AsyncMock(wraps=gateway.edit_text)
    send_text = AsyncMock(wraps=gateway.send_text)
    with (
        patch.object(DeliveryGateway, "edit_text", new=edit_text),
        patch.object(DeliveryGateway, "send_text", new=send_text),
        patch("mindroom.delivery_gateway.send_message_outcome", AsyncMock(side_effect=transport)),
    ):
        yield edit_text, send_text


@asynccontextmanager
async def _in_reply_span(
    runner: ResponseRunner,
    request: ResponseRequest,
    *,
    placeholder_event_id: str | None = "$thinking",
    show_tool_calls: bool = True,
) -> AsyncIterator[tuple[AsyncMock, AsyncMock]]:
    """Run the block in the request's reply span, recording the reply writes it sends through the real gateway."""
    with _through_the_gateway(runner) as sent:
        async with response_span(
            runner,
            request,
            placeholder_event_id=placeholder_event_id,
            show_tool_calls=show_tool_calls,
        ):
            yield sent


@asynccontextmanager
async def _ended_with_its_note(runner: ResponseRunner, request: ResponseRequest) -> AsyncIterator[None]:
    """Run the block in the request's reply span after that span ended with its interruption note recorded."""
    async with response_span(runner, request, placeholder_event_id="$response") as handle:
        await handle.runtime.decide(
            handle,
            lambda reply, span: rl.suppress(reply, span, reason="hook_failed", now_ns=1),
        )
        assert handle.span.outcome is rl.SpanOutcome.FAILED
        yield


async def _begin_locked_turn_in_span(
    runner: ResponseRunner,
    request: ResponseRequest,
    *,
    resolved_target: MessageTarget,
) -> ResponseRequest | SourceFate:
    """Admit and prepare ``request`` inside a span scope, as its locked lifecycle does."""
    async with runner.deps.replies.span_scope():
        return await runner._begin_locked_turn(
            request,
            resolved_target=resolved_target,
            history_scope=runner.deps.state_writer.history_scope(),
        )


async def _paused_reply_state(store: PrincipalStore) -> rl.ReplyState:
    """Return the state of the paused reply ``$waiting``, which answers its approval's turn."""
    reply = await store.replies.for_event("$waiting")
    assert reply is not None
    return reply.state


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


def _ordered_pause(paused: PausedAttempt) -> PausedAttempt:
    """Build the valid renderer-owned input expected by approval coordination tests."""
    body = ""
    trace: list[ToolTraceEntry] = []
    for index, tool in enumerate(paused.tools, start=1):
        marker, entry = format_tool_started_event(tool.call, tool_index=index)
        assert entry is not None
        body += marker
        trace.append(entry)
    return replace(paused, response_text=body.strip(), tool_trace=tuple(trace))


@pytest.mark.asyncio
async def test_repeated_inbox_drains_keep_failed_recovery_proof_fail_closed() -> None:
    """Later recoverable and empty drains must not erase an earlier unsafe cancellation."""
    inbox = _inbox()
    response_started = asyncio.Event()

    async def interrupted_response() -> None:
        response_started.set()
        await asyncio.Event().wait()

    response_task = inbox.track_inbox_response(
        interrupted_response(),
        name="test_unrecoverable_interrupted_response",
        recovery_proof_ready=lambda: False,
        room_id=_target().room_id,
    )
    await response_started.wait()

    assert await inbox.drain_inbox_responses(cancel_after_seconds=0) is False
    assert inbox.incomplete_inbox_responses_recoverable is False
    await asyncio.gather(response_task, return_exceptions=True)
    await asyncio.sleep(0)

    recoverable_response_started = asyncio.Event()

    async def recoverable_interrupted_response() -> None:
        recoverable_response_started.set()
        await asyncio.Event().wait()

    recoverable_response_task = inbox.track_inbox_response(
        recoverable_interrupted_response(),
        name="test_recoverable_interrupted_response",
        recovery_proof_ready=lambda: True,
        room_id=_target().room_id,
    )
    await recoverable_response_started.wait()

    assert await inbox.drain_inbox_responses(cancel_after_seconds=0.01) is False
    assert inbox.incomplete_inbox_responses_recoverable is False
    await asyncio.gather(recoverable_response_task, return_exceptions=True)
    await asyncio.sleep(0)

    assert await inbox.drain_inbox_responses(cancel_after_seconds=0) is True
    assert inbox.incomplete_inbox_responses_recoverable is False


@pytest.mark.asyncio
async def test_process_shutdown_accepts_clean_response_without_recovery_proof() -> None:
    """A clean terminal response has completed its contract without recovery."""
    inbox = _inbox()
    response_started = asyncio.Event()
    recovery_proof_checked = False

    async def response_finishes_cleanly() -> None:
        response_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return

    def invalid_recovery_proof() -> bool:
        nonlocal recovery_proof_checked
        recovery_proof_checked = True
        msg = "clean completion must not consult recovery"
        raise RuntimeError(msg)

    response_task = inbox.track_inbox_response(
        response_finishes_cleanly(),
        name="test_clean_process_shutdown_response",
        recovery_proof_ready=invalid_recovery_proof,
        room_id=_target().room_id,
    )
    await response_started.wait()
    inbox.begin_process_shutdown()
    await response_task

    assert (
        await inbox.drain_inbox_responses(
            cancel_after_seconds=0.01,
            shutdown_intent=ORDERLY_SHUTDOWN,
        )
        is True
    )
    assert recovery_proof_checked is False


@pytest.mark.asyncio
async def test_process_shutdown_reports_terminal_durable_response_as_drained() -> None:
    """A terminal process-cancelled response with durable recovery proof is drained."""
    inbox = _inbox()
    response_started = asyncio.Event()
    recovery_proof_checked = asyncio.Event()

    async def interrupted_response() -> None:
        response_started.set()
        await asyncio.Event().wait()

    async def durable_recovery_ready() -> bool:
        recovery_proof_checked.set()
        return True

    response_task = inbox.track_inbox_response(
        interrupted_response(),
        name="test_durable_process_shutdown_response",
        recovery_proof_ready=durable_recovery_ready,
        room_id=_target().room_id,
    )
    await response_started.wait()
    inbox.begin_process_shutdown()

    assert (
        await inbox.drain_inbox_responses(
            cancel_after_seconds=0.01,
            shutdown_intent=ORDERLY_SHUTDOWN,
        )
        is True
    )
    assert recovery_proof_checked.is_set()
    assert inbox.incomplete_inbox_responses_recoverable is True
    assert response_task.done()


@pytest.mark.asyncio
async def test_process_shutdown_waits_for_durable_proof_to_become_ready() -> None:
    """A terminal task may commit its recovery handoff just after cancellation."""
    inbox = _inbox()
    response_started = asyncio.Event()
    proof_attempts = 0

    async def interrupted_response() -> None:
        response_started.set()
        await asyncio.Event().wait()

    async def eventually_durable_recovery() -> bool:
        nonlocal proof_attempts
        proof_attempts += 1
        await asyncio.sleep(0)
        return proof_attempts >= 3

    response_task = inbox.track_inbox_response(
        interrupted_response(),
        name="test_eventually_durable_process_response",
        recovery_proof_ready=eventually_durable_recovery,
        room_id=_target().room_id,
    )
    await response_started.wait()
    inbox.begin_process_shutdown()

    assert await inbox.drain_inbox_responses(
        cancel_after_seconds=0.05,
        shutdown_intent=ORDERLY_SHUTDOWN,
    )
    assert proof_attempts == 3
    assert inbox.incomplete_inbox_responses_recoverable
    assert response_task.done()


@pytest.mark.asyncio
async def test_process_shutdown_retries_cancellation_within_bounded_cleanup() -> None:
    """Finite cancellation-resistant cleanup must not survive the shutdown budget."""
    inbox = _inbox()
    response_started = asyncio.Event()
    release_response = asyncio.Event()
    cancellation_count = 0

    async def layered_cleanup() -> None:
        nonlocal cancellation_count
        response_started.set()
        while cancellation_count < 5 and not release_response.is_set():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancellation_count += 1

    response_task = inbox.track_inbox_response(
        layered_cleanup(),
        name="test_layered_process_shutdown_cleanup",
        recovery_proof_ready=lambda: True,
        room_id=_target().room_id,
    )
    await response_started.wait()
    inbox.begin_process_shutdown()

    try:
        assert (
            await inbox.drain_inbox_responses(
                cancel_after_seconds=0.1,
                shutdown_intent=ORDERLY_SHUTDOWN,
            )
            is True
        )
    finally:
        release_response.set()
        if not response_task.done():
            response_task.cancel()
        await asyncio.gather(response_task, return_exceptions=True)

    assert cancellation_count == 5
    assert inbox.pending_inbox_response_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("shutdown_intent", "handed_off"),
    [(ORDERLY_SHUTDOWN, True), (SYNC_RESTART_SHUTDOWN, True), (ENTITY_REMOVED_SHUTDOWN, False)],
    ids=["process_shutdown", "entity_replacement", "entity_removal"],
)
async def test_a_successor_runtime_inherits_cancelled_responses(
    shutdown_intent: RuntimeShutdownIntent,
    handed_off: bool,
) -> None:
    """A stopping process or a replaced entity leaves its replies pending for the runtime that replays them."""
    inbox = _inbox()
    response_started = asyncio.Event()
    observed: list[bool] = []

    async def response() -> None:
        response_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            observed.append(current_task_is_process_shutdown())
            raise

    inbox.track_inbox_response(
        response(),
        name="test_inherited_response",
        recovery_proof_ready=lambda: True,
        room_id=_target().room_id,
    )
    await response_started.wait()

    await inbox.drain_inbox_responses(cancel_after_seconds=0.05, shutdown_intent=shutdown_intent)

    assert observed == [handed_off]


@pytest.mark.asyncio
async def test_process_shutdown_keeps_indefinitely_resistant_response_owned() -> None:
    """Repeated process cancellation must remain bounded and fail closed."""
    inbox = _inbox()
    response_started = asyncio.Event()
    release_response = asyncio.Event()
    cancellation_count = 0

    async def indefinitely_resistant_response() -> None:
        nonlocal cancellation_count
        response_started.set()
        while True:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancellation_count += 1
                if release_response.is_set():
                    raise

    response_task = inbox.track_inbox_response(
        indefinitely_resistant_response(),
        name="test_indefinitely_resistant_process_response",
        recovery_proof_ready=lambda: True,
        room_id=_target().room_id,
    )
    await response_started.wait()
    inbox.begin_process_shutdown()

    try:
        with pytest.raises(ResponseShutdownTimeoutError):
            await asyncio.wait_for(
                inbox.drain_inbox_responses(
                    cancel_after_seconds=0.05,
                    shutdown_intent=ORDERLY_SHUTDOWN,
                ),
                timeout=0.2,
            )
        assert cancellation_count > 2
        assert not response_task.done()
        assert inbox.pending_inbox_response_count == 1
    finally:
        release_response.set()
        if not response_task.done():
            response_task.cancel()
        await asyncio.gather(response_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_process_shutdown_bounds_async_recovery_proof() -> None:
    """A stuck journal/outbox proof must not outlive the response-drain budget."""
    inbox = _inbox()
    response_started = asyncio.Event()
    proof_started = asyncio.Event()
    proof_cancelled = asyncio.Event()
    release_proof = asyncio.Event()

    async def interrupted_response() -> None:
        response_started.set()
        await asyncio.Event().wait()

    async def stuck_recovery_proof() -> bool:
        proof_started.set()
        try:
            await release_proof.wait()
        except asyncio.CancelledError:
            proof_cancelled.set()
            raise
        return True

    response_task = inbox.track_inbox_response(
        interrupted_response(),
        name="test_stuck_process_shutdown_recovery_proof",
        recovery_proof_ready=stuck_recovery_proof,
        room_id=_target().room_id,
    )
    await response_started.wait()
    inbox.begin_process_shutdown()

    try:
        with pytest.raises(
            ResponseShutdownTimeoutError,
            match="recovery proof",
        ):
            await asyncio.wait_for(
                inbox.drain_inbox_responses(
                    cancel_after_seconds=0.02,
                    shutdown_intent=ORDERLY_SHUTDOWN,
                ),
                timeout=0.2,
            )
        assert proof_started.is_set()
        await asyncio.wait_for(proof_cancelled.wait(), timeout=0.02)
        await asyncio.sleep(0)
        assert response_task.done()
        assert inbox.pending_inbox_response_count == 1
        assert inbox.pending_response_phase_counts == {"recovery_proof": 1}
    finally:
        release_proof.set()
        await asyncio.gather(response_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_process_shutdown_retains_cancellation_resistant_recovery_proof_owner() -> None:
    """The proof deadline returns while its still-live store reader stays owned."""
    inbox = _inbox()
    response_started = asyncio.Event()
    proof_started = asyncio.Event()
    proof_cancelled = asyncio.Event()
    release_proof = asyncio.Event()
    proof_finished = asyncio.Event()

    async def interrupted_response() -> None:
        response_started.set()
        await asyncio.Event().wait()

    async def cancellation_resistant_proof() -> bool:
        proof_started.set()
        try:
            while not release_proof.is_set():
                try:
                    await release_proof.wait()
                except asyncio.CancelledError:
                    proof_cancelled.set()
            return True
        finally:
            proof_finished.set()

    response_task = inbox.track_inbox_response(
        interrupted_response(),
        name="test_resistant_process_shutdown_recovery_proof",
        recovery_proof_ready=cancellation_resistant_proof,
        room_id=_target().room_id,
    )
    await response_started.wait()
    inbox.begin_process_shutdown()
    loop = asyncio.get_running_loop()
    started_at = loop.time()

    async def delayed_release() -> None:
        await asyncio.sleep(0.2)
        release_proof.set()

    release_task = asyncio.create_task(delayed_release())

    try:
        with pytest.raises(
            ResponseShutdownTimeoutError,
            match="recovery proof",
        ):
            await asyncio.wait_for(
                inbox.drain_inbox_responses(
                    cancel_after_seconds=0.01,
                    shutdown_intent=ORDERLY_SHUTDOWN,
                ),
                timeout=0.1,
            )
        assert loop.time() - started_at < 0.08
        assert proof_started.is_set()
        await asyncio.wait_for(proof_cancelled.wait(), timeout=0.02)
        assert inbox.pending_inbox_response_count == 1
    finally:
        release_proof.set()
        await asyncio.wait_for(proof_finished.wait(), timeout=0.1)
        await release_task
        await asyncio.sleep(0)
        await asyncio.gather(response_task, return_exceptions=True)

    assert await inbox.finish_process_shutdown_recovery(timeout_seconds=0)
    assert inbox.pending_inbox_response_count == 0


@pytest.mark.asyncio
async def test_process_shutdown_reuses_late_recovery_proof_on_cleanup_retry() -> None:
    """A proof finishing after its deadline remains the one retry consumes."""
    inbox = _inbox()
    response_started = asyncio.Event()
    proof_started = asyncio.Event()
    release_proof = asyncio.Event()
    proof_finished = asyncio.Event()
    proof_calls = 0

    async def interrupted_response() -> None:
        response_started.set()
        await asyncio.Event().wait()

    async def cancellation_resistant_proof() -> bool:
        nonlocal proof_calls
        proof_calls += 1
        proof_started.set()
        try:
            while not release_proof.is_set():
                with suppress(asyncio.CancelledError):
                    await release_proof.wait()
            return True
        finally:
            proof_finished.set()

    response_task = inbox.track_inbox_response(
        interrupted_response(),
        name="test_late_recovery_proof_reuse",
        recovery_proof_ready=cancellation_resistant_proof,
        room_id=_target().room_id,
    )
    await response_started.wait()
    inbox.begin_process_shutdown()

    with pytest.raises(
        ResponseShutdownTimeoutError,
        match="recovery proof",
    ):
        await inbox.drain_inbox_responses(
            cancel_after_seconds=0.01,
            shutdown_intent=ORDERLY_SHUTDOWN,
        )

    assert proof_started.is_set()
    assert inbox.pending_inbox_response_count == 1
    release_proof.set()
    await asyncio.wait_for(proof_finished.wait(), timeout=0.1)
    await asyncio.sleep(0)

    assert await inbox.drain_inbox_responses(
        cancel_after_seconds=0,
        shutdown_intent=ORDERLY_SHUTDOWN,
    )
    assert inbox.incomplete_inbox_responses_recoverable
    assert inbox.pending_inbox_response_count == 0
    assert proof_calls == 1
    await asyncio.gather(response_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_recovery_proofs_wait_until_every_response_is_terminal() -> None:
    """A live response must fail the gate before any durable proof can block."""
    inbox = _inbox()
    proof_started = asyncio.Event()
    release_proof = asyncio.Event()

    async def finished_response() -> None:
        return

    async def live_response() -> None:
        await asyncio.Event().wait()

    async def stuck_recovery_proof() -> bool:
        proof_started.set()
        await release_proof.wait()
        return True

    finished_task = asyncio.create_task(finished_response())
    live_task = inbox.track_inbox_response(
        live_response(),
        name="terminal_gate",
        recovery_proof_ready=stuck_recovery_proof,
        room_id=_target().room_id,
    )
    await finished_task

    try:
        assert (
            await inbox._recovery_proofs_are_ready(
                dict(inbox._inbox_response_tasks),
                deadline=asyncio.get_running_loop().time() + 0.02,
            )
            is False
        )
        assert not proof_started.is_set()
    finally:
        release_proof.set()
        live_task.cancel()
        await asyncio.gather(live_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_process_shutdown_drains_four_owned_response_lifecycles(
    tmp_path: Path,
) -> None:
    """Process stop must unwind every tracked response without settling it, so restart recovery still owns each."""
    coordinator = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    all_attempts_started = asyncio.Event()
    started_count = 0

    async def blocked_attempt(*_args: object, **_kwargs: object) -> AsyncIterator[str]:
        nonlocal started_count
        started_count += 1
        if started_count == 4:
            all_attempts_started.set()
        await asyncio.Event().wait()
        yield ""

    async def owned_response(index: int) -> None:
        await coordinator.generate_response(
            _plain_request(
                _target(thread_id=f"$thread-{index}"),
                source_event_id=f"$source-{index}",
            ),
        )

    with (
        patch.object(
            DeliveryGateway,
            "send_text",
            new=AsyncMock(return_value="$placeholder"),
        ),
        patch.object(
            LockedTurns,
            "_finalize_locked_outcome",
            wraps=coordinator.locked._finalize_locked_outcome,
        ) as settle,
        patch_response_runner_module(
            stream_agent_response=blocked_attempt,
            should_use_streaming=AsyncMock(return_value=False),
            apply_post_response_effects=AsyncMock(),
        ),
    ):
        tasks = [
            coordinator.inbox.track_inbox_response(
                owned_response(index),
                name=f"test_process_shutdown_response_{index}",
                recovery_proof_ready=lambda: True,
                room_id=_target().room_id,
            )
            for index in range(4)
        ]
        # Each attempt opens its session store first, and concurrent opens of one state root queue on a polled lock.
        await asyncio.wait_for(all_attempts_started.wait(), timeout=SETTLE_TIMEOUT_SECONDS)
        coordinator.inbox.begin_process_shutdown()
        try:
            # The drain returns once the cancelled attempts unwind. One that kept running would exhaust both windows
            # and raise; the outer bound stays above them, so it only reports a drain that ignores its windows.
            assert (
                await asyncio.wait_for(
                    coordinator.inbox.drain_inbox_responses(cancel_after_seconds=2.0),
                    timeout=SETTLE_TIMEOUT_SECONDS,
                )
                is True
            )
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(0)

    settle.assert_not_called()
    assert coordinator.inbox.pending_inbox_response_count == 0
    assert not coordinator.inbox._inbox_response_tasks


@pytest.mark.asyncio
async def test_process_shutdown_starts_terminal_proof_while_peer_unwinds() -> None:
    """A terminal response uses the shared cleanup window to prove its handoff."""
    inbox = _inbox()
    fast_started = asyncio.Event()
    slow_started = asyncio.Event()
    slow_cancelled = asyncio.Event()
    release_slow = asyncio.Event()
    proof_started = asyncio.Event()
    release_proof = asyncio.Event()

    async def fast_response() -> None:
        fast_started.set()
        await asyncio.Event().wait()

    async def slow_response() -> None:
        slow_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            slow_cancelled.set()
            await release_slow.wait()
            raise

    async def fast_proof() -> bool:
        proof_started.set()
        await release_proof.wait()
        return True

    tasks = [
        inbox.track_inbox_response(
            fast_response(),
            name="test_fast_terminal_response",
            recovery_proof_ready=fast_proof,
            room_id=_target().room_id,
        ),
        inbox.track_inbox_response(
            slow_response(),
            name="test_slow_unwinding_response",
            recovery_proof_ready=lambda: True,
            room_id=_target().room_id,
        ),
    ]
    await fast_started.wait()
    await slow_started.wait()
    inbox.begin_process_shutdown()
    draining = asyncio.create_task(
        inbox.drain_inbox_responses(
            cancel_after_seconds=0.1,
            shutdown_intent=ORDERLY_SHUTDOWN,
        ),
    )

    await slow_cancelled.wait()
    await asyncio.wait_for(proof_started.wait(), timeout=0.02)
    assert not draining.done()

    release_proof.set()
    release_slow.set()
    assert await draining
    await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_failed_detached_inbox_response_returns_sources_to_retry_owner() -> None:
    """A post-handoff failure hands the task's sources straight back to their retry owner."""
    retry_sources = MagicMock()
    replies = MagicMock()
    inbox = _inbox(replies=replies, retry_approval_sources=retry_sources)
    replies.waits_to_claim.return_value = False

    async def fail_after_handoff() -> None:
        msg = "delivery failed"
        raise RuntimeError(msg)

    response_task = inbox.track_inbox_response(
        fail_after_handoff(),
        name="test_failed_detached_inbox_response",
        recovery_proof_ready=lambda: False,
        room_id=_target().room_id,
        source_event_ids=("$event",),
    )

    await asyncio.gather(response_task, return_exceptions=True)
    await asyncio.sleep(0)

    retry_sources.assert_called_once_with(_target().room_id, ("$event",))


@pytest.mark.asyncio
async def test_detached_inbox_response_owns_source_until_task_finishes() -> None:
    """Journal replay must not reclaim a source while its response task is alive."""
    retry_sources = MagicMock()
    replies = MagicMock()
    inbox = _inbox(replies=replies, retry_approval_sources=retry_sources)
    replies.waits_to_claim.return_value = False
    response_started = asyncio.Event()
    release_response = asyncio.Event()

    async def parked_response() -> None:
        response_started.set()
        await release_response.wait()

    response_task = inbox.track_inbox_response(
        parked_response(),
        name="test_source_owned_inbox_response",
        recovery_proof_ready=lambda: False,
        source_event_ids=("$reaction",),
        room_id=_target().room_id,
    )

    assert inbox.has_live_inbox_response("$reaction")
    await response_started.wait()
    release_response.set()
    await response_task
    await asyncio.sleep(0)

    assert not inbox.has_live_inbox_response("$reaction")
    retry_sources.assert_called_once_with(_target().room_id, ("$reaction",))


@pytest.mark.asyncio
async def test_an_inbox_response_whose_claim_waits_does_not_retry_its_sources_as_it_ends() -> None:
    """A deferred claim's wake retries its sources; the task's end does not retry them at once in a loop."""
    retry_sources = MagicMock()
    replies = MagicMock()
    inbox = _inbox(replies=replies, retry_approval_sources=retry_sources)
    replies.waits_to_claim.return_value = True

    async def deferred_response() -> None:
        return None

    response_task = inbox.track_inbox_response(
        deferred_response(),
        name="test_deferred_inbox_response",
        recovery_proof_ready=lambda: True,
        source_event_ids=("$edit",),
        room_id=_target().room_id,
    )
    await response_task
    await asyncio.sleep(0)

    retry_sources.assert_not_called()
    replies.waits_to_claim.assert_called_once_with(frozenset({"$edit"}))


def _attempt_runner() -> ResponseAttemptRunner:
    return ResponseAttemptRunner(
        ResponseAttemptDeps(
            client=make_matrix_client_mock(),
            logger=get_logger("tests.response_attempt"),
            show_stop_button=lambda: False,
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

    async def fake_stream_agent_response(
        ctx: ResponseTurnContext,
        *_args: object,
        **_kwargs: object,
    ) -> AsyncIterator[str]:
        turn = 1 if ctx.reply_to_event_id == "$event1" else 2
        events.append(f"respond_start:{turn}")
        if turn == 1:
            first_turn_started.set()
            await gate.wait()
        events.append(f"respond_end:{turn}")
        yield "ok"

    def _request_for(turn: int) -> ResponseRequest:
        target = _target(thread_id="$thread", reply_to_event_id=f"$event{turn}")
        envelope = _envelope(target, source_event_id=f"$event{turn}")
        return ResponseRequest(
            sources=ResponseSources(
                pending_event_ids=(envelope.source_event_id,),
                logical_source_event_ids=(envelope.source_event_id,),
            ),
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
        patch.object(LockedTurns, "_run_cancellable_response", new=attempt_in_place()),
        patch.object(DeliveryGateway, "deliver_final", new=AsyncMock(return_value=completed_outcome())),
        patch_response_runner_module(
            stream_agent_response=fake_stream_agent_response,
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
        assert await asyncio.wait_for(first, timeout=2) == Owned()
        assert await asyncio.wait_for(second, timeout=2) == Owned()

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
@pytest.mark.usefixtures("enforce_turn_authorization")
@pytest.mark.parametrize("uncertain", [False, True])
async def test_queued_response_rechecks_room_membership_after_acquiring_lifecycle_lock(
    tmp_path: Path,
    uncertain: bool,
) -> None:
    """A room-backed grant revoked during lock wait must prevent model execution."""
    bot = _bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    config = runner.deps.runtime.config
    config.agents["general"].access = ResponderAccessConfig(members_of_rooms=["grant"])
    grant_room_id = "!grant:localhost"
    state = MatrixState.load(runtime_paths=runner.deps.runtime_paths)
    state.add_room("grant", grant_room_id, "#grant:localhost", "Grant")
    save_matrix_state(state, runner.deps.runtime_paths)
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

    async def fake_stream_agent_response(
        ctx: ResponseTurnContext,
        *_args: object,
        **_kwargs: object,
    ) -> AsyncIterator[str]:
        assert ctx.reply_to_event_id is not None
        model_sources.append(ctx.reply_to_event_id)
        if ctx.reply_to_event_id == "$first":
            first_started.set()
            await release_first.wait()
        yield "ok"

    first_request = _plain_request(_target(thread_id="$thread", reply_to_event_id="$first"), source_event_id="$first")
    second_request = _plain_request(
        _target(thread_id="$thread", reply_to_event_id="$second"),
        source_event_id="$second",
    )
    with (
        patch(
            "mindroom.delivery_gateway.DeliveryGateway.send_text",
            new=AsyncMock(return_value="$placeholder"),
        ),
        patch.object(LockedTurns, "_run_cancellable_response", new=attempt_in_place()),
        patch.object(DeliveryGateway, "deliver_final", new=AsyncMock(return_value=completed_outcome())),
        patch_response_runner_module(
            stream_agent_response=fake_stream_agent_response,
            should_use_streaming=AsyncMock(return_value=False),
            apply_post_response_effects=AsyncMock(),
        ),
    ):
        first = asyncio.create_task(runner.generate_response(first_request))
        await asyncio.wait_for(first_started.wait(), timeout=2)
        assert await runner.locked.request_remains_authorized(second_request)
        second = asyncio.create_task(runner.generate_response(second_request))
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
        if uncertain:
            memberships.invalidate(config, reason="uncertain_sync_response")
        release_first.set()

        assert await asyncio.wait_for(first, timeout=2) == Owned()
        if uncertain:
            with pytest.raises(ReplyMembershipPendingError):
                await asyncio.wait_for(second, timeout=2)
            assert model_sources == ["$first"]
            await memberships.refresh(config, runner.deps.runtime_paths, membership_client)
            assert await runner.generate_response(second_request) == Owned()
        else:
            # The revoked request leaves its source for its caller to settle.
            assert await asyncio.wait_for(second, timeout=2) == Unclaimed(UnclaimedReason.REVOKED)

    assert model_sources == (["$first", "$second"] if uncertain else ["$first"])


@pytest.mark.asyncio
async def test_source_redacted_before_response_registration_is_left_unclaimed_without_side_effects(
    tmp_path: Path,
) -> None:
    """A durable tombstone observed under the lock must prevent every persistence side effect."""
    bot = _bot(tmp_path)
    target = _target(thread_id="$thread", reply_to_event_id="$event")
    envelope = _envelope(target, source_event_id="$event")
    delivery_gateway = MagicMock(spec=DeliveryGateway)
    delivery_gateway.send_text = AsyncMock(return_value="$placeholder")
    # No earlier attempt left a reply, so no reply's records settle the sources.
    delivery_gateway.settle_unclaimed_reply = AsyncMock(return_value=False)
    request_preparer = MagicMock(spec=ResponsePayloadPreparer)
    request_preparer.prepare = AsyncMock()
    runner = runner_for(
        replace(
            unwrap_extracted_collaborator(bot._response_runner).deps,
            delivery_gateway=delivery_gateway,
            request_preparer=request_preparer,
        ),
    )
    preparations = 0
    redacted = SourcePreparation(invalid=(envelope.source_event_id,))

    async def prepare_source_turn(_history: object) -> SourcePreparation:
        nonlocal preparations
        preparations += 1
        return redacted

    request = ResponseRequest(
        sources=ResponseSources(
            pending_event_ids=(envelope.source_event_id,),
            logical_source_event_ids=(envelope.source_event_id,),
        ),
        thread_history=[],
        prompt="REDACTED_SECRET",
        user_id="@user:localhost",
        response_envelope=envelope,
        payload_preparation=_preparation(target, envelope),
        prepare_source_turn=prepare_source_turn,
    )

    # The redacted source is left to the request's caller, which settles it.
    assert await runner.generate_response(request) == Unclaimed(UnclaimedReason.TERMINAL_SOURCES, redacted)
    assert preparations == 1
    delivery_gateway.send_text.assert_not_awaited()
    request_preparer.prepare.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("final_result", [False, True])
async def test_final_source_gate_uses_refreshed_history_without_repeating_lifecycle_hooks(
    tmp_path: Path,
    final_result: bool,
) -> None:
    """The final owner gate sees refreshed history and suppresses the turn when its source ended."""
    bot = _bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    target = _target(thread_id="$thread", reply_to_event_id="$event")
    history = ThreadHistoryResult(
        [make_visible_message(event_id="$context", body="refreshed")],
        is_full_history=True,
    )
    runner.deps.resolver.fetch_thread_history = AsyncMock(return_value=history)
    prepare = AsyncMock(
        side_effect=[SourcePreparation(), SourcePreparation(invalid=("$event",) if final_result else ())],
    )
    acquired = MagicMock()
    request = ResponseRequest(
        sources=ResponseSources(
            pending_event_ids=("$event",),
            logical_source_event_ids=("$event",),
        ),
        thread_history=[],
        prompt="hello",
        user_id="@user:localhost",
        response_envelope=_envelope(target, source_event_id="$event"),
        prepare_source_turn=prepare,
        on_lifecycle_lock_acquired=acquired,
    )
    prepared = await _begin_locked_turn_in_span(runner, request, resolved_target=target)
    assert [item.args[0] for item in prepare.await_args_list] == [[], history]
    acquired.assert_called_once_with()
    if final_result is True:
        # The claimed reply's records settle the source that ended.
        assert prepared == Owned()
    else:
        assert isinstance(prepared, ResponseRequest)


@pytest.mark.asyncio
async def test_begin_locked_turn_waits_for_cancelled_source_preparation(tmp_path: Path) -> None:
    """Cancellation must not release the lifecycle lock while cleanup still mutates storage."""
    bot = _bot(tmp_path)
    target = _target(thread_id="$thread", reply_to_event_id="$event")
    runner = unwrap_extracted_collaborator(bot._response_runner)
    preparation_started = asyncio.Event()
    allow_preparation_finish = asyncio.Event()

    async def prepare_source_turn(_history: object) -> SourcePreparation:
        preparation_started.set()
        await allow_preparation_finish.wait()
        return SourcePreparation()

    request = ResponseRequest(
        sources=ResponseSources(
            pending_event_ids=("$event",),
            logical_source_event_ids=("$event",),
        ),
        thread_history=[],
        prompt="prompt",
        user_id="@user:localhost",
        response_envelope=_envelope(target, source_event_id="$event"),
        prepare_source_turn=prepare_source_turn,
    )
    preparation_task = asyncio.create_task(
        _begin_locked_turn_in_span(runner, request, resolved_target=target),
    )
    await asyncio.wait_for(preparation_started.wait(), timeout=2)

    request_task_cancel(preparation_task, cancel_source="sync_restart")
    await asyncio.sleep(0)

    assert preparation_task.done() is False
    allow_preparation_finish.set()
    with pytest.raises(asyncio.CancelledError):
        await preparation_task


@pytest.mark.asyncio
async def test_failing_continuation_recovers_frozen_success_before_failure_settlement(tmp_path: Path) -> None:
    """A failure fence racing a generated answer cannot retire that answer as a denial."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    store = runner.deps.replies.store
    await _admit_approval_source(store)
    continuation = approval_continuation(
        approval_id="approval-failing-final",
    )
    assert await paused_for_approval(store, continuation) == continuation
    claimed = await claim_continuation(
        store,
        continuation.approval_id,
        runtime_generation=runner.deps.approval_runtime_generation,
    )
    assert claimed is not None
    failing = await store.approvals.request_approval_failure(
        claimed.approval_id,
        ApprovalFailure(rl.ApprovalFailureKind.ERROR, "entity removed"),
        expected_state="claimed",
        expected_generation=claimed.generation,
        expected_runtime_generation=claimed.runtime_generation,
    )
    assert failing is not None
    await freeze_resume_final(
        store,
        claimed,
        text="finished",
        payload={"body": "* finished", "m.new_content": {"body": "finished"}},
    )
    assert await store.claim_matrix_delivery(delivery_id="$source", stage=DeliveryStage.FINAL) is not None
    await store.acknowledge_matrix_delivery(
        delivery_id="$source",
        stage=DeliveryStage.FINAL,
        event_id="$final-edit",
        delivered_projections=(),
    )
    lifecycle = MagicMock(finalize=AsyncMock())

    with patch.object(LockedTurns, "build_lifecycle", return_value=lifecycle):
        await dispatch_approval(
            runner.approvals,
            failing,
            target=_target(thread_id="$thread", reply_to_event_id="$source"),
        )

    # The paused reply answers the turn with the recovered answer.
    reply = await store.replies.for_event("$waiting")
    assert reply is not None
    assert reply.state is rl.ReplyState.COMPLETED
    lifecycle.finalize.assert_awaited_once()
    assert await store.approvals.approval_continuation(continuation.approval_id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [None, ApprovalFailure(rl.ApprovalFailureKind.SHUTDOWN)],
    ids=["stale_claim", "handoff_fence"],
)
async def test_restart_interrupted_approval_hands_its_turn_back_to_replay(
    tmp_path: Path,
    failure: ApprovalFailure | None,
) -> None:
    """An approved run a restart cut short ends its cards and leaves its reply and source for replay to continue."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    store = runner.deps.replies.store
    await _admit_approval_source(store)
    continuation = approval_continuation(
        approval_id="approval-stale-claim",
    )
    assert await paused_for_approval(store, continuation) == continuation
    owned = await claim_continuation(
        store,
        continuation.approval_id,
        runtime_generation="previous-runtime",
    )
    assert owned is not None
    if failure is not None:
        owned = await store.approvals.request_approval_failure(
            continuation.approval_id,
            failure,
            expected_state="claimed",
            expected_runtime_generation="previous-runtime",
        )
        assert owned is not None
    edit_text = AsyncMock()
    expire_cards = AsyncMock(return_value=True)

    with (
        patch.object(DeliveryGateway, "edit_text", new=edit_text),
        patch(
            "mindroom.approval.response.approval_manager.get_approval_store",
            return_value=MagicMock(cards=None, expire_continuation_cards=expire_cards),
        ),
    ):
        await dispatch_approval(
            runner.approvals,
            owned,
            target=_target(thread_id="$thread", reply_to_event_id="$source"),
        )

    edit_text.assert_not_awaited()
    expire_cards.assert_awaited_once_with(
        continuation.approval_id,
        ApprovalFailure(rl.ApprovalFailureKind.SHUTDOWN).reason,
    )
    assert await store.approvals.approval_continuation(continuation.approval_id) is None
    assert await store.is_pending("$source")


@pytest.mark.asyncio
async def test_restart_hand_back_retries_cards_that_did_not_expire(tmp_path: Path) -> None:
    """A hand-back whose cards did not expire waits for the next pass instead of settling the reply."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    store = runner.deps.replies.store
    await _admit_approval_source(store)
    continuation = approval_continuation(
        approval_id="approval-retry-cards",
    )
    assert await paused_for_approval(store, continuation) == continuation
    claimed = await claim_continuation(store, continuation.approval_id, runtime_generation="previous-runtime")
    assert claimed is not None
    edit_text = AsyncMock()
    expire_cards = AsyncMock(side_effect=[False, True])
    target = _target(thread_id="$thread", reply_to_event_id="$source")

    with (
        patch.object(DeliveryGateway, "edit_text", new=edit_text),
        patch(
            "mindroom.approval.response.approval_manager.get_approval_store",
            return_value=MagicMock(cards=None, expire_continuation_cards=expire_cards),
        ),
    ):
        await dispatch_approval(runner.approvals, claimed, target=target)
        waiting = await store.approvals.approval_continuation(continuation.approval_id)
        assert waiting is not None
        await dispatch_approval(runner.approvals, waiting, target=target)

    assert waiting.state == "failing"
    edit_text.assert_not_awaited()
    assert await store.approvals.approval_continuation(continuation.approval_id) is None
    assert await store.is_pending("$source")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handed_off", "failure"),
    [
        (True, ApprovalFailure(rl.ApprovalFailureKind.SHUTDOWN)),
        (False, ApprovalFailure(rl.ApprovalFailureKind.RESTART)),
    ],
    ids=["handed_to_successor", "cancelled_in_place"],
)
async def test_cancelled_claimed_approval_records_whether_a_successor_takes_it(
    tmp_path: Path,
    handed_off: bool,
    failure: ApprovalFailure,
) -> None:
    """A run handed to a successor runtime is fenced as restart-interrupted; any other cancellation keeps its provenance."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    store = runner.deps.replies.store
    await _admit_approval_source(store)
    continuation = approval_continuation(
        approval_id="approval-restart-cancelled",
    )
    assert await paused_for_approval(store, continuation) == continuation
    claimed = await claim_continuation(
        store,
        continuation.approval_id,
        runtime_generation="current-runtime",
    )
    assert claimed is not None
    started = asyncio.Event()

    async def run_until_cancelled(*_args: object, **_kwargs: object) -> None:
        started.set()
        await asyncio.Event().wait()

    with (
        patch.object(runner.approvals, "_run_claimed_approval_lifecycle", new=run_until_cancelled),
    ):
        task = asyncio.create_task(
            runner.approvals._run_owned_approval_continuation(
                claimed,
                target=_target(thread_id="$thread", reply_to_event_id="$source"),
            ),
        )
        await started.wait()
        request_task_cancel(task, cancel_source="sync_restart", process_shutdown=handed_off)
        with pytest.raises(asyncio.CancelledError):
            await task

    failing = await store.approvals.approval_continuation(continuation.approval_id)
    assert failing is not None
    assert failing.state == "failing"
    assert failing.failure == failure


async def _settle_resume_that_ended(
    runner: ResponseRunner,
    continuation: ApprovalContinuation,
    ending: TurnEnding,
) -> None:
    """Run a claimed approval resume whose locked lifecycle settled with ``ending``, as the approval runner settles it."""

    async def settled(_self: LockedTurns, *_args: object, progress: TurnProgress, **_kwargs: object) -> None:
        progress.ending = ending

    with (
        patch.object(LockedTurns, "run_and_settle_locked_response", new=settled),
        patch.object(LockedTurns, "refresh_mid_turn_context_for_approval", new=AsyncMock()),
    ):
        await runner.approvals._run_claimed_approval_lifecycle(continuation, target=continuation_target(continuation))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ending", "failure"),
    [
        (Cancelled("user_stop"), ApprovalFailure(rl.ApprovalFailureKind.USER_STOP)),
        (Suppressed(Suppression.HOOK), ApprovalFailure(rl.ApprovalFailureKind.ERROR)),
    ],
)
async def test_claimed_approval_non_interruption_uses_ordinary_settlement(
    tmp_path: Path,
    ending: TurnEnding,
    failure: ApprovalFailure,
) -> None:
    """User stops and hook suppression must not be relabeled as interruptions."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    continuation = approval_continuation(state="claimed", runtime_generation=runner.deps.approval_runtime_generation)
    settle_failure = AsyncMock()

    with patch.object(runner.approvals, "_settle", new=settle_failure):
        await _settle_resume_that_ended(runner, continuation, ending)

    settle_failure.assert_awaited_once_with(continuation, failure)


def test_a_replay_account_lists_finished_and_running_tools_without_team_chrome(tmp_path: Path) -> None:
    """The replayed turn learns which tools of the stopped attempt finished and which may still have been running."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    finished = ToolTraceEntry(type="tool_call_completed", tool_name="counter", args_preview="{}", result_preview="1")
    running = ToolTraceEntry(type="tool_call_started", tool_name="report", args_preview='{"pages": 3}')
    shown = "🤝 **Team Response** (General, Helper):\n\nHalf of the report\n\n\n*No team consensus - showing individual responses only*"
    handle = SimpleNamespace(
        span=SimpleNamespace(kind=rl.SpanKind.REPLAY),
        resumed=UnfinishedStreamedReply(visible_text=shown, tool_trace=()),
    )

    replayed = runner._with_recorded_interrupted_attempt(
        _plain_request(_target(), source_event_id="$source"),
        cast("SpanHandle", handle),
        (finished, running),
    )

    account = html.unescape(replayed.model_prompt or "")
    assert "Half of the report\n\n(turn stopped before completion; 1 tool call(s) had finished; " in account
    assert 'The `counter` tool finished with input preview "{}" and output preview "1".' in account
    assert 'The `report` tool was still running with input preview "{\\"pages\\": 3}"' in account
    assert "Team Response" not in account
    assert "No team consensus" not in account
    assert "their effects stand: do not call a finished tool with side effects again" in account


@pytest.mark.asyncio
async def test_claimed_approval_generic_interruption_keeps_generic_marker(tmp_path: Path) -> None:
    """A generic cancellation preserves its provenance instead of claiming a restart."""
    runner_bot = _bot(tmp_path)
    runner = unwrap_extracted_collaborator(runner_bot._response_runner)
    store = runner.deps.replies.store
    await _admit_approval_source(store)
    continuation = approval_continuation(
        approval_id="approval-interrupted",
    )
    # An approved run's paused reply; this instance resumes it and stops while resuming it.
    assert await paused_for_approval(store, continuation, text="committed partial") is not None
    async with resumed_approval(runner_bot, continuation):
        pass
    # The next instance takes the replies over and settles the interrupted resume.
    bot = _bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    store = runner.deps.replies.store
    await bot._take_over_replies()
    claimed = await store.approvals.approval_continuation(continuation.approval_id)
    assert claimed is not None
    unique_room_send_responses(bot.client)
    with patch(
        "mindroom.approval.response.approval_manager.get_approval_store",
        return_value=MagicMock(cards=None, expire_continuation_cards=AsyncMock(return_value=True)),
    ):
        await _settle_resume_that_ended(runner, claimed, Cancelled("interrupted"))

    assert await store.approvals.approval_continuation(continuation.approval_id) is None
    note = bot.client.room_send.await_args_list[-1].kwargs["content"]["m.new_content"]
    assert note["body"] == f"committed partial\n\n{INTERRUPTED_RESPONSE_NOTE}"
    assert note[STREAM_STATUS_KEY] == STREAM_STATUS_ERROR


@pytest.mark.parametrize(
    ("outcome", "ending"),
    [
        (rl.SpanOutcome.COMPLETED, Completed()),
        (rl.SpanOutcome.CANCELLED, Cancelled("user_stop")),
        (rl.SpanOutcome.FAILED, Failed(FailureCause.ERROR)),
    ],
)
@pytest.mark.asyncio
async def test_a_frozen_approval_final_reports_how_its_span_ended(
    tmp_path: Path,
    outcome: rl.SpanOutcome,
    ending: TurnEnding,
) -> None:
    """A FINAL its resume wrote as a failure or cancellation is recovered as one, never as a completed answer."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    store = runner.deps.replies.store
    await _admit_approval_source(store)
    await store.enqueue_matrix_delivery(
        delivery_id="$source",
        stage=DeliveryStage.FINAL,
        room_id="!room:localhost",
        thread_id="$thread",
        payload={"body": "* note", "m.new_content": {"body": "note"}},
        edits_event_id="$waiting",
    )
    await store.claim_matrix_delivery(delivery_id="$source", stage=DeliveryStage.FINAL)
    await store.acknowledge_matrix_delivery(
        delivery_id="$source",
        stage=DeliveryStage.FINAL,
        event_id="$final",
        delivered_projections=(),
    )
    frozen = await store.load_matrix_delivery(delivery_id="$source", stage=DeliveryStage.FINAL)
    assert frozen is not None
    reply = MagicMock(presentation=encode_presentation(Presentation(segments=(Segment(kind="answer", text="note"),))))
    reply.possibly_shown = None
    with (
        patch.object(ReplyStore, "span", AsyncMock(return_value=MagicMock(outcome=outcome))),
        patch.object(ReplyStore, "load", AsyncMock(return_value=reply)),
    ):
        restored = await runner.approvals._approval_outcome_from_delivery(
            replace(frozen, span_id="span-resume", reply_id="reply-1"),
        )
    assert restored.ending == ending


@pytest.mark.asyncio
async def test_a_recovered_approval_answer_reports_what_its_final_showed(tmp_path: Path) -> None:
    """A final transform reshaped the answer the FINAL showed; recovered after-response work sees that, not the raw answer."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    store = runner.deps.replies.store
    await _admit_approval_source(store)
    await store.enqueue_matrix_delivery(
        delivery_id="$source",
        stage=DeliveryStage.FINAL,
        room_id="!room:localhost",
        thread_id="$thread",
        payload={"body": "* transformed", "m.new_content": {"body": "transformed"}},
        edits_event_id="$waiting",
    )
    await store.claim_matrix_delivery(delivery_id="$source", stage=DeliveryStage.FINAL)
    await store.acknowledge_matrix_delivery(
        delivery_id="$source",
        stage=DeliveryStage.FINAL,
        event_id="$final",
        delivered_projections=(),
    )
    frozen = await store.load_matrix_delivery(delivery_id="$source", stage=DeliveryStage.FINAL)
    assert frozen is not None
    reply = MagicMock(
        presentation=encode_presentation(Presentation(segments=(Segment(kind="answer", text="raw answer"),))),
        possibly_shown=encode_presentation(Presentation(segments=(Segment(kind="answer", text="transformed"),))),
    )
    with (
        patch.object(ReplyStore, "load", AsyncMock(return_value=reply)),
        patch.object(ReplyStore, "span", AsyncMock(return_value=MagicMock(outcome=rl.SpanOutcome.COMPLETED))),
    ):
        restored = await runner.approvals._approval_outcome_from_delivery(
            replace(frozen, reply_id="reply-1", span_id="span-resume"),
        )
    assert restored.final_visible_body == "transformed"


@pytest.mark.asyncio
async def test_recovery_fails_an_approval_whose_resume_already_failed(tmp_path: Path) -> None:
    """A crash between a resume's failure and its fence leaves nothing for a replay to continue: the approval fails."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    await _admit_approval_source(runner.deps.replies.store)
    claimed = approval_continuation(
        approval_id="approval-resume-failed",
        state="claimed",
        claim_span_id="span-resume",
    )
    settle_failure = AsyncMock(return_value=True)
    release_to_replay = AsyncMock(return_value=True)
    with (
        patch.object(ReplyStore, "span", AsyncMock(return_value=MagicMock(outcome=rl.SpanOutcome.FAILED))),
        patch.object(runner.approvals, "_settle", settle_failure),
        patch.object(runner.approvals, "_release_to_replay", release_to_replay),
    ):
        await dispatch_approval(
            runner.approvals,
            claimed,
            target=_target(thread_id="$thread", reply_to_event_id="$source"),
        )
    settle_failure.assert_awaited_once()
    release_to_replay.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True], ids=("error", "cancellation"))
async def test_final_recovery_error_fences_current_claim(tmp_path: Path, *, cancelled: bool) -> None:
    """A failed outbox read cannot hide a same-runtime claim until restart."""
    bot = _bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    store = runner.deps.replies.store
    await _admit_approval_source(store)
    continuation = approval_continuation(
        approval_id=f"approval-recovery-error-{cancelled}",
    )
    assert await paused_for_approval(store, continuation) is not None
    await bot._take_over_replies()
    failure = asyncio.CancelledError() if cancelled else RuntimeError("Agno continuation failed")

    with (
        patch.object(runner.approvals, "_run_claimed_approval_lifecycle", new=AsyncMock(side_effect=failure)),
        # The dispatch reads the ready continuation's final answer; the outbox fails once its resume ends.
        patch(
            "mindroom.approval.continuation_runner.read_final",
            new=AsyncMock(side_effect=[(None, None), RuntimeError("outbox temporarily unavailable")]),
        ),
        patch.object(runner.approvals, "_settle", new=AsyncMock(return_value=False)),
    ):
        if cancelled:
            with pytest.raises(asyncio.CancelledError):
                await runner._run_owned_or_locked_response(
                    _plain_request(_target(thread_id="$thread"), source_event_id="$source"),
                    target=_target(thread_id="$thread"),
                    locked_operation=AsyncMock(),
                )
        else:
            # The approval that owns the source keeps it.
            assert (
                await runner._run_owned_or_locked_response(
                    _plain_request(_target(thread_id="$thread"), source_event_id="$source"),
                    target=_target(thread_id="$thread"),
                    locked_operation=AsyncMock(),
                )
                == Owned()
            )

    retained = await store.approvals.approval_continuation(continuation.approval_id)
    assert retained is not None
    assert retained.state == "failing"


@pytest.mark.asyncio
async def test_claimed_request_preparation_excludes_early_placeholder_from_refreshed_history(tmp_path: Path) -> None:
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
    runner = runner_for(
        replace(
            unwrap_extracted_collaborator(bot._response_runner).deps,
            resolver=resolver,
            request_preparer=request_preparer,
            delivery_gateway=delivery_gateway,
        ),
    )
    request = ResponseRequest(
        sources=ResponseSources(
            pending_event_ids=(envelope.source_event_id,),
            logical_source_event_ids=(envelope.source_event_id,),
        ),
        thread_history=[],
        prompt="hello",
        user_id="@user:localhost",
        response_envelope=envelope,
        payload_preparation=_preparation(target, envelope),
    )

    await runner.deps.replies.take_ownership()
    # The locked turn claims its reply span in the scope every response opens.
    async with runner.deps.replies.span_scope():
        history_scope = runner.deps.state_writer.history_scope()
        admitted = await runner._admit_locked_turn(request, resolved_target=target, history_scope=history_scope)
        assert isinstance(admitted, ResponseRequest)
        placeholder_event_id = await runner._send_placeholder(
            admitted,
            resolved_target=target,
            history_scope=history_scope,
            execution_identity=runner.deps.tool_runtime.build_execution_identity(
                target=target,
                user_id=request.user_id,
            ),
            placeholder_message="Thinking...",
        )
        assert placeholder_event_id == "$placeholder"
        prepared_request = await runner._prepare_claimed_request(
            admitted,
            resolved_target=target,
            placeholder_event_id=placeholder_event_id,
        )

    assert isinstance(prepared_request, ResponseRequest)
    assert isinstance(prepared_request.thread_history, ThreadHistoryResult)
    assert [message.event_id for message in prepared_request.thread_history] == ["$history"]
    assert prepared_request.thread_history.is_full_history is True
    assert prepared_request.thread_history.diagnostics == {"cache_status": "fresh"}


@pytest.mark.asyncio
async def test_setup_cancellation_releases_reply_span(tmp_path: Path) -> None:
    """A setup cancellation propagates, and the reply's span is released for a retry instead of noted directly."""
    bot = _bot(tmp_path)
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    setup_started = asyncio.Event()

    async def blocked_streaming_check(*_args: object, **_kwargs: object) -> bool:
        setup_started.set()
        await asyncio.Event().wait()
        return False

    with (
        patch(
            "mindroom.delivery_gateway.DeliveryGateway.send_text",
            new=AsyncMock(return_value="$placeholder"),
        ),
        patch_response_runner_module(should_use_streaming=AsyncMock(side_effect=blocked_streaming_check)),
    ):
        response = asyncio.create_task(coordinator.generate_response(_plain_request(_target())))
        await asyncio.wait_for(setup_started.wait(), timeout=1.0)
        response.cancel("sync_restart")
        with pytest.raises(asyncio.CancelledError, match="sync_restart"):
            await response

    # The reply's records own the early placeholder.
    replies = bot._reply_runtime.store.replies
    reply = await replies.for_sources(("$event",))
    assert reply is not None
    assert reply.state is rl.ReplyState.ACTIVE
    spans = await replies.spans(reply.reply_id)
    assert [span.outcome for span in spans] == [rl.SpanOutcome.RELEASED]


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "error", "cancel"])
@pytest.mark.parametrize("is_team", [False, True])
async def test_lifecycle_identity_uses_canonical_requester_before_lock_wait(
    tmp_path: Path,
    outcome: str,
    is_team: bool,
) -> None:
    """Admitted lock waits retain canonical identity and release it on every exit."""
    bot = _bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    if is_team:
        bot.config.teams["helpers"] = TeamConfig(display_name="Helpers", role="Help users", agents=["general"])
        runner = runner_for(replace(runner.deps, agent_name="helpers"))
    gate = bot.orchestrator.response_admission_gate
    request = _plain_request(_target())
    envelope = request.response_envelope
    request = replace(
        request,
        response_envelope=replace(
            envelope,
            origin=replace(envelope.origin, requester_id="@alice:example.org"),
        ),
    )
    started = asyncio.Event()
    finish = asyncio.Event()

    async def blocked_lifecycle(**_kwargs: object) -> SourceFate:
        started.set()
        await finish.wait()
        if outcome == "error":
            message = "lifecycle failed"
            raise RuntimeError(message)
        return Owned()

    with patch.object(runner.lifecycle_coordinator, "run_locked_response", new=blocked_lifecycle):
        task = asyncio.create_task(
            runner._run_locked_response_lifecycle(
                request,
                response_kind="agent",
                locked_operation=AsyncMock(),
            ),
        )
        await asyncio.wait_for(started.wait(), timeout=1.0)
        try:
            assert gate.in_flight_response_count == 1
            (identity,) = gate.response_identities
            assert identity.responder == ("team/helpers" if is_team else "general")
            assert identity.requester_id == "@alice:example.org"
            assert request.user_id == "@user:localhost"
        finally:
            if outcome == "cancel":
                task.cancel()
            finish.set()
            if outcome == "error":
                with pytest.raises(RuntimeError, match="lifecycle failed"):
                    await task
            elif outcome == "cancel":
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                assert await task == Owned()
    assert gate.response_identities == set()
    assert gate.in_flight_response_count == 0


@pytest.mark.asyncio
async def test_preparation_failure_note_describes_the_proximate_error_not_its_cause(tmp_path: Path) -> None:
    """Only the preparation wrapper is unwrapped when linking an early placeholder failure."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    underlying_error = ValueError("underlying failure")
    proximate_error = RuntimeError("proximate setup failure")
    proximate_error.__cause__ = underlying_error

    friendly_error = MagicMock(return_value="setup failed")

    with (
        patch(
            "mindroom.delivery_gateway.DeliveryGateway.send_text",
            new=AsyncMock(return_value="$placeholder"),
        ),
        patch.object(runner, "_prepare_request_after_lock", new=AsyncMock(side_effect=proximate_error)),
        patch("mindroom.response_runner.get_user_friendly_error_message", new=friendly_error),
    ):
        # The reply's dispatch-failure note answers the turn.
        assert await runner.generate_response(_plain_request(_target())) == Owned()

    assert friendly_error.call_args.args[0] is proximate_error
    assert proximate_error.__cause__ is underlying_error


@pytest.mark.asyncio
async def test_silent_schedule_does_not_signal_queued_human_activity(tmp_path: Path) -> None:
    """A silent source must serialize without producing a queued-input notice."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    base_request = _plain_request(_target())
    request = replace(
        base_request,
        response_envelope=request_envelope(
            target=base_request.response_envelope.target,
            prompt=base_request.prompt,
            user_id=base_request.user_id,
            source_kind=SILENT_SCHEDULE_SOURCE_KIND,
        ),
    )
    run_locked_response = AsyncMock(return_value=Owned())

    with patch.object(runner.lifecycle_coordinator, "run_locked_response", new=run_locked_response):
        await runner._run_locked_response_lifecycle(
            request,
            response_kind="agent",
            locked_operation=AsyncMock(return_value=Owned()),
        )

    assert run_locked_response.await_args.kwargs["signal_queued_message"] is False


@pytest.mark.asyncio
async def test_replayed_source_adopts_journal_owned_approval_continuation(tmp_path: Path) -> None:
    """A crash after suspension persistence must not execute the inbound source a second time."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    request = _plain_request(_target(thread_id="$thread"), source_event_id="$source")
    await _admit_approval_source(runner.deps.replies.store)
    continuation = approval_continuation(
        approval_id="approval-replay",
        run_id="run-paused",
        room_id=request.room_id,
        thread_id=request.thread_id,
        calls=(
            ApprovalCall(
                tool_call_id="call-1",
                tool_name="dangerous",
                invoking_agent="general",
                expires_at_ns=9_000_000_000_000_000_000,
            ),
        ),
        execution_identity=ToolExecutionIdentity(
            channel="matrix",
            agent_name="general",
            requester_id="@user:localhost",
            room_id="!room:localhost",
            thread_id="$thread",
            resolved_thread_id="$thread",
            session_id="session-1",
        ),
        sources=ResponseSources(
            (request.response_envelope.source_event_id,),
            (request.response_envelope.source_event_id,),
        ),
        state="waiting",
    )
    assert await paused_for_approval(runner.deps.replies.store, continuation) == continuation
    locked_operation = AsyncMock(return_value=Unclaimed(UnclaimedReason.NOTHING_TO_RUN))

    # The approval keeps owning the source.
    assert (
        await runner._run_owned_or_locked_response(
            request,
            target=request.response_envelope.target,
            locked_operation=locked_operation,
        )
        == Owned()
    )
    locked_operation.assert_not_awaited()


@pytest.mark.asyncio
async def test_approval_resume_queued_behind_follow_up_does_not_signal_human_input(tmp_path: Path) -> None:
    """An internal resume must serialize without interrupting the active human turn."""
    bot = _bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    target = _target(thread_id="$thread", reply_to_event_id="$follow-up")
    await _admit_approval_source(runner.deps.replies.store)
    continuation = approval_continuation(
        approval_id="approval-resume",
        run_id="run-paused",
        room_id=target.room_id,
        thread_id=target.resolved_thread_id,
    )
    assert await paused_for_approval(runner.deps.replies.store, continuation) is not None
    await bot._take_over_replies()
    follow_up_started = asyncio.Event()
    release_follow_up = asyncio.Event()

    async def active_follow_up(_target: MessageTarget) -> SourceFate:
        follow_up_started.set()
        await release_follow_up.wait()
        return Owned()

    follow_up = asyncio.create_task(
        runner._run_locked_response_lifecycle(
            _plain_request(target, source_event_id="$follow-up"),
            response_kind="ai",
            locked_operation=active_follow_up,
        ),
    )
    await asyncio.wait_for(follow_up_started.wait(), timeout=1.0)
    lifecycle = runner.lifecycle_coordinator
    queued_signal = lifecycle._get_or_create_queued_signal(target)
    resume_entered = asyncio.Event()
    begin_response_turn_notice = lifecycle._begin_response_turn_notice
    run_approval_continuation = AsyncMock(return_value=False)

    def observe_response_turn_start(
        *,
        lifecycle_lock: asyncio.Lock,
        queued_signal: _QueuedMessageState,
        response_envelope: MessageEnvelope,
        signal_queued_message: bool,
    ) -> str | None:
        notice = begin_response_turn_notice(
            lifecycle_lock=lifecycle_lock,
            queued_signal=queued_signal,
            response_envelope=response_envelope,
            signal_queued_message=signal_queued_message,
        )
        if response_envelope.source_event_id == "$source":
            resume_entered.set()
        return notice

    with (
        patch.object(lifecycle, "_begin_response_turn_notice", new=observe_response_turn_start),
        patch.object(
            runner.approvals,
            "_run_owned_approval_continuation",
            new=run_approval_continuation,
        ),
    ):
        resume = asyncio.create_task(runner._resume_approval_source("$source"))
        await asyncio.wait_for(resume_entered.wait(), timeout=1.0)

        pending_human_messages = {message.event_id for message in queued_signal.pending_message_snapshot()}
        run_approval_continuation.assert_not_awaited()
        release_follow_up.set()
        assert await asyncio.wait_for(follow_up, timeout=1.0) == Owned()
        await asyncio.wait_for(resume, timeout=1.0)
        run_approval_continuation.assert_awaited_once()

    assert pending_human_messages == set()


@pytest.mark.asyncio
@pytest.mark.parametrize("revoked_layer", ["room", "entity", "pending"])
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_ready_approval_replay_rechecks_current_authorization(
    tmp_path: Path,
    revoked_layer: str,
) -> None:
    """A ready continuation must fail safely instead of executing after revocation."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    request = _plain_request(_target(thread_id="$thread"), source_event_id="$source")
    await _admit_approval_source(runner.deps.replies.store)
    continuation = approval_continuation(
        approval_id="approval-revoked",
        run_id="run-paused",
        room_id=request.room_id,
        thread_id=request.thread_id,
    )
    assert await paused_for_approval(runner.deps.replies.store, continuation) == continuation
    if revoked_layer in {"room", "pending"}:
        runner.deps.runtime.config.agents["general"].access = ResponderAccessConfig(
            current_room_members=True,
        )
        membership_client = AsyncMock(spec=nio.AsyncClient)
        membership_client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=[])
        if revoked_layer == "room":
            await runner.deps.runtime.agent_reply_memberships.refresh(
                runner.deps.runtime.config,
                runner.deps.runtime_paths,
                membership_client,
            )
    else:
        runner.deps.runtime.config.agents["general"].access = ResponderAccessConfig(users=[])
    with (
        patch.object(
            runner.approvals,
            "_settle",
            new=AsyncMock(return_value=True),
        ) as settle_failure,
        patch.object(
            runner.approvals,
            "_run_claimed_approval_lifecycle",
            new=AsyncMock(side_effect=AssertionError("revoked continuation executed")),
        ) as execute,
    ):
        if revoked_layer == "pending":
            with pytest.raises(ReplyMembershipPendingError):
                await runner._run_owned_or_locked_response(
                    request,
                    target=request.response_envelope.target,
                    locked_operation=AsyncMock(side_effect=AssertionError("approval source regenerated")),
                )
            assert await runner.deps.approval_store.approval_continuation(continuation.approval_id) == continuation
            assert await runner.deps.replies.store.is_pending("$source")
            settle_failure.assert_not_awaited()
            execute.assert_not_awaited()
            # The same owned source becomes a definitive denial after refresh.
            await runner.deps.runtime.agent_reply_memberships.refresh(
                runner.deps.runtime.config,
                runner.deps.runtime_paths,
                membership_client,
            )
        fate = await runner._run_owned_or_locked_response(
            request,
            target=request.response_envelope.target,
            locked_operation=AsyncMock(return_value=Unclaimed(UnclaimedReason.NOTHING_TO_RUN)),
        )

    assert fate == Owned()
    # The settlement fences the ready continuation the recheck refused.
    settle_failure.assert_awaited_once_with(
        continuation,
        ApprovalFailure(
            rl.ApprovalFailureKind.ERROR,
            "Current authorization no longer permits this tool approval continuation.",
        ),
    )
    execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_ready_team_approval_rechecks_every_persisted_member(tmp_path: Path) -> None:
    """A ready team continuation must not resume after one member loses access."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    request = _plain_request(_target(thread_id="$thread"), source_event_id="$source")
    await _admit_approval_source(runner.deps.replies.store)
    continuation = approval_continuation(
        approval_id="approval-team-member-revoked",
        run_id="run-paused",
        entity_kind="team",
        room_id=request.room_id,
        thread_id=request.thread_id,
        team_member_names=("general", "worker"),
        team_mode="coordinate",
    )
    assert await paused_for_approval(runner.deps.replies.store, continuation) == continuation
    runner.deps.runtime.config.agents["general"].access = ResponderAccessConfig(
        users=[continuation.requester_id],
    )
    runner.deps.runtime.config.agents["worker"] = runner.deps.runtime.config.agents["general"].model_copy(
        update={
            "display_name": "Worker",
            "access": ResponderAccessConfig(users=[]),
        },
    )
    failing = replace(
        continuation,
        state="failing",
        failure=ApprovalFailure(
            rl.ApprovalFailureKind.ERROR,
            "Current authorization no longer permits this tool approval continuation.",
        ),
    )

    with (
        patch.object(runner.approvals, "_request_failure", new=AsyncMock(return_value=failing)),
        patch.object(runner.approvals, "_settle", new=AsyncMock(return_value=True)),
        patch.object(
            runner.approvals,
            "_run_claimed_approval_lifecycle",
            new=AsyncMock(side_effect=AssertionError("revoked team continuation executed")),
        ) as execute,
    ):
        fate = await runner._run_owned_or_locked_response(
            request,
            target=request.response_envelope.target,
            locked_operation=AsyncMock(return_value=Unclaimed(UnclaimedReason.NOTHING_TO_RUN)),
        )

    assert fate == Owned()
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_incomplete_resume_failure_keeps_the_source_unhandled(tmp_path: Path) -> None:
    """A visible error is not terminal while its continuation still owns the source."""
    bot = _bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    request = _plain_request(_target(thread_id="$thread"), source_event_id="$source")
    await _admit_approval_source(runner.deps.replies.store)
    continuation = approval_continuation(
        approval_id="approval-incomplete-failure",
        run_id="run-paused",
        room_id=request.room_id,
        thread_id=request.thread_id,
        calls=(
            ApprovalCall(
                tool_call_id="call-1",
                tool_name="dangerous",
                invoking_agent="general",
                expires_at_ns=9_000_000_000_000_000_000,
                decision=ApprovalDecision.APPROVED,
            ),
        ),
    )
    assert await paused_for_approval(runner.deps.replies.store, continuation) is not None
    await bot._take_over_replies()
    # The resume failed.
    incomplete = Failed(FailureCause.ERROR, "Matrix unavailable")

    with patch.object(
        runner.approvals,
        "_run_claimed_approval_lifecycle",
        new=AsyncMock(return_value=incomplete),
    ):
        fate = await runner._run_owned_or_locked_response(
            request,
            target=request.response_envelope.target,
            locked_operation=AsyncMock(return_value=Unclaimed(UnclaimedReason.NOTHING_TO_RUN)),
        )

    retained = await runner.deps.approval_store.approval_continuation(continuation.approval_id)
    assert fate == Owned()
    assert retained is not None
    assert retained.state == "claimed"
    assert await runner.deps.replies.store.is_pending("$source")


@pytest.mark.asyncio
async def test_waiting_message_without_continuation_replays_the_safe_paused_turn(tmp_path: Path) -> None:
    """A crash before row creation replays because the paused tool has not executed."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    request = _plain_request(_target(thread_id="$thread"), source_event_id="$source")
    await _admit_approval_source(runner.deps.replies.store)

    paused = _ordered_pause(
        PausedAttempt(
            session_id="session-1",
            run_id="run-paused",
            requirements=(
                RunRequirement(ToolExecution(tool_call_id="call-1", tool_name="dangerous", requires_confirmation=True)),
            ),
            toolkit_owners={("general", "dangerous"): "test_toolkit"},
        ),
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
        patch("mindroom.approval.continuation_runner.uuid4", return_value=MagicMock(hex="approval-cancel")),
        patch("mindroom.approval.response.resolve_tool_approval_approver", return_value="@user:localhost"),
        patch("mindroom.approval.response.evaluate_tool_approval", new=AsyncMock(return_value=(True, 60.0))),
        patch.object(
            type(runner.deps.approval_store),
            "pause_for_approval",
            new=AsyncMock(side_effect=RuntimeError("crash before continuation commit")),
        ),
        pytest.raises(RuntimeError, match="crash before continuation commit"),
    ):
        async with _in_reply_span(runner, request, placeholder_event_id=None):
            await runner.approvals.suspend_for_approval(
                paused,
                request=request,
                target=request.response_envelope.target,
                execution_identity=identity,
                entity_kind="agent",
                history_scope=runner.deps.state_writer.history_scope(),
                show_tool_calls=True,
            )

    assert await runner.deps.approval_store.approval_continuation("approval-cancel") is None
    pending = await runner.deps.replies.store.pending(runtime_generation=runner.deps.approval_runtime_generation)
    assert [event.event_id for event in pending] == ["$source"]


@pytest.mark.asyncio
async def test_team_approval_persists_pinned_member_models(tmp_path: Path) -> None:
    """Approval pause must retain the member aliases selected at turn start."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    request = _plain_request(_target(thread_id="$thread"), source_event_id="$source")
    await _admit_approval_source(runner.deps.replies.store)
    paused = _ordered_pause(
        PausedAttempt(
            session_id="session-1",
            run_id="run-paused",
            requirements=(
                RunRequirement(ToolExecution(tool_call_id="call-1", tool_name="dangerous", requires_confirmation=True)),
            ),
            runtime_model_name="large",
            team_member_model_names=(("general", "large"),),
            toolkit_owners={("general", "dangerous"): "test_toolkit"},
        ),
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
        patch("mindroom.approval.continuation_runner.uuid4", return_value=MagicMock(hex="approval-team-models")),
        patch("mindroom.approval.response.resolve_tool_approval_approver", return_value="@user:localhost"),
        patch("mindroom.approval.response.evaluate_tool_approval", new=AsyncMock(return_value=(True, 60.0))),
    ):
        async with _in_reply_span(runner, request, placeholder_event_id=None) as (_edit_text, _send_text):
            await runner.approvals.suspend_for_approval(
                paused,
                request=request,
                target=request.response_envelope.target,
                execution_identity=identity,
                entity_kind="team",
                history_scope=runner.deps.state_writer.history_scope(),
                show_tool_calls=True,
                team_member_names=("general",),
                team_mode="coordinate",
            )

    continuation = await runner.deps.approval_store.approval_continuation("approval-team-models")
    assert continuation is not None
    assert continuation.team_member_model_names == (("general", "large"),)


@pytest.mark.asyncio
async def test_approval_pause_keeps_the_reply_correlation_when_the_request_names_none(tmp_path: Path) -> None:
    """A reply resumed after approval keeps the identity its tools saw before the pause."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    request = _plain_request(_target(thread_id="$thread", reply_to_event_id="$question"), source_event_id="$source")
    await _admit_approval_source(runner.deps.replies.store)
    paused = _ordered_pause(
        PausedAttempt(
            session_id="session-1",
            run_id="run-paused",
            requirements=(
                RunRequirement(ToolExecution(tool_call_id="call-1", tool_name="dangerous", requires_confirmation=True)),
            ),
            toolkit_owners={("general", "dangerous"): "test_toolkit"},
        ),
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
        patch("mindroom.approval.continuation_runner.uuid4", return_value=MagicMock(hex="approval-reply-correlation")),
        patch("mindroom.approval.response.resolve_tool_approval_approver", return_value="@user:localhost"),
        patch("mindroom.approval.response.evaluate_tool_approval", new=AsyncMock(return_value=(True, 60.0))),
    ):
        async with _in_reply_span(runner, request, placeholder_event_id=None) as (_edit_text, _send_text):
            await runner.approvals.suspend_for_approval(
                paused,
                request=request,
                target=request.response_envelope.target,
                execution_identity=identity,
                entity_kind="agent",
                history_scope=runner.deps.state_writer.history_scope(),
                show_tool_calls=True,
            )

    continuation = await runner.deps.approval_store.approval_continuation("approval-reply-correlation")
    assert continuation is not None
    assert continuation.correlation_id == "$question"


@pytest.mark.asyncio
async def test_approval_collaborators_read_live_config_after_hot_reload(tmp_path: Path) -> None:
    """Unchanged bots must apply reloaded approval policy and agent configuration."""
    bot = _bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    reloaded = bot.config.model_copy(deep=True)
    bot.orchestrator.config = reloaded
    tool = ToolExecution(tool_call_id="call-1", tool_name="dangerous", tool_args={})

    def resolve_approver(config: object, *_args: object) -> None:
        assert config is reloaded

    async def evaluate_policy(config: object, *_args: object) -> tuple[bool, float]:
        assert config is reloaded
        return False, 60.0

    with (
        patch("mindroom.approval.response.resolve_tool_approval_approver", side_effect=resolve_approver),
        patch("mindroom.approval.response.evaluate_tool_approval", side_effect=evaluate_policy),
    ):
        await plan_approval_calls(
            ((tool, "general"),),
            config=runner.deps.runtime.config,
            runtime_paths=runner.deps.runtime_paths,
            requester_id="@user:localhost",
            toolkit_owners={("general", "dangerous"): "test_toolkit"},
            room_id="!room:test",
            thread_id="$thread",
        )

    continuation = approval_continuation(
        approval_id="approval-live-config",
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
        transport_agent_name="general",
    )

    storage = runner.deps.state_writer.create_storage(identity)
    session = new_scope_session(session_id="session-1", scope=HistoryScope(kind="agent", scope_id="general"))
    session.runs = [
        RunRecord(
            run_id="run-1",
            session_id="session-1",
            agent_id="general",
            user_id="@user:localhost",
            status=RunStatus.paused,
        ),
    ]
    seed_session(storage, session)

    def create_from_live_config(_name: str, config: object, *_args: object, **_kwargs: object) -> None:
        assert config is reloaded
        msg = "live config observed"
        raise RuntimeError(msg)

    with (
        patch("mindroom.approval.execution.create_agent", side_effect=create_from_live_config),
        pytest.raises(RuntimeError, match="live config observed"),
    ):
        await runner.approvals._approval_execution.continue_run(
            continuation,
            paused_answer=PausedAnswer(),
            tool_context=turn_tool_context(runner.deps.runtime.config, runner.deps.runtime_paths, identity),
            tool_trace_collector=[],
            typing_log_context={},
            progress=None,
        )


@pytest.mark.asyncio
async def test_missing_approver_records_explicit_fail_closed_reason(tmp_path: Path) -> None:
    """A policy denial must explain that no human approval recipient was available."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    tool = ToolExecution(tool_call_id="call-1", tool_name="dangerous", tool_args={})

    with (
        patch("mindroom.approval.response.resolve_tool_approval_approver", return_value=None),
        patch("mindroom.approval.response.evaluate_tool_approval", new=AsyncMock(return_value=(True, 60.0))),
    ):
        plan = await plan_approval_calls(
            ((tool, "general"),),
            config=runner.deps.runtime.config,
            runtime_paths=runner.deps.runtime_paths,
            requester_id="@user:localhost",
            toolkit_owners={("general", "dangerous"): "test_toolkit"},
            room_id="!room:test",
            thread_id="$thread",
        )

    assert plan.calls[0].decision is ApprovalDecision.DENIED
    assert plan.calls[0].reason == NO_APPROVER_REASON
    assert plan.calls[0].human_approval_required is True


@pytest.mark.asyncio
@pytest.mark.parametrize("router_missing", [True, False])
async def test_missing_router_denies_a_gated_call_so_the_agent_can_invite_it(
    tmp_path: Path,
    router_missing: bool,
) -> None:
    """A card cannot reach a room without the router, so the run resumes with a denial that says how to recover."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    initialize_approval_store_for_test(runner.deps.runtime_paths, router_missing=lambda _room_id: router_missing)
    tool = ToolExecution(tool_call_id="call-1", tool_name="dangerous", tool_args={})

    with (
        patch("mindroom.approval.response.resolve_tool_approval_approver", return_value="@user:localhost"),
        patch("mindroom.approval.response.evaluate_tool_approval", new=AsyncMock(return_value=(True, 60.0))),
    ):
        plan = await plan_approval_calls(
            ((tool, "general"),),
            config=runner.deps.runtime.config,
            runtime_paths=runner.deps.runtime_paths,
            requester_id="@user:localhost",
            toolkit_owners={("general", "dangerous"): "test_toolkit"},
            room_id="!room:test",
            thread_id="$thread",
        )

    if router_missing:
        assert plan.calls[0].decision is ApprovalDecision.DENIED
        assert plan.calls[0].reason is not None
        assert "call `invite_router`" in plan.calls[0].reason
        assert plan.waiting_text is None
    else:
        assert plan.calls[0].decision is None
        assert plan.waiting_text == "Waiting for approval: `dangerous`"


@pytest.mark.asyncio
async def test_pause_plan_keeps_each_calls_approval_provenance(tmp_path: Path) -> None:
    """A mixed tool generation must not copy its final policy decision onto every call."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    human_tool = ToolExecution(tool_call_id="call-human", tool_name="publish_report", tool_args={})
    policy_tool = ToolExecution(tool_call_id="call-policy", tool_name="read_report", tool_args={})

    with (
        patch(
            "mindroom.approval.response.resolve_tool_approval_approver",
            return_value="@user:localhost",
        ),
        patch(
            "mindroom.approval.response.evaluate_tool_approval",
            new=AsyncMock(side_effect=[(True, 60.0), (False, 60.0)]),
        ),
    ):
        plan = await plan_approval_calls(
            (
                (human_tool, "general"),
                (policy_tool, "general"),
            ),
            config=runner.deps.runtime.config,
            runtime_paths=runner.deps.runtime_paths,
            requester_id="@user:localhost",
            toolkit_owners={("general", "publish_report"): "test_toolkit", ("general", "read_report"): "test_toolkit"},
            room_id="!room:test",
            thread_id="$thread",
        )

    assert [call.human_approval_required for call in plan.calls] == [True, False]


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
            "mindroom.approval.response.resolve_tool_approval_approver",
            return_value="@user:localhost",
        ),
        patch("mindroom.approval.response.evaluate_tool_approval", side_effect=evaluate),
    ):
        plan = await plan_approval_calls(
            (
                (automatic, "general"),
                (gated, "general"),
            ),
            config=runner.deps.runtime.config,
            runtime_paths=runner.deps.runtime_paths,
            requester_id="@user:localhost",
            toolkit_owners={
                ("general", "conditional_read"): "test_toolkit",
                ("general", "conditional_write"): "test_toolkit",
            },
            room_id="!room:test",
            thread_id="$thread",
        )

    continuation = approval_continuation(
        approval_id="approval-mixed",
        response_event_id="$thinking",
        calls=plan.calls,
        state="waiting",
    )
    approval_store = MagicMock(
        prepare_reply_card=AsyncMock(return_value=object()),
        reserve_and_publish=AsyncMock(return_value=True),
        transport=FakeApprovalTransport(),
    )
    with patch(
        "mindroom.approval.response.approval_manager.get_approval_store",
        return_value=approval_store,
    ):
        await approval_response._publish_cards(
            continuation,
            plan,
            config=runner.deps.runtime.config,
            runtime_paths=runner.deps.runtime_paths,
            store=runner.deps.approval_store,
            target=_target(thread_id="$thread"),
            failure_reason="card failed",
        )

    assert plan.waiting_text == "Waiting for approval: `conditional_write`"
    assert [call.decision for call in plan.calls] == [ApprovalDecision.APPROVED, None]
    approval_store.prepare_reply_card.assert_awaited_once()
    assert approval_store.prepare_reply_card.await_args.kwargs["tool_name"] == "conditional_write"
    approval_store.reserve_and_publish.assert_awaited_once()


@pytest.mark.asyncio
async def test_all_human_gated_pause_plan_keeps_waiting_text_and_cards(tmp_path: Path) -> None:
    """A fully gated batch must retain its current visible approval behavior."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    first = ToolExecution(tool_call_id="call-1", tool_name="dangerous_one", tool_args={})
    second = ToolExecution(tool_call_id="call-2", tool_name="dangerous_two", tool_args={})

    with (
        patch(
            "mindroom.approval.response.resolve_tool_approval_approver",
            return_value="@user:localhost",
        ),
        patch(
            "mindroom.approval.response.evaluate_tool_approval",
            new=AsyncMock(return_value=(True, 60.0)),
        ),
    ):
        plan = await plan_approval_calls(
            (
                (first, "general"),
                (second, "general"),
            ),
            config=runner.deps.runtime.config,
            runtime_paths=runner.deps.runtime_paths,
            requester_id="@user:localhost",
            toolkit_owners={("general", "dangerous_one"): "test_toolkit", ("general", "dangerous_two"): "test_toolkit"},
            room_id="!room:test",
            thread_id="$thread",
        )

    continuation = approval_continuation(
        approval_id="approval-gated",
        response_event_id="$thinking",
        calls=plan.calls,
        state="waiting",
    )
    approval_store = MagicMock(
        prepare_reply_card=AsyncMock(return_value=object()),
        reserve_and_publish=AsyncMock(return_value=True),
        transport=FakeApprovalTransport(),
    )
    with patch(
        "mindroom.approval.response.approval_manager.get_approval_store",
        return_value=approval_store,
    ):
        await approval_response._publish_cards(
            continuation,
            plan,
            config=runner.deps.runtime.config,
            runtime_paths=runner.deps.runtime_paths,
            store=runner.deps.approval_store,
            target=_target(thread_id="$thread"),
            failure_reason="card failed",
        )

    assert plan.waiting_text == "Waiting for approval: `dangerous_one`, `dangerous_two`"
    assert [call.decision for call in plan.calls] == [None, None]
    assert [awaited.kwargs["tool_name"] for awaited in approval_store.prepare_reply_card.await_args_list] == [
        "dangerous_one",
        "dangerous_two",
    ]
    approval_store.reserve_and_publish.assert_awaited_once()


@pytest.mark.asyncio
async def test_automatic_pause_publishes_ordered_tools_and_wakes_continuation(tmp_path: Path) -> None:
    """A fully automatic pause must acknowledge its tools before continuation is scheduled."""
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
    await _admit_approval_source(runner.deps.replies.store)
    request = _plain_request(_target(thread_id="$thread"), source_event_id="$source")
    paused = _ordered_pause(
        PausedAttempt(
            session_id="session-1",
            run_id="run-1",
            requirements=(
                RunRequirement(
                    ToolExecution(
                        tool_call_id="call-auto-1",
                        tool_name="conditional_tool",
                        tool_args={"requires_approval": False},
                    ),
                ),
                RunRequirement(
                    ToolExecution(
                        tool_call_id="call-auto-2",
                        tool_name="conditional_tool",
                        tool_args={"requires_approval": False},
                    ),
                ),
            ),
            toolkit_owners={("general", "conditional_tool"): "test_toolkit"},
        ),
    )
    identity = runner.deps.tool_runtime.build_execution_identity(
        target=request.response_envelope.target,
        user_id=request.user_id,
    )
    retry_sources = Mock()
    runner = replace_response_runner_deps(bot, retry_approval_sources=retry_sources)

    with patch(
        "mindroom.approval.response.resolve_tool_approval_approver",
        return_value="@user:localhost",
    ):
        async with _in_reply_span(runner, request) as (edit_text, send_text):
            outcome = await runner.approvals.suspend_for_approval(
                paused,
                request=request,
                target=request.response_envelope.target,
                execution_identity=identity,
                entity_kind="agent",
                history_scope=runner.deps.state_writer.history_scope(),
                show_tool_calls=True,
            )

    edit_request = edit_text.await_args.args[0]
    assert edit_request.new_text == paused.response_text
    assert edit_request.tool_trace == list(paused.tool_trace)
    send_text.assert_not_awaited()
    retry_sources.assert_called_once_with(request.room_id, ("$source",))
    assert outcome.final_visible_body == paused.response_text
    assert outcome.delivery_kind == "edited"
    assert outcome.extra_content == {STREAM_STATUS_KEY: STREAM_STATUS_PENDING}


@pytest.mark.asyncio
@pytest.mark.parametrize(("window_thread_id", "card_expected"), [("$thread", False), ("$other", True)])
async def test_first_pause_skips_the_card_inside_the_requesters_auto_approve_window_of_the_reply_thread(
    tmp_path: Path,
    window_thread_id: str,
    card_expected: bool,
) -> None:
    """A reply's gated call runs unasked only when the window names the thread that reply answers in."""
    bot = _bot(tmp_path)
    bot.config.tool_approval = bot.config.tool_approval.model_copy(
        update={"rules": [ApprovalRuleConfig(match="dangerous", action="require_approval")]},
    )
    runner = unwrap_extracted_collaborator(bot._response_runner)
    await _admit_approval_source(runner.deps.replies.store)
    request = _plain_request(_target(thread_id="$thread"), source_event_id="$source")
    start_auto_approve(
        runner.deps.runtime_paths,
        room_id="!room:localhost",
        thread_id=window_thread_id,
        requester_id="@user:localhost",
        duration=timedelta(minutes=5),
    )
    paused = _ordered_pause(
        PausedAttempt(
            session_id="session-1",
            run_id="run-1",
            requirements=(RunRequirement(ToolExecution(tool_call_id="call-1", tool_name="dangerous", tool_args={})),),
            toolkit_owners={("general", "dangerous"): "test_toolkit"},
        ),
    )
    identity = runner.deps.tool_runtime.build_execution_identity(
        target=request.response_envelope.target,
        user_id=request.user_id,
    )
    retry_sources = Mock()
    runner = replace_response_runner_deps(bot, retry_approval_sources=retry_sources)
    approval_store = MagicMock(
        prepare_reply_card=AsyncMock(return_value=object()),
        reserve_and_publish=AsyncMock(return_value=True),
        transport=FakeApprovalTransport(),
    )

    with (
        patch(
            "mindroom.approval.response.resolve_tool_approval_approver",
            return_value="@user:localhost",
        ),
        patch("mindroom.approval.response.approval_manager.get_approval_store", return_value=approval_store),
    ):
        async with _in_reply_span(runner, request):
            outcome = await runner.approvals.suspend_for_approval(
                paused,
                request=request,
                target=request.response_envelope.target,
                execution_identity=identity,
                entity_kind="agent",
                history_scope=runner.deps.state_writer.history_scope(),
                show_tool_calls=True,
            )

    if card_expected:
        approval_store.prepare_reply_card.assert_awaited_once()
        assert outcome.extra_content == {STREAM_STATUS_KEY: STREAM_STATUS_APPROVAL_PENDING}
    else:
        approval_store.prepare_reply_card.assert_not_awaited()
        retry_sources.assert_called_once_with(request.room_id, ("$source",))
        assert outcome.extra_content == {STREAM_STATUS_KEY: STREAM_STATUS_PENDING}


@pytest.mark.asyncio
async def test_automatic_pause_without_visible_event_sends_ordered_tools(tmp_path: Path) -> None:
    """A direct automatic pause sends the presentation its continuation will extend."""
    bot = _bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    await _admit_approval_source(runner.deps.replies.store)
    request = _plain_request(_target(thread_id="$thread"), source_event_id="$source")
    paused = _ordered_pause(
        PausedAttempt(
            session_id="session-1",
            run_id="run-1",
            requirements=(
                RunRequirement(ToolExecution(tool_call_id="call-auto", tool_name="conditional_read", tool_args={})),
            ),
            toolkit_owners={("general", "conditional_read"): "test_toolkit"},
        ),
    )
    identity = runner.deps.tool_runtime.build_execution_identity(
        target=request.response_envelope.target,
        user_id=request.user_id,
    )
    retry_sources = Mock()
    runner = replace_response_runner_deps(bot, retry_approval_sources=retry_sources)

    with (
        patch(
            "mindroom.approval.response.resolve_tool_approval_approver",
            return_value="@user:localhost",
        ),
        patch(
            "mindroom.approval.response.evaluate_tool_approval",
            new=AsyncMock(return_value=(False, 60.0)),
        ),
    ):
        async with _in_reply_span(runner, request, placeholder_event_id=None) as (_edit_text, send_text):
            outcome = await runner.approvals.suspend_for_approval(
                paused,
                request=request,
                target=request.response_envelope.target,
                execution_identity=identity,
                entity_kind="agent",
                history_scope=runner.deps.state_writer.history_scope(),
                show_tool_calls=True,
            )

    send_request = send_text.await_args.args[0]
    assert send_request.response_text == paused.response_text
    assert send_request.tool_trace == list(paused.tool_trace)
    assert send_request.extra_content == {STREAM_STATUS_KEY: STREAM_STATUS_PENDING}
    retry_sources.assert_called_once_with(request.room_id, ("$source",))
    assert outcome.final_visible_body == paused.response_text
    assert outcome.delivery_kind == "sent"
    assert outcome.extra_content == {STREAM_STATUS_KEY: STREAM_STATUS_PENDING}


@pytest.mark.parametrize(
    ("turn_visibility", "reloaded_visibility"),
    [(False, True), (True, False)],
)
@pytest.mark.asyncio
async def test_pause_persists_visibility_and_presentation_frozen_for_the_turn(
    tmp_path: Path,
    turn_visibility: bool,
    reloaded_visibility: bool,
) -> None:
    """Approval publication cannot reinterpret a committed stream after config reload."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    await _admit_approval_source(runner.deps.replies.store)
    request = _plain_request(_target(thread_id="$thread"), source_event_id="$source")
    trace = ToolTraceEntry(
        type="tool_call_started",
        tool_name="inspect",
        tool_call_id="call-1",
    )
    paused = PausedAttempt(
        session_id="session-1",
        run_id="run-1",
        requirements=(RunRequirement(ToolExecution(tool_call_id="call-1", tool_name="inspect", tool_args={})),),
        response_text=("Before approval.\n\n🔧 `inspect` [1] ⏳" if turn_visibility else "Before approval."),
        tool_trace=(trace,),
        toolkit_owners={("general", "inspect"): "test_toolkit"},
    )
    identity = runner.deps.tool_runtime.build_execution_identity(
        target=request.response_envelope.target,
        user_id=request.user_id,
    )
    runner.deps.runtime.config.agents[runner.deps.agent_name].show_tool_calls = reloaded_visibility

    with (
        patch("mindroom.approval.response.resolve_tool_approval_approver", return_value="@user:localhost"),
        patch(
            "mindroom.approval.response.evaluate_tool_approval",
            new=AsyncMock(return_value=(True, 60.0)),
        ),
        patch.object(runner.approvals, "_publish_generation", new=AsyncMock()),
    ):
        # The turn's span froze the visibility it claimed with.
        async with _in_reply_span(
            runner,
            request,
            placeholder_event_id="$stream",
            show_tool_calls=turn_visibility,
        ) as (edit_text, _send_text):
            outcome = await runner.approvals.suspend_for_approval(
                paused,
                request=request,
                target=request.response_envelope.target,
                execution_identity=identity,
                entity_kind="agent",
                history_scope=runner.deps.state_writer.history_scope(),
                show_tool_calls=turn_visibility,
            )

    continuation = await runner.deps.approval_store.approval_continuation_for_source("$source")
    assert continuation is not None
    assert continuation.show_tool_calls is turn_visibility
    edit_request = edit_text.await_args.args[0]
    assert edit_request.new_text == paused.response_text
    assert edit_request.tool_trace == ([trace] if turn_visibility else None)
    assert outcome.final_visible_body == paused.response_text
    assert outcome.tool_trace == ((trace,) if turn_visibility else ())


@pytest.mark.asyncio
async def test_pause_republishes_the_acknowledged_interactive_body(tmp_path: Path) -> None:
    """Suspension must not replace a rendered question with its canonical fenced JSON."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    await _admit_approval_source(runner.deps.replies.store)
    request = _plain_request(_target(thread_id="$thread"), source_event_id="$source")
    raw_interactive = (
        '```interactive\n{"question":"Choose","options":[{"emoji":"✅","label":"Yes","value":"yes"}]}\n```'
    )
    rendered_interactive = interactive.parse_and_format_interactive(
        raw_interactive,
        extract_mapping=True,
    ).formatted_text
    tool = ToolExecution(tool_call_id="call-1", tool_name="inspect", tool_args={})
    trace = ToolTraceEntry(type="tool_call_started", tool_name="inspect", tool_call_id="call-1")
    marker = "\n\n🔧 `inspect` [1] ⏳"
    error = ResponsePausedForApproval(
        PausedAttempt(
            session_id="session-1",
            run_id="run-1",
            requirements=(RunRequirement(tool),),
            response_text=f"{raw_interactive}{marker}",
            tool_trace=(trace,),
            toolkit_owners={("general", "inspect"): "test_toolkit"},
        ),
    )
    error.capture_presentation(
        StreamingPresentation(
            response_text=f"{raw_interactive}{marker}",
            rendered_response_text=f"{rendered_interactive}{marker}",
            tool_trace=(trace,),
        ),
    )
    paused = response_runner._paused_with_committed_presentation(error, show_tool_calls=True)
    identity = runner.deps.tool_runtime.build_execution_identity(
        target=request.response_envelope.target,
        user_id=request.user_id,
    )

    with (
        patch("mindroom.approval.response.resolve_tool_approval_approver", return_value="@user:localhost"),
        patch(
            "mindroom.approval.response.evaluate_tool_approval",
            new=AsyncMock(return_value=(True, 60.0)),
        ),
        patch.object(runner.approvals, "_publish_generation", new=AsyncMock()),
    ):
        async with _in_reply_span(runner, request, placeholder_event_id="$stream") as (edit_text, _send_text):
            await runner.approvals.suspend_for_approval(
                paused,
                request=request,
                target=request.response_envelope.target,
                execution_identity=identity,
                entity_kind="agent",
                history_scope=runner.deps.state_writer.history_scope(),
                show_tool_calls=True,
            )

    assert await runner.deps.approval_store.approval_continuation_for_source("$source") is not None
    assert edit_text.await_args.args[0].new_text == f"{rendered_interactive}{marker}"


@pytest.mark.asyncio
async def test_pause_publication_rejects_an_unanchored_agent_tool(tmp_path: Path) -> None:
    """The approval coordinator must not repair or persist an unacknowledged tool start."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    await _admit_approval_source(runner.deps.replies.store)
    request = _plain_request(_target(thread_id="$thread"), source_event_id="$source")
    tool = ToolExecution(tool_call_id="call-1", tool_name="inspect", tool_args={})
    trace = ToolTraceEntry(type="tool_call_started", tool_name="inspect", tool_call_id="call-1")
    error = ResponsePausedForApproval(
        PausedAttempt(
            session_id="session-1",
            run_id="run-1",
            requirements=(RunRequirement(tool),),
            response_text="Before approval.\n\n🔧 `inspect` [1] ⏳",
            tool_trace=(trace,),
            toolkit_owners={("general", "inspect"): "test_toolkit"},
        ),
    )
    error.capture_presentation(
        StreamingPresentation(
            response_text="Before approval.",
        ),
    )
    handed_off = response_runner._paused_with_committed_presentation(error, show_tool_calls=True)
    identity = runner.deps.tool_runtime.build_execution_identity(
        target=request.response_envelope.target,
        user_id=request.user_id,
    )

    edit_text = AsyncMock(return_value=True)
    with (
        patch.object(DeliveryGateway, "edit_text", new=edit_text),
        patch("mindroom.approval.response.resolve_tool_approval_approver", return_value="@user:localhost"),
        patch(
            "mindroom.approval.response.evaluate_tool_approval",
            new=AsyncMock(return_value=(True, 60.0)),
        ),
        patch.object(runner.approvals, "_publish_generation", new=AsyncMock()),
        pytest.raises(RuntimeError, match="ordered presentation"),
    ):
        await runner.approvals.suspend_for_approval(
            handed_off,
            request=request,
            target=request.response_envelope.target,
            execution_identity=identity,
            entity_kind="agent",
            history_scope=runner.deps.state_writer.history_scope(),
            show_tool_calls=True,
        )

    continuation = await runner.deps.approval_store.approval_continuation_for_source("$source")
    assert continuation is None
    edit_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_pause_publication_rejects_an_unanchored_team_tool(
    tmp_path: Path,
) -> None:
    """A first team tool must be acknowledged in its member slot before approval publication."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    await _admit_approval_source(runner.deps.replies.store)
    request = _plain_request(_target(thread_id="$thread"), source_event_id="$source")
    tool = ToolExecution(tool_call_id="call-1", tool_name="inspect", tool_args={})
    requirement = RunRequirement(tool_execution=tool, member_agent_id="member-a", member_display_name="Member A")
    trace = ToolTraceEntry(
        type="tool_call_started",
        tool_name="inspect",
        tool_call_id="call-1",
        member_id="member-a",
    )
    error = ResponsePausedForApproval(
        PausedAttempt(
            session_id="session-1",
            run_id="run-1",
            requirements=(requirement,),
            response_text="🤝 **Team Response** (Member A):\n\n**Member A**: 🔧 `inspect` [1] ⏳",
            tool_trace=(trace,),
            toolkit_owners={("general", "inspect"): "test_toolkit"},
        ),
    )
    error.capture_presentation(
        StreamingPresentation(response_text=""),
    )
    handed_off = response_runner._paused_with_committed_presentation(error, show_tool_calls=True)
    identity = runner.deps.tool_runtime.build_execution_identity(
        target=request.response_envelope.target,
        user_id=request.user_id,
    )

    edit_text = AsyncMock(return_value=True)
    with (
        patch.object(DeliveryGateway, "edit_text", new=edit_text),
        patch("mindroom.approval.response.resolve_tool_approval_approver", return_value="@user:localhost"),
        patch(
            "mindroom.approval.response.evaluate_tool_approval",
            new=AsyncMock(return_value=(True, 60.0)),
        ),
        patch.object(runner.approvals, "_publish_generation", new=AsyncMock()),
        pytest.raises(RuntimeError, match="ordered presentation"),
    ):
        await runner.approvals.suspend_for_approval(
            handed_off,
            request=request,
            target=request.response_envelope.target,
            execution_identity=identity,
            entity_kind="team",
            history_scope=runner.deps.state_writer.history_scope(),
            show_tool_calls=True,
            team_member_names=("member-a",),
            team_mode="coordinate",
        )

    continuation = await runner.deps.approval_store.approval_continuation_for_source("$source")
    assert continuation is None
    edit_text.assert_not_awaited()


def test_pause_presentation_rejects_a_tool_in_the_wrong_member_scope() -> None:
    """Stable call identity cannot authorize a marker attached to another member."""
    tool = ToolExecution(tool_call_id="call-1", tool_name="inspect", tool_args={})
    requirement = RunRequirement(tool_execution=tool, member_agent_id="member-a", member_display_name="Same")
    paused = PausedAttempt(
        session_id="session-1",
        run_id="run-1",
        requirements=(requirement,),
        response_text="🤝 **Team Response** (Same):\n\n**Same**: 🔧 `inspect` [1] ⏳",
        tool_trace=(
            ToolTraceEntry(
                type="tool_call_started",
                tool_name="inspect",
                tool_call_id="call-1",
                member_id="member-b",
            ),
        ),
        toolkit_owners={("general", "inspect"): "test_toolkit"},
    )

    with pytest.raises(RuntimeError, match="ordered presentation"):
        require_ordered_pause_presentation(paused, show_tool_calls=True)


def test_pause_presentation_rejects_a_duplicate_visible_marker() -> None:
    """One trace slot cannot authorize two visible markers."""
    tool = ToolExecution(tool_call_id="call-1", tool_name="inspect", tool_args={})
    paused = _ordered_pause(
        PausedAttempt(
            session_id="session-1",
            run_id="run-1",
            requirements=(RunRequirement(tool),),
            toolkit_owners={("general", "inspect"): "test_toolkit"},
        ),
    )
    duplicated = replace(paused, response_text=f"{paused.response_text}\n\n{paused.response_text}")

    with pytest.raises(RuntimeError, match="ordered presentation"):
        require_ordered_pause_presentation(duplicated, show_tool_calls=True)


def test_pause_presentation_rejects_an_unmatched_visible_marker() -> None:
    """Visible markers without a trace slot must fail the approval handoff."""
    tool = ToolExecution(tool_call_id="call-1", tool_name="inspect", tool_args={})
    paused = _ordered_pause(
        PausedAttempt(
            session_id="session-1",
            run_id="run-1",
            requirements=(RunRequirement(tool),),
            toolkit_owners={("general", "inspect"): "test_toolkit"},
        ),
    )
    unmatched = replace(paused, response_text=f"{paused.response_text}\n\n🔧 `other` [2] ⏳")

    with pytest.raises(RuntimeError, match="ordered presentation"):
        require_ordered_pause_presentation(unmatched, show_tool_calls=True)


def test_streaming_pause_handoff_uses_only_transport_committed_presentation() -> None:
    """The lifecycle persists the body/trace acknowledged by Matrix, not buffered stream state."""
    error = ResponsePausedForApproval(
        PausedAttempt(
            session_id="session-1",
            run_id="run-1",
            requirements=(RunRequirement(ToolExecution(tool_call_id="call-1", tool_name="inspect")),),
            toolkit_owners={("general", "inspect"): "test_toolkit"},
        ),
    )
    trace = ToolTraceEntry(type="tool_call_started", tool_name="inspect", tool_call_id="call-1")
    error.capture_presentation(
        StreamingPresentation(
            response_text="Committed.\n\n🔧 `inspect` [1] ⏳",
            tool_trace=(trace,),
            state={"kind": "team", "consensus": "Committed."},
        ),
    )

    paused = response_runner._paused_with_committed_presentation(error, show_tool_calls=True)

    assert paused.response_text == "Committed.\n\n🔧 `inspect` [1] ⏳"
    assert paused.tool_trace == (trace,)
    assert paused.response_presentation_state == {"kind": "team", "consensus": "Committed."}


def test_hidden_textless_team_pause_keeps_internal_continuation_snapshot() -> None:
    """Hidden tool state is resume-only and survives an empty committed presentation."""
    tool = ToolExecution(tool_call_id="call-1", tool_name="inspect")
    trace = ToolTraceEntry(
        type="tool_call_started",
        tool_name="inspect",
        tool_call_id="call-1",
        member_id="member-a",
    )
    state = {
        "kind": "team_stream",
        "version": 3,
        "members": [{"id": "member-a", "display_name": "Member A", "content": ""}],
        "consensus": "",
    }
    error = ResponsePausedForApproval(
        PausedAttempt(
            session_id="session-1",
            run_id="run-1",
            requirements=(RunRequirement(tool),),
            tool_trace=(trace,),
            response_presentation_state=state,
            toolkit_owners={("general", "inspect"): "test_toolkit"},
        ),
    )
    error.capture_presentation(StreamingPresentation(response_text=""))

    paused = response_runner._paused_with_committed_presentation(error, show_tool_calls=False)

    assert paused.response_text == ""
    assert paused.tool_trace == (trace,)
    assert paused.response_presentation_state == state
    restored = TeamStreamPresentation.restore(
        config_names=["member-a"],
        show_tool_calls=False,
        state=paused.response_presentation_state,
        tool_trace=paused.tool_trace,
    )
    assert restored.tool_trace == [trace]


@pytest.mark.asyncio
async def test_completed_approval_continuation_delivers_canonical_ordered_body_unchanged(tmp_path: Path) -> None:
    """Final delivery cannot flatten structured tool anchors ahead of their stream positions."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    target = _target(thread_id="$thread")
    request = _plain_request(target, source_event_id="$source")
    continuation = approval_continuation(
        approval_id="approval-visible-tools",
        room_id=target.room_id,
        thread_id=target.resolved_thread_id,
        response_event_id="$thinking",
        state="claimed",
    )
    trace = [
        ToolTraceEntry(type="tool_call_completed", tool_name="fetch_report"),
        ToolTraceEntry(type="tool_call_completed", tool_name="save_report"),
    ]
    ordered_body = "Before first.\n\n🔧 `fetch_report` [1]\n\nBetween calls.\n\n🔧 `save_report` [2]\n\nFinal answer."

    async def continue_call(
        *_args: object,
        tool_trace_collector: list[ToolTraceEntry],
        **_kwargs: object,
    ) -> CompletedApprovalRun:
        tool_trace_collector.extend(trace)
        return CompletedApprovalRun(response_text=ordered_body, metadata_content={})

    deliver_final = AsyncMock(
        return_value=FinalDeliveryOutcome(
            ending=Completed(),
            event_id="$thinking",
            is_visible_response=True,
            final_visible_body="Final answer.",
            delivery_kind="edited",
        ),
    )
    with (
        patch.object(runner.approvals, "_continue_entity_call", new=continue_call),
        patch.object(DeliveryGateway, "deliver_final", new=deliver_final),
    ):
        await runner.approvals._execute_claimed_approval(
            continuation,
            request=request,
            target=target,
            identity=runner.locked.build_lifecycle(request, response_kind="ai").identity,
        )

    final_request = deliver_final.await_args.args[0]
    assert final_request.response_text == ordered_body
    assert final_request.tool_trace == trace


@asynccontextmanager
async def _resumed_streamable_approval(
    bot: AgentBot,
    *,
    requester_online: bool,
    entity_kind: Literal["agent", "team"] = "agent",
) -> AsyncIterator[ApprovalContinuation]:
    """Claim one ready agent or team continuation, whose requester is online or away, with its resume span."""
    runner = unwrap_extracted_collaborator(bot._response_runner)
    store = runner.deps.replies.store
    await _admit_approval_source(store)
    client = runner.locked.client()
    client.get_presence.return_value.presence = "online" if requester_online else "offline"
    sent = count(1)
    client.room_send.side_effect = lambda **_kwargs: nio.RoomSendResponse(
        event_id=f"$sent-{next(sent)}",
        room_id="!room:localhost",
    )
    continuation = approval_continuation(
        approval_id="approval-streamed",
        entity_kind=entity_kind,
        show_tool_calls=True,
        runtime_model_name="default",
        team_member_names=("general",) if entity_kind == "team" else (),
        team_mode="coordinate" if entity_kind == "team" else None,
        execution_identity=runner.deps.tool_runtime.build_execution_identity(
            target=_target(thread_id="$thread", reply_to_event_id="$source"),
            user_id="@user:localhost",
        ),
    )
    assert await paused_for_approval(store, continuation) is not None
    async with resumed_approval(bot, continuation) as claimed:
        yield claimed


def _approval_reply_edits(client: AsyncClient) -> list[tuple[str, str]]:
    """Return each edit of the continued reply as its stream status and visible body."""
    edits: list[tuple[str, str]] = []
    for call in client.room_send.call_args_list:
        content = call.kwargs["content"]
        if content.get("m.relates_to", {}).get("event_id") == "$waiting" and "m.new_content" in content:
            edits.append((content["m.new_content"][STREAM_STATUS_KEY], content["m.new_content"]["body"]))
    return edits


@pytest.mark.asyncio
@pytest.mark.parametrize("requester_online", [True, False])
async def test_approval_continuation_streams_into_its_reply_only_for_streaming_requesters(
    tmp_path: Path,
    *,
    requester_online: bool,
) -> None:
    """An online requester watches resumed work in the reply; an away requester gets only the final edit."""
    bot = _bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    completed = ToolTraceEntry(type="tool_call_completed", tool_name="inspect", tool_call_id="call-1")
    body = "Checking.\n\n🔧 `inspect` [1]\n\nThe report is clean."

    async def continue_call(
        _continuation: ApprovalContinuation,
        *,
        tool_trace_collector: list[ToolTraceEntry],
        progress: ProgressPublisher | None,
        **_kwargs: object,
    ) -> CompletedApprovalRun:
        assert (progress is not None) is requester_online
        if progress is not None:
            await progress(StructuredStreamChunk(content=body, tool_trace=[completed]))
        tool_trace_collector.append(completed)
        return CompletedApprovalRun(response_text=body, metadata_content={})

    with patch.object(runner.approvals, "_continue_entity_call", new=continue_call):
        async with _resumed_streamable_approval(bot, requester_online=requester_online) as claimed:
            ending = await runner.approvals._run_claimed_approval_lifecycle(
                claimed,
                target=_target(thread_id="$thread", reply_to_event_id="$source"),
            )

    assert ending == Completed()
    progress_edits = [(STREAM_STATUS_STREAMING, body)] if requester_online else []
    assert _approval_reply_edits(runner.locked.client()) == [*progress_edits, (STREAM_STATUS_COMPLETED, body)]


@pytest.mark.asyncio
async def test_team_approval_continuation_streams_into_its_reply(tmp_path: Path) -> None:
    """A resumed team run edits the same reply live before its durable final edit."""
    bot = _bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    body = "🤝 **Team Response** (General):\n\n**General**: The report is clean."

    async def continue_team(*, progress: ProgressPublisher | None, **kwargs: object) -> CompletedApprovalRun:
        # The runner's call must fit the real function, not only this fake.
        inspect.signature(continue_paused_team_run).bind(progress=progress, **kwargs)
        assert progress is not None
        await progress(StructuredStreamChunk(content=body))
        return CompletedApprovalRun(response_text=body, metadata_content={})

    async with _resumed_streamable_approval(bot, requester_online=True, entity_kind="team") as claimed:
        with (
            patch("mindroom.approval.continuation_runner.continue_paused_team_run", new=continue_team),
            patch("mindroom.reply_turn.request.typing_indicator", _noop_typing),
        ):
            ending = await runner.approvals._run_claimed_approval_lifecycle(
                claimed,
                target=_target(thread_id="$thread", reply_to_event_id="$source"),
            )

    assert ending == Completed()
    assert _approval_reply_edits(runner.locked.client()) == [
        (STREAM_STATUS_STREAMING, body),
        (STREAM_STATUS_COMPLETED, body),
    ]


@pytest.mark.asyncio
async def test_streamed_approval_continuation_that_pauses_again_ends_on_its_new_approval(tmp_path: Path) -> None:
    """Live progress yields to the next approval, leaving the reply approval-pending."""
    bot = _bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    gated = _ordered_pause(
        PausedAttempt(
            session_id="session-1",
            run_id="run-1",
            requirements=(
                RunRequirement(
                    ToolExecution(
                        tool_call_id="call-2",
                        tool_name="publish_report",
                        tool_args={},
                        requires_confirmation=True,
                    ),
                ),
            ),
            toolkit_owners={("general", "publish_report"): "reports"},
        ),
    )
    paused = replace(gated, response_text=f"Checked the report.\n\n{gated.response_text}")

    async def continue_call(
        _continuation: ApprovalContinuation,
        *,
        progress: ProgressPublisher | None,
        **_kwargs: object,
    ) -> PausedAttempt:
        assert progress is not None
        await progress(StructuredStreamChunk(content="Checked the report."))
        return paused

    async def publish_cards(continuation: ApprovalContinuation, *_args: object, **_kwargs: object) -> None:
        activated = await runner.deps.approval_store.activate_approval_continuation(
            continuation.approval_id,
            expected_generation=continuation.generation,
        )
        assert activated is not None

    async with _resumed_streamable_approval(bot, requester_online=True) as claimed:
        with (
            patch.object(runner.approvals, "_continue_entity_call", new=continue_call),
            patch("mindroom.approval.response._publish_cards", new=publish_cards),
        ):
            ending = await runner.approvals._run_claimed_approval_lifecycle(
                claimed,
                target=_target(thread_id="$thread", reply_to_event_id="$source"),
            )

    assert ending == AwaitingApproval()
    assert _approval_reply_edits(runner.locked.client()) == [
        (STREAM_STATUS_STREAMING, "Checked the report."),
        (STREAM_STATUS_APPROVAL_PENDING, paused.response_text),
    ]


@pytest.mark.asyncio
async def test_stopping_a_streamed_approval_continuation_settles_it_as_cancelled(tmp_path: Path) -> None:
    """A stop during live progress ends progress before the reply settles as cancelled."""
    bot = _bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    # The turn whose answer the approval paused, which the Stop reaction names.
    await bot._turn_store.record_pending_turn(
        TurnRecord.create(
            ("$source",),
            response_event_id="$waiting",
            completed=False,
            response_owner="general",
            requester_id="@user:localhost",
            conversation_target=_target(thread_id="$thread", reply_to_event_id="$source"),
        ),
    )
    client = runner.locked.client()
    progress_edited = asyncio.Event()

    async def continue_call(
        _continuation: ApprovalContinuation,
        *,
        progress: ProgressPublisher | None,
        **_kwargs: object,
    ) -> CompletedApprovalRun:
        assert progress is not None
        await progress(StructuredStreamChunk(content="Checking the report."))
        await asyncio.get_running_loop().create_future()
        msg = "The stopped continuation kept running"
        raise AssertionError(msg)

    async with _resumed_streamable_approval(bot, requester_online=True) as claimed:
        with (
            patch.object(runner.approvals, "_continue_entity_call", new=continue_call),
            patch(
                "mindroom.approval.response.approval_manager.get_approval_store",
                return_value=MagicMock(expire_continuation_cards=AsyncMock(return_value=True)),
            ),
        ):
            send = client.room_send.side_effect

            def record_send(**kwargs: object) -> nio.RoomSendResponse:
                if "m.new_content" in cast("dict[str, object]", kwargs["content"]):
                    progress_edited.set()
                return send(**kwargs)

            client.room_send.side_effect = record_send
            lifecycle = asyncio.create_task(
                runner.approvals._run_claimed_approval_lifecycle(
                    claimed,
                    target=_target(thread_id="$thread", reply_to_event_id="$source"),
                ),
            )
            async with asyncio.timeout(5):
                await progress_edited.wait()
            assert await bot._user_stop_reconciler.finalize("$waiting", 7, room_id="!room:localhost")
            ending = await lifecycle

    assert ending == Cancelled("user_stop")
    assert _approval_reply_edits(client) == [
        (STREAM_STATUS_STREAMING, "Checking the report."),
        # The Stop keeps what the reply showed and adds its note.
        (STREAM_STATUS_CANCELLED, "Checking the report.\n\n**[Response cancelled by user]**"),
    ]
    assert await runner.deps.approval_store.approval_continuation(claimed.approval_id) is None


@pytest.mark.asyncio
async def test_missing_approver_denial_stays_neutral_and_wakes_continuation(tmp_path: Path) -> None:
    """Fail-closed automatic denial must not claim that a nonexistent recipient can approve it."""
    bot = _bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    await _admit_approval_source(runner.deps.replies.store)
    request = _plain_request(_target(thread_id="$thread"), source_event_id="$source")
    paused = _ordered_pause(
        PausedAttempt(
            session_id="session-1",
            run_id="run-1",
            requirements=(
                RunRequirement(ToolExecution(tool_call_id="call-denied", tool_name="dangerous", tool_args={})),
            ),
            toolkit_owners={("general", "dangerous"): "test_toolkit"},
        ),
    )
    identity = runner.deps.tool_runtime.build_execution_identity(
        target=request.response_envelope.target,
        user_id=request.user_id,
    )
    retry_sources = Mock()
    runner = replace_response_runner_deps(bot, retry_approval_sources=retry_sources)

    with (
        patch("mindroom.approval.response.resolve_tool_approval_approver", return_value=None),
        patch(
            "mindroom.approval.response.evaluate_tool_approval",
            new=AsyncMock(return_value=(True, 60.0)),
        ),
    ):
        async with _in_reply_span(runner, request, placeholder_event_id="$thinking") as (edit_text, _send_text):
            outcome = await runner.approvals.suspend_for_approval(
                paused,
                request=request,
                target=request.response_envelope.target,
                execution_identity=identity,
                entity_kind="agent",
                history_scope=runner.deps.state_writer.history_scope(),
                show_tool_calls=True,
            )

    continuation = await runner.deps.approval_store.approval_continuation_for_source("$source")
    assert continuation is not None
    assert continuation.state == "ready"
    assert continuation.calls[0].decision is ApprovalDecision.DENIED
    edit_request = edit_text.await_args.args[0]
    assert edit_request.new_text == paused.response_text
    assert edit_request.tool_trace == list(paused.tool_trace)
    retry_sources.assert_called_once_with(request.room_id, ("$source",))
    assert outcome.extra_content == {STREAM_STATUS_KEY: STREAM_STATUS_PENDING}


@pytest.mark.parametrize(
    ("gated_tools", "window_thread_id", "expected_text", "expected_state", "expected_cards"),
    [
        (set(), None, None, "ready", []),
        ({"conditional_write"}, None, "Waiting for approval: `conditional_write`", "waiting", ["conditional_write"]),
        (
            {"conditional_read", "conditional_write"},
            None,
            "Waiting for approval: `conditional_read`, `conditional_write`",
            "waiting",
            ["conditional_read", "conditional_write"],
        ),
        ({"conditional_read", "conditional_write"}, "$thread", None, "ready", []),
        (
            {"conditional_read", "conditional_write"},
            "$other",
            "Waiting for approval: `conditional_read`, `conditional_write`",
            "waiting",
            ["conditional_read", "conditional_write"],
        ),
    ],
    ids=["automatic", "mixed", "human", "auto-approve", "auto-approve-elsewhere"],
)
@pytest.mark.asyncio
async def test_chained_pause_persists_and_publishes_only_human_gated_calls(
    tmp_path: Path,
    gated_tools: set[str],
    window_thread_id: str | None,
    expected_text: str | None,
    expected_state: str,
    expected_cards: list[str],
) -> None:
    """Every chained generation must durably expose only its unresolved calls; the reply thread's window has none."""
    bot = _bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    if window_thread_id is not None:
        start_auto_approve(
            runner.deps.runtime_paths,
            room_id="!room:localhost",
            thread_id=window_thread_id,
            requester_id="@user:localhost",
            duration=timedelta(minutes=5),
        )
    store = runner.deps.replies.store
    await _admit_approval_source(store)
    continuation = approval_continuation(
        approval_id="approval-chain",
        runtime_model_name="default",
    )
    assert await paused_for_approval(store, continuation) is not None
    committed_trace = (
        ToolTraceEntry(
            type="tool_call_started",
            tool_name="conditional_read",
            tool_call_id="call-read",
        ),
        ToolTraceEntry(
            type="tool_call_started",
            tool_name="conditional_write",
            tool_call_id="call-write",
        ),
    )
    paused = PausedAttempt(
        session_id="session-1",
        run_id="run-2",
        runtime_model_name="large",
        requirements=(
            RunRequirement(ToolExecution(tool_call_id="call-read", tool_name="conditional_read", tool_args={})),
            RunRequirement(ToolExecution(tool_call_id="call-write", tool_name="conditional_write", tool_args={})),
        ),
        response_text=("Committed before pause.\n\n🔧 `conditional_read` [1] ⏳\n\n🔧 `conditional_write` [2] ⏳"),
        tool_trace=committed_trace,
        toolkit_owners={
            ("general", "conditional_read"): "test_toolkit",
            ("general", "conditional_write"): "test_toolkit",
        },
    )
    approval_store = MagicMock(
        prepare_reply_card=AsyncMock(return_value=object()),
        reserve_and_publish=AsyncMock(return_value=True),
        transport=FakeApprovalTransport(),
    )
    retry_sources = Mock()
    runner = replace_response_runner_deps(bot, retry_approval_sources=retry_sources)

    async def evaluate(_config: object, _paths: object, tool_name: str, *_args: object) -> tuple[bool, float]:
        return tool_name in gated_tools, 60.0

    target = _target(thread_id="$thread")
    with (
        patch(
            "mindroom.approval.response.resolve_tool_approval_approver",
            return_value="@user:localhost",
        ),
        patch("mindroom.approval.response.evaluate_tool_approval", side_effect=evaluate),
        patch(
            "mindroom.approval.response.approval_manager.get_approval_store",
            return_value=approval_store,
        ),
        _through_the_gateway(runner) as (edit_text, _send_text),
    ):
        # The resume span of the paused reply runs the next generation.
        async with resumed_approval(bot, continuation) as current:
            presentation = await runner.approvals._advance_pause(current, paused, target=target)

    persisted = await store.approvals.approval_continuation(continuation.approval_id)
    assert persisted is not None
    assert persisted.generation == 1
    assert persisted.state == expected_state
    assert persisted.runtime_model_name == "large"
    assert presentation.response_text == paused.response_text
    assert presentation.approval_pending is (expected_text is not None)
    assert presentation.tool_trace == committed_trace
    edit_request = edit_text.await_args.args[0]
    assert edit_request.new_text == paused.response_text
    assert edit_request.tool_trace == list(committed_trace)
    assert edit_request.extra_content == {
        STREAM_STATUS_KEY: STREAM_STATUS_APPROVAL_PENDING if expected_text else STREAM_STATUS_PENDING,
    }
    assert [
        awaited.kwargs["tool_name"] for awaited in approval_store.prepare_reply_card.await_args_list
    ] == expected_cards
    if expected_state == "ready":
        retry_sources.assert_called_once_with(current.room_id, ("$source",))
        restarted = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
        claimed = await claim_continuation(
            restarted.deps.replies.store,
            continuation.approval_id,
            runtime_generation=restarted.deps.approval_runtime_generation,
        )
        assert claimed is not None
        assert (
            await claim_continuation(
                store,
                continuation.approval_id,
                runtime_generation=runner.deps.approval_runtime_generation,
            )
            is None
        )
    else:
        retry_sources.assert_not_called()


@pytest.mark.asyncio
async def test_chained_pause_rejects_an_unanchored_tool_before_persistence(tmp_path: Path) -> None:
    """Every chained pending call must have one exact visible trace slot before journal advance."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    store = runner.deps.replies.store
    await _admit_approval_source(store)
    continuation = approval_continuation(
        approval_id="approval-chain-invalid",
    )
    assert await paused_for_approval(store, continuation) == continuation
    current = await claim_continuation(
        store,
        continuation.approval_id,
        runtime_generation=runner.deps.approval_runtime_generation,
    )
    assert current is not None
    paused = PausedAttempt(
        session_id="session-1",
        run_id="run-2",
        requirements=(
            RunRequirement(ToolExecution(tool_call_id="call-1", tool_name="first", tool_args={})),
            RunRequirement(ToolExecution(tool_call_id="call-2", tool_name="second", tool_args={})),
        ),
        response_text="Before.\n\n🔧 `first` [1] ⏳",
        tool_trace=(
            ToolTraceEntry(
                type="tool_call_started",
                tool_name="first",
                tool_call_id="call-1",
            ),
        ),
        toolkit_owners={("general", "first"): "test_toolkit", ("general", "second"): "test_toolkit"},
    )
    with (
        patch.object(runner.approvals, "_advance_reply_pause", new=AsyncMock()) as reply_pause,
        pytest.raises(RuntimeError, match="ordered presentation"),
    ):
        await runner.approvals._advance_pause(current, paused, target=_target(thread_id="$thread"))

    persisted = await store.approvals.approval_continuation(continuation.approval_id)
    assert persisted is not None
    assert persisted.state == "claimed"
    assert persisted.generation == 0
    reply_pause.assert_not_awaited()


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
        "mindroom.approval.response.resolve_tool_approval_approver",
        return_value="@user:localhost",
    ):
        plan = await plan_approval_calls(
            ((tool, "general"),),
            config=runner.deps.runtime.config,
            runtime_paths=runner.deps.runtime_paths,
            requester_id="@user:localhost",
            toolkit_owners={("general", "native_confirmation"): "test_toolkit"},
            room_id="!room:test",
            thread_id="$thread",
        )

    assert plan.calls[0].decision is None
    assert plan.calls[0].human_approval_required is True


@pytest.mark.asyncio
async def test_policy_confirmation_honors_exact_argument_exemption(tmp_path: Path) -> None:
    """The policy gate's confirmation flag must not turn an exempt policy call into human approval."""
    register_tool_approval_exemption("synthetic_exempt", lambda arguments, _: arguments.get("dry_run") is True)
    bot = _bot(tmp_path)
    bot.config.tool_approval = bot.config.tool_approval.model_copy(update={"default": "require_approval"})
    runner = unwrap_extracted_collaborator(bot._response_runner)
    toolkit = Toolkit(
        name="approval-test",
        tools=[Function(name="synthetic_exempt", entrypoint=lambda: None)],
    )
    function = gated_functions(toolkit, bot.config)["synthetic_exempt"]
    assert function.gate is ToolGate.POLICY
    tool = function.planned_execution("call-exempt", {"dry_run": True})

    with patch(
        "mindroom.approval.response.resolve_tool_approval_approver",
        return_value="@user:localhost",
    ):
        plan = await plan_approval_calls(
            ((tool, "general"),),
            config=runner.deps.runtime.config,
            runtime_paths=runner.deps.runtime_paths,
            requester_id="@user:localhost",
            toolkit_owners={("general", function.name): "test_toolkit"},
            room_id="!room:test",
            thread_id="$thread",
        )

    assert plan.calls[0].decision is ApprovalDecision.APPROVED
    assert plan.waiting_text is None
    assert plan.calls[0].human_approval_required is False


@pytest.mark.asyncio
async def test_policy_confirmation_honors_script_auto_approval(tmp_path: Path) -> None:
    """An exact-call script decision remains authoritative after the policy gate pauses a potentially gated tool."""
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
    function = gated_functions(toolkit, bot.config)["scripted_tool"]
    assert function.gate is ToolGate.POLICY
    tool = function.planned_execution("call-script", {"requires_approval": False})

    with patch(
        "mindroom.approval.response.resolve_tool_approval_approver",
        return_value="@user:localhost",
    ):
        plan = await plan_approval_calls(
            ((tool, "general"),),
            config=runner.deps.runtime.config,
            runtime_paths=runner.deps.runtime_paths,
            requester_id="@user:localhost",
            toolkit_owners={("general", function.name): "test_toolkit"},
            room_id="!room:test",
            thread_id="$thread",
        )

    assert plan.calls[0].decision is ApprovalDecision.APPROVED
    assert plan.waiting_text is None
    assert plan.calls[0].human_approval_required is False


@pytest.mark.asyncio
async def test_recovered_claim_honors_acknowledged_final_outbox_delivery(tmp_path: Path) -> None:
    """An attempted FINAL is recovered and completed without resuming the run twice."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    store = runner.deps.replies.store
    await _admit_approval_source(store)
    continuation = approval_continuation(
        approval_id="approval-final-acked",
        calls=(
            ApprovalCall(
                tool_call_id="call-1",
                tool_name="dangerous",
                invoking_agent="general",
                expires_at_ns=9_000_000_000_000_000_000,
                decision=ApprovalDecision.APPROVED,
            ),
        ),
    )
    assert await paused_for_approval(store, continuation) == continuation
    claimed = await claim_continuation(
        store,
        continuation.approval_id,
        runtime_generation=runner.deps.approval_runtime_generation,
    )
    assert claimed is not None
    await freeze_resume_final(
        store,
        claimed,
        text="finished",
        payload={"body": "finished", "formatted_body": "finished"},
    )
    assert await store.claim_matrix_delivery(delivery_id="$source", stage=DeliveryStage.FINAL) is not None

    async def acknowledge_frozen_final() -> None:
        await store.acknowledge_matrix_delivery(
            delivery_id="$source",
            stage=DeliveryStage.FINAL,
            event_id="$final",
            delivered_projections=(),
        )

    with (
        patch.object(DeliveryGateway, "recover_deliveries", new=AsyncMock(side_effect=acknowledge_frozen_final)),
        patch.object(runner.approvals, "_continue_entity_call", new=AsyncMock()) as continue_entity,
        patch.object(LockedTurns, "build_lifecycle", return_value=MagicMock(finalize=AsyncMock())) as build_lifecycle,
    ):
        await dispatch_approval(
            runner.approvals,
            claimed,
            target=_target(thread_id="$thread", reply_to_event_id="$source"),
        )

    assert await _paused_reply_state(store) is rl.ReplyState.COMPLETED
    continue_entity.assert_not_awaited()
    build_lifecycle.return_value.finalize.assert_awaited_once()
    assert await store.approvals.approval_continuation(continuation.approval_id) is None
    assert not await store.is_pending("$source")


@pytest.mark.asyncio
async def test_recovered_claim_restores_plain_body_and_interactive_metadata(tmp_path: Path) -> None:
    """Restart recovery must replay semantic final facts, not rendered Matrix HTML."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    store = runner.deps.replies.store
    await _admit_approval_source(store)
    continuation = approval_continuation(
        approval_id="approval-final-semantic",
    )
    assert await paused_for_approval(store, continuation) == continuation
    claimed = await claim_continuation(
        store,
        continuation.approval_id,
        runtime_generation=runner.deps.approval_runtime_generation,
    )
    assert claimed is not None
    await freeze_resume_final(
        store,
        claimed,
        text="plain final",
        payload={
            "body": "plain fallback",
            "formatted_body": "<strong>rendered html</strong>",
        },
        result={
            "body": "plain final",
            "interactive": {
                "question_text": "Pick",
                "option_map": {"1": "yes", "✅": "yes"},
                "option_labels": {"1": "Yes", "✅": "Yes"},
                "options_list": [{"emoji": "✅", "label": "Yes", "value": "yes"}],
            },
        },
    )
    assert await store.claim_matrix_delivery(delivery_id="$source", stage=DeliveryStage.FINAL) is not None
    await store.acknowledge_matrix_delivery(
        delivery_id="$source",
        stage=DeliveryStage.FINAL,
        event_id="$final",
        delivered_projections=(),
    )
    lifecycle = MagicMock(finalize=AsyncMock(side_effect=lambda outcome, **_kwargs: outcome))

    with patch.object(LockedTurns, "build_lifecycle", return_value=lifecycle):
        await dispatch_approval(
            runner.approvals,
            claimed,
            target=_target(thread_id="$thread", reply_to_event_id="$source"),
        )

    assert await _paused_reply_state(store) is rl.ReplyState.COMPLETED
    final = lifecycle.finalize.await_args.args[0]
    assert final.final_visible_body == "plain final"
    assert final.interactive_metadata is not None
    assert final.interactive_metadata.question_text == "Pick"
    assert final.option_map == {"1": "yes", "✅": "yes"}


@pytest.mark.asyncio
async def test_original_owner_recovery_retires_acknowledged_failure_without_success_effects(tmp_path: Path) -> None:
    """A frozen failure FINAL must not be replayed through the successful response lifecycle."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    store = runner.deps.replies.store
    await _admit_approval_source(store)
    continuation = approval_continuation(
        approval_id="approval-failure-final",
        state="failing",
        failure=ApprovalFailure(rl.ApprovalFailureKind.ERROR),
    )
    assert await paused_for_approval(store, continuation) == continuation
    await store.enqueue_matrix_delivery(
        delivery_id="$source",
        stage=DeliveryStage.FINAL,
        room_id="!room:localhost",
        thread_id="$thread",
        payload={"body": "Tool approval continuation failed safely."},
        edits_event_id="$waiting",
    )
    assert await store.claim_matrix_delivery(delivery_id="$source", stage=DeliveryStage.FINAL) is not None
    await store.acknowledge_matrix_delivery(
        delivery_id="$source",
        stage=DeliveryStage.FINAL,
        event_id="$failure-edit",
        delivered_projections=(),
    )

    with patch.object(LockedTurns, "build_lifecycle", return_value=MagicMock(finalize=AsyncMock())) as build_lifecycle:
        assert await runner.approvals.recover_approval_final(continuation.approval_id)

    build_lifecycle.assert_not_called()
    assert await store.approvals.approval_continuation(continuation.approval_id) is None
    assert not await store.is_pending("$source")


@pytest.mark.asyncio
async def test_permanently_refused_approval_final_releases_its_sources(tmp_path: Path) -> None:
    """An oversized frozen FINAL is terminal failure, not immortal approval debt."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    store = runner.deps.replies.store
    await _admit_approval_source(store)
    continuation = approval_continuation(
        approval_id="approval-oversized-final",
    )
    assert await paused_for_approval(store, continuation) == continuation
    claimed = await claim_continuation(
        store,
        continuation.approval_id,
        runtime_generation=runner.deps.approval_runtime_generation,
    )
    assert claimed is not None
    await store.enqueue_matrix_delivery(
        delivery_id="$source",
        stage=DeliveryStage.FINAL,
        room_id="!room:localhost",
        thread_id="$thread",
        payload={"body": "oversized final"},
        result={"body": "oversized final"},
        edits_event_id="$waiting",
    )
    assert await store.claim_matrix_delivery(delivery_id="$source", stage=DeliveryStage.FINAL) is not None
    await store.record_permanent_matrix_delivery_failure(
        delivery_id="$source",
        stage=DeliveryStage.FINAL,
        reason="matrix event exceeds the hard size limit",
    )
    edit_text = AsyncMock(side_effect=AssertionError("a failed frozen row must not be reused"))

    with (
        patch.object(DeliveryGateway, "edit_text", new=edit_text),
        patch(
            "mindroom.approval.response.approval_manager.get_approval_store",
            return_value=MagicMock(cards=None, expire_continuation_cards=AsyncMock(return_value=True)),
        ),
    ):
        await dispatch_approval(
            runner.approvals,
            claimed,
            target=_target(thread_id="$thread", reply_to_event_id="$source"),
        )

    assert await _paused_reply_state(store) is rl.ReplyState.FAILED
    edit_text.assert_not_awaited()
    assert await store.approvals.approval_continuation(continuation.approval_id) is None
    assert not await store.is_pending("$source")
    failed = await store.load_matrix_delivery(delivery_id="$source", stage=DeliveryStage.FINAL)
    assert failed is not None
    assert failed.permanently_failed


@pytest.mark.asyncio
async def test_acknowledged_final_wins_cancellation_before_delivery_returns(tmp_path: Path) -> None:
    """A visible successful FINAL must complete even if the live caller is cancelled afterward."""
    bot = _bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    store = runner.deps.replies.store
    await _admit_approval_source(store)
    continuation = approval_continuation(
        approval_id="approval-final-cancelled-return",
    )
    assert await paused_for_approval(store, continuation) is not None

    async def acknowledge_then_cancel(*_args: object, **_kwargs: object) -> tuple[object, object]:
        current = await store.approvals.approval_continuation(continuation.approval_id)
        assert current is not None
        await freeze_resume_final(store, current, text="plain final", payload={"body": "plain final"})
        assert await store.claim_matrix_delivery(delivery_id="$source", stage=DeliveryStage.FINAL) is not None
        await store.acknowledge_matrix_delivery(
            delivery_id="$source",
            stage=DeliveryStage.FINAL,
            event_id="$final",
            delivered_projections=(),
        )
        raise asyncio.CancelledError

    lifecycle = MagicMock(finalize=AsyncMock(side_effect=lambda outcome, **_kwargs: outcome))
    async with resumed_approval(bot, continuation) as claimed:
        with (
            patch.object(runner.approvals, "_execute_claimed_approval", side_effect=acknowledge_then_cancel),
            patch.object(LockedTurns, "build_lifecycle", return_value=lifecycle),
            patch.object(runner.approvals, "_approval_post_response_outcome", return_value=ResponseOutcome()),
        ):
            ending = await runner.approvals._run_claimed_approval_lifecycle(
                claimed,
                target=_target(thread_id="$thread", reply_to_event_id="$source"),
            )

    assert ending == Completed()
    assert await _paused_reply_state(store) is rl.ReplyState.COMPLETED
    assert await store.approvals.approval_continuation(continuation.approval_id) is None
    finalized = [call.args[0] for call in lifecycle.finalize.await_args_list]
    assert [item.ending for item in finalized] == [Completed()]


@pytest.mark.asyncio
async def test_acknowledged_final_wins_cancellation_after_lifecycle_delivery(tmp_path: Path) -> None:
    """Late lifecycle cancellation must adopt the visible success before failure fencing."""
    bot = _bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    store = runner.deps.replies.store
    request = _plain_request(_target(thread_id="$thread"), source_event_id="$source")
    await _admit_approval_source(store)
    continuation = approval_continuation(
        approval_id="approval-final-late-cancel",
    )
    assert await paused_for_approval(store, continuation) is not None
    await bot._take_over_replies()

    async def acknowledge_then_cancel(claimed: ApprovalContinuation, **_kwargs: object) -> None:
        await freeze_resume_final(store, claimed, text="plain final", payload={"body": "plain final"})
        assert claimed.state == "claimed"
        assert await store.claim_matrix_delivery(delivery_id="$source", stage=DeliveryStage.FINAL) is not None
        await store.acknowledge_matrix_delivery(
            delivery_id="$source",
            stage=DeliveryStage.FINAL,
            event_id="$final",
            delivered_projections=(),
        )
        raise asyncio.CancelledError

    lifecycle = MagicMock(finalize=AsyncMock(side_effect=lambda outcome, **_kwargs: outcome))
    with (
        patch.object(runner.approvals, "_run_claimed_approval_lifecycle", side_effect=acknowledge_then_cancel),
        patch.object(LockedTurns, "build_lifecycle", return_value=lifecycle),
        patch.object(runner.approvals, "_approval_post_response_outcome", return_value=ResponseOutcome()),
    ):
        fate = await runner._run_owned_or_locked_response(
            request,
            target=request.response_envelope.target,
            locked_operation=AsyncMock(return_value=Unclaimed(UnclaimedReason.NOTHING_TO_RUN)),
        )

    assert fate == Owned()
    assert await _paused_reply_state(store) is rl.ReplyState.COMPLETED
    assert await store.approvals.approval_continuation(continuation.approval_id) is None
    assert not await store.is_pending("$source")


@pytest.mark.asyncio
async def test_recovered_claim_keeps_unacknowledged_final_recoverable(tmp_path: Path) -> None:
    """An inconclusive FINAL retry must retain the exact paused-run ownership."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    store = runner.deps.replies.store
    await _admit_approval_source(store)
    continuation = approval_continuation(
        approval_id="approval-final-unacknowledged",
        calls=(
            ApprovalCall(
                tool_call_id="call-1",
                tool_name="dangerous",
                invoking_agent="general",
                expires_at_ns=9_000_000_000_000_000_000,
                decision=ApprovalDecision.APPROVED,
            ),
        ),
    )
    assert await paused_for_approval(store, continuation) == continuation
    claimed = await claim_continuation(
        store,
        continuation.approval_id,
        runtime_generation=runner.deps.approval_runtime_generation,
    )
    assert claimed is not None
    await store.enqueue_matrix_delivery(
        delivery_id="$source",
        stage=DeliveryStage.FINAL,
        room_id="!room:localhost",
        thread_id="$thread",
        payload={"body": "finished", "formatted_body": "finished"},
        edits_event_id="$waiting",
    )
    assert await store.claim_matrix_delivery(delivery_id="$source", stage=DeliveryStage.FINAL) is not None

    with patch.object(DeliveryGateway, "recover_deliveries", new=AsyncMock()):
        await dispatch_approval(
            runner.approvals,
            claimed,
            target=_target(thread_id="$thread", reply_to_event_id="$source"),
        )

    retained = await store.approvals.approval_continuation(continuation.approval_id)
    assert retained is not None
    assert retained.state == "claimed"
    assert retained.runtime_generation == runner.deps.approval_runtime_generation
    assert await store.is_pending("$source")
    final = await store.load_matrix_delivery(delivery_id="$source", stage=DeliveryStage.FINAL)
    assert final is not None
    assert final.attempted
    assert final.acknowledged_event_id is None


@pytest.mark.asyncio
async def test_team_approval_resume_reuses_persisted_member_models(tmp_path: Path) -> None:
    """A resumed team must rebuild members from the turn's pinned aliases."""
    bot = _bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    await _admit_approval_source(runner.deps.replies.store)
    target = _target(thread_id="$thread")
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@user:localhost",
        room_id=target.room_id,
        thread_id=target.resolved_thread_id,
        resolved_thread_id=target.resolved_thread_id,
        session_id="session-1",
        transport_agent_name="general",
    )
    continuation = approval_continuation(
        approval_id="approval-team-model-resume",
        run_id="run-paused",
        entity_kind="team",
        room_id=target.room_id,
        thread_id=target.resolved_thread_id,
        execution_identity=identity,
        runtime_model_name="large",
        team_member_names=("general",),
        team_member_model_names=(("general", "large"),),
        team_mode="coordinate",
    )
    assert await paused_for_approval(runner.deps.replies.store, continuation) is not None
    continued = create_autospec(
        continue_paused_team_run,
        return_value=CompletedApprovalRun(response_text="done", metadata_content={}),
    )

    with (
        patch.object(
            runner.deps.tool_runtime,
            "build_turn_context",
            return_value=turn_tool_context(runner.deps.runtime.config, runner.deps.runtime_paths, identity),
        ),
        patch("mindroom.approval.continuation_runner.continue_paused_team_run", new=continued),
        patch("mindroom.reply_turn.request.typing_indicator", _noop_typing),
    ):
        async with resumed_approval(bot, continuation) as claimed:
            result = await runner.approvals._continue_entity_call(
                claimed,
                request=_plain_request(target, source_event_id="$source"),
                target=target,
                tool_trace_collector=[],
                progress=None,
                cli_approval_handler=None,
            )

    assert isinstance(result, CompletedApprovalRun)
    assert continued.await_args.kwargs.get("member_model_names") == {"general": "large"}


@pytest.mark.asyncio
async def test_approval_request_restores_exact_hook_envelope_after_store_reload(tmp_path: Path) -> None:
    """Resume hooks must observe the same ingress identity and correlation as suspension."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    store = runner.deps.replies.store
    await _admit_approval_source(store)
    original_envelope = replace(
        _envelope(_target(thread_id="$thread"), source_event_id="$source"),
        mentioned_agents=("research", "general"),
        hook_source="plugin:message_received",
        dispatch_policy_source_kind="plugin",
        message_received_depth=3,
    )
    continuation = approval_continuation(
        approval_id="approval-hook-context",
        request_body=original_envelope.body,
        origin=original_envelope.origin,
        mentioned_agents=original_envelope.mentioned_agents,
        hook_source=original_envelope.hook_source,
        dispatch_policy_source_kind=original_envelope.dispatch_policy_source_kind,
        message_received_depth=original_envelope.message_received_depth,
        correlation_id="correlation-original",
    )
    assert await paused_for_approval(store, continuation) == continuation
    reloaded = await store.approvals.approval_continuation(continuation.approval_id)
    assert reloaded is not None

    restored = runner.approvals.approval_response_request(
        reloaded,
        target=_target(thread_id="$thread", reply_to_event_id="$source"),
    )

    assert restored.sources == ResponseSources(
        pending_event_ids=("$source",),
        logical_source_event_ids=("$source",),
    )
    assert restored.correlation_id == "correlation-original"
    assert restored.response_envelope.mentioned_agents == ("research", "general")
    assert restored.response_envelope.hook_source == "plugin:message_received"
    assert restored.response_envelope.dispatch_policy_source_kind == "plugin"
    assert restored.response_envelope.message_received_depth == 3


def test_resumed_approval_tool_context_keeps_the_turns_hook_source(tmp_path: Path) -> None:
    """Tools of a resumed turn see the hook that started it, as they did before the approval."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    target = _target(thread_id="$thread", reply_to_event_id="$source")

    def tool_context(hook_source: str | None) -> ToolRuntimeContext:
        continuation = approval_continuation(approval_id="approval-hook-source", hook_source=hook_source)
        request = runner.approvals.approval_response_request(continuation, target=target)
        context = runner.deps.tool_runtime.build_context(
            target,
            user_id=continuation.requester_id,
            source_envelope=request.response_envelope,
        )
        assert context is not None
        return context

    assert tool_context("slack-watch/w-1").hook_source == "slack-watch/w-1"
    assert tool_context(None).hook_source is None


@pytest.mark.asyncio
async def test_continuation_tool_context_preserves_original_correlation_id(tmp_path: Path) -> None:
    """Resumed tool hooks and runtime events stay correlated with the originating Matrix turn."""
    bot = _bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    await _admit_approval_source(runner.deps.replies.store)
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@user:localhost",
        room_id="!room:localhost",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-1",
        transport_agent_name="general",
    )
    continuation = approval_continuation(
        approval_id="approval-correlation",
        calls=(
            ApprovalCall(
                tool_call_id="call-1",
                tool_name="publish_report",
                invoking_agent="general",
                expires_at_ns=1,
                decision=ApprovalDecision.APPROVED,
                human_approval_required=True,
            ),
        ),
        execution_identity=identity,
        correlation_id="correlation-original",
    )
    assert await paused_for_approval(runner.deps.replies.store, continuation) is not None
    request = replace(
        _plain_request(_target(thread_id="$thread"), source_event_id="$source"),
        correlation_id="correlation-original",
    )
    observed: list[str | None] = []
    receipts: list[str] = []
    progress = AsyncMock()
    forwarded_progress: list[object] = []

    def build_turn_context(
        *_args: object,
        correlation_id: str | None = None,
        **_kwargs: object,
    ) -> ToolRuntimeContext:
        observed.append(correlation_id)
        return turn_tool_context(runner.deps.runtime.config, runner.deps.runtime_paths, identity)

    async def continue_run(*_args: object, **kwargs: object) -> CompletedApprovalRun:
        forwarded_progress.append(kwargs["progress"])
        receipt = approval_receipt._context.get()
        assert receipt is not None
        # The paused run the continuation names reads the receipt.
        assert set(receipt.readers) == {continuation.run_id}
        receipts.append(build_approval_receipt(receipt.readers[continuation.run_id]))
        return CompletedApprovalRun(response_text="done", metadata_content={})

    with (
        patch.object(runner.deps.tool_runtime, "build_turn_context", side_effect=build_turn_context),
        patch(
            "mindroom.approval.execution.AgentApprovalExecution.continue_run",
            new=continue_run,
        ),
    ):
        async with resumed_approval(bot, continuation) as claimed:
            await runner.approvals._continue_entity_call(
                claimed,
                request=request,
                target=request.response_envelope.target,
                tool_trace_collector=[],
                progress=progress,
                cli_approval_handler=None,
            )

    assert observed == ["correlation-original"]
    assert forwarded_progress == [progress]
    assert receipts == [
        "[SYSTEM NOTICE — TOOL APPROVAL RECEIPT] This trusted MindRoom runtime receipt records how "
        "paused tool calls were authorized. Do not infer approval policy from tool success alone.\n"
        "- `publish_report` (tool call id `call-1`): human approval was required and granted before execution, "
        "directly or through a matching timed approval window.",
    ]


@pytest.mark.asyncio
async def test_a_chained_approval_s_receipt_records_every_call_the_run_s_approvals_decided(tmp_path: Path) -> None:
    """After two approvals in a row, the resume's receipt records the first approval's call beside the second's.

    A reader the first approval did not resume, such as a member the second approval decides, sees only the second
    receipt, so that receipt alone says how the first approval's call was authorized.
    """
    bot = _bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    store = runner.deps.replies.store
    await _admit_approval_source(store)
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@user:localhost",
        room_id="!room:localhost",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-1",
        transport_agent_name="general",
    )

    def approved(tool_call_id: str, tool_name: str, *, human_approval_required: bool) -> ApprovalCall:
        return ApprovalCall(
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            invoking_agent="general",
            expires_at_ns=1,
            decision=ApprovalDecision.APPROVED,
            human_approval_required=human_approval_required,
        )

    first = approval_continuation(
        approval_id="approval-chain",
        calls=(approved("call-1", "save_file", human_approval_required=True),),
        execution_identity=identity,
    )
    assert await paused_for_approval(store, first) is not None
    claimed = await claim_continuation(
        store,
        first.approval_id,
        runtime_generation=runner.deps.approval_runtime_generation,
    )
    assert claimed is not None
    second_calls = (
        approved("call-2", "save_file", human_approval_required=True),
        approved("call-3", "read_file", human_approval_required=False),
    )
    advanced = await advance_continuation(
        store,
        first.approval_id,
        claimant_generation=claimed.generation,
        run_id=claimed.run_id,
        session_id=claimed.session_id,
        calls=second_calls,
    )
    assert advanced is not None
    second = await store.approvals.activate_approval_continuation(
        first.approval_id,
        expected_generation=advanced.generation,
    )
    assert second is not None
    assert (second.state, second.generation, second.calls) == ("ready", 1, second_calls)
    request = _plain_request(_target(thread_id="$thread"), source_event_id="$source")
    receipts: list[str] = []

    async def continue_run(*_args: object, **_kwargs: object) -> CompletedApprovalRun:
        receipt_calls = take_unsent_receipt(second.run_id, lambda call_id: call_id)
        assert receipt_calls is not None
        receipts.append(build_approval_receipt(receipt_calls))
        return CompletedApprovalRun(response_text="done", metadata_content={})

    with (
        patch.object(
            runner.deps.tool_runtime,
            "build_turn_context",
            return_value=turn_tool_context(runner.deps.runtime.config, runner.deps.runtime_paths, identity),
        ),
        patch("mindroom.approval.execution.AgentApprovalExecution.continue_run", new=continue_run),
    ):
        async with resumed_approval(bot, second) as resumed:
            await runner.approvals._continue_entity_call(
                resumed,
                request=request,
                target=request.response_envelope.target,
                tool_trace_collector=[],
                progress=None,
                cli_approval_handler=None,
            )

    granted = (
        "human approval was required and granted before execution, directly or through a matching timed approval "
        "window."
    )
    assert receipts == [
        "[SYSTEM NOTICE — TOOL APPROVAL RECEIPT] This trusted MindRoom runtime receipt records how "
        "paused tool calls were authorized. Do not infer approval policy from tool success alone.\n"
        f"- `save_file` (tool call id `call-1`): {granted}\n"
        f"- `save_file` (tool call id `call-2`): {granted}\n"
        "- `read_file` (tool call id `call-3`): human approval was not required; policy approved execution.",
    ]


@pytest.mark.asyncio
async def test_suspension_rejects_missing_requester_before_persistence(tmp_path: Path) -> None:
    """A continuation without the original session user cannot be resumed against the same run."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    request = replace(_plain_request(_target()), user_id=None)
    paused = PausedAttempt(
        session_id="session-1",
        run_id="run-1",
        requirements=(
            RunRequirement(ToolExecution(tool_call_id="call-1", tool_name="dangerous", requires_confirmation=True)),
        ),
        toolkit_owners={("general", "dangerous"): "test_toolkit"},
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
        await runner.approvals.suspend_for_approval(
            paused,
            request=request,
            target=_target(),
            execution_identity=identity,
            entity_kind="agent",
            history_scope=runner.deps.state_writer.history_scope(),
            show_tool_calls=True,
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
    coordinator = runner_for(
        replace(
            unwrap_extracted_collaborator(bot._response_runner).deps,
            resolver=resolver,
            request_preparer=request_preparer,
        ),
    )

    target = _target(thread_id="$thread", reply_to_event_id="$event1")
    envelope = _envelope(target, source_event_id="$event1")
    request = ResponseRequest(
        sources=ResponseSources(
            pending_event_ids=(envelope.source_event_id,),
            logical_source_event_ids=(envelope.source_event_id,),
        ),
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
        timezone="UTC",
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
# 2. Attempt mechanics: placeholder, failures, and suspensions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_adopted_placeholder_is_passed_to_the_response_function() -> None:
    """The event the turn already made visible is what the attempt generates against."""
    runner = _attempt_runner()
    seen: list[str | None] = []

    async def respond(message_id: str | None) -> None:
        seen.append(message_id)

    await runner.run(
        ResponseAttemptRequest(
            target=_target(),
            response_function=respond,
            span=_Span().attempt(),
            existing_event_id="$placeholder",
        ),
    )

    assert seen == ["$placeholder"]


@pytest.mark.asyncio
async def test_generation_failure_reraises() -> None:
    """Generation failures re-raise to the response lifecycle that settles the reply."""
    runner = _attempt_runner()

    async def respond(_message_id: str | None) -> None:
        msg = "generation exploded"
        raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="generation exploded"):
        await runner.run(
            ResponseAttemptRequest(
                target=_target(),
                response_function=respond,
                span=_Span().attempt(),
                existing_event_id="$placeholder",
            ),
        )


@pytest.mark.asyncio
async def test_approval_suspension_is_not_logged_as_generation_failure() -> None:
    """A native pause is a lifecycle handoff, not an exceptional generation failure."""
    logger = MagicMock()
    runner = ResponseAttemptRunner(replace(_attempt_runner().deps, logger=logger))
    suspension = ResponsePausedForApproval(
        PausedAttempt(
            session_id="session-1",
            run_id="run-paused",
            requirements=(
                RunRequirement(ToolExecution(tool_call_id="call-1", tool_name="dangerous", requires_confirmation=True)),
            ),
            toolkit_owners={("general", "dangerous"): "test_toolkit"},
        ),
    )

    async def respond(_message_id: str | None) -> None:
        raise suspension

    with pytest.raises(ResponsePausedForApproval) as raised:
        await runner.run(
            ResponseAttemptRequest(
                target=_target(),
                response_function=respond,
                span=_Span().attempt(),
                existing_event_id="$placeholder",
            ),
        )

    assert raised.value is suspension
    logger.exception.assert_not_called()


# ---------------------------------------------------------------------------
# 3. Cancellation: user stop mid-generation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_stop_mid_generation_cancels_the_span_task() -> None:
    """A Stop on the reply span mid-generation cancels the attempt, which records the outcome and keeps its event."""
    runner = _attempt_runner()
    spans = SpanRegistry()
    spans.expect("span-1")
    started = asyncio.Event()
    attempt_tasks: list[asyncio.Task[None] | None] = []

    async def respond(_message_id: str | None) -> None:
        attempt_tasks.append(asyncio.current_task())
        started.set()
        await asyncio.Event().wait()

    run_task = asyncio.create_task(
        runner.run(
            ResponseAttemptRequest(
                target=_target(),
                response_function=respond,
                span=SpanAttempt(
                    register=lambda task: spans.register("span-1", task),
                    add_stop_button=AsyncMock(),
                    reply_event=AsyncMock(),
                ),
                existing_event_id="$placeholder",
            ),
        ),
    )
    await asyncio.wait_for(started.wait(), timeout=2)

    assert spans.cancel("span-1", cancel_source="user_stop") is True
    # The attempt survives the cancellation and finishes, reporting the Stop as its ending.
    assert await asyncio.wait_for(run_task, timeout=2) == Cancelled("user_stop")

    [attempt_task] = attempt_tasks
    assert attempt_task is not None
    assert attempt_task.cancelled()
    assert spans.live_span_ids() == frozenset()


# ---------------------------------------------------------------------------
# 4. Streaming vs non-streaming delivery through DeliveryGateway
# ---------------------------------------------------------------------------


@contextmanager
def _agent_turn(stream: Callable[..., AsyncIterator[object]] | None, *, streaming: bool) -> Iterator[None]:
    """Run agent turns, streamed or delivered whole, below the placeholder each turn sends first.

    ``stream`` stands in for the agent's turn stream; without it the turn runs its real driver.
    """
    changes: dict[str, object] = {} if stream is None else {"stream_agent_response": stream}
    with (
        patch.object(DeliveryGateway, "send_text", new=AsyncMock(return_value="$placeholder")),
        patch_response_runner_module(
            **changes,
            should_use_streaming=AsyncMock(return_value=streaming),
            typing_indicator=_noop_typing,
            apply_post_response_effects=AsyncMock(),
        ),
    ):
        yield


@contextmanager
def _delivery_starts(steps: list[str]) -> Iterator[None]:
    """Log each time a turn notes that its delivery began."""
    note_delivery_started = TurnProgress.note_delivery_started

    def note(progress: object, event_id: str | None = None) -> None:
        steps.append("started")
        note_delivery_started(progress, event_id)  # type: ignore[arg-type]

    with patch.object(TurnProgress, "note_delivery_started", new=note):
        yield


def _consuming_delivery(transport: StreamTransportOutcome) -> AsyncMock:
    """Return a ``deliver_stream`` that reads the whole response stream, then reports ``transport``."""

    async def deliver(request: StreamingDeliveryRequest) -> StreamTransportOutcome:
        async for _chunk in request.response_stream:
            pass
        return transport

    return AsyncMock(side_effect=deliver)


def _paused_attempt() -> ResponsePausedForApproval:
    return ResponsePausedForApproval(
        PausedAttempt(
            session_id="session-1",
            run_id="run-paused",
            requirements=(
                RunRequirement(ToolExecution(tool_call_id="call-1", tool_name="dangerous", requires_confirmation=True)),
            ),
            toolkit_owners={("general", "dangerous"): "test_toolkit"},
        ),
    )


def _suspended_outcome() -> FinalDeliveryOutcome:
    return FinalDeliveryOutcome(ending=AwaitingApproval(), event_id="$placeholder", is_visible_response=True)


@pytest.mark.asyncio
async def test_non_streaming_response_delivers_through_deliver_final(tmp_path: Path) -> None:
    """The non-streaming path hands the generated text to DeliveryGateway.deliver_final once."""
    bot = _bot(tmp_path)
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    deliver_final = AsyncMock(return_value=completed_outcome("$response", body="final text"))

    with (
        patch.object(DeliveryGateway, "deliver_final", new=deliver_final),
        _agent_turn(streamed_answer(AsyncMock(return_value="final text")), streaming=False),
    ):
        assert await coordinator.generate_response(_plain_request(_target())) == Owned()

    deliver_final.assert_awaited_once()
    final_request = deliver_final.await_args.args[0]
    assert final_request.response_text == "final text"
    assert final_request.target.room_id == "!room:localhost"
    assert final_request.identity.response_kind == "ai"
    assert final_request.existing_event_id is None


@pytest.mark.asyncio
async def test_non_streaming_final_delivery_exposes_fixed_shutdown_phase(tmp_path: Path) -> None:
    """The real non-streaming gateway boundary is visible while it retains ownership."""
    coordinator = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    finalization_started = asyncio.Event()
    finalization_cancelled = asyncio.Event()
    release_finalization = asyncio.Event()

    async def retained_before_response(**_kwargs: object) -> object:
        finalization_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            finalization_cancelled.set()
            await release_finalization.wait()
            raise

    coordinator.deps.delivery_gateway.deps.response_hooks.apply_before_response = AsyncMock(
        side_effect=retained_before_response,
    )
    with _agent_turn(streamed_answer(AsyncMock(return_value="final text")), streaming=False):
        response_task = coordinator.inbox.track_inbox_response(
            coordinator.generate_response(_plain_request(_target())),
            name="test_non_streaming_final_delivery_phase",
            recovery_proof_ready=lambda: True,
            room_id=_target().room_id,
        )
        await finalization_started.wait()
        coordinator.inbox.begin_process_shutdown()
        await finalization_cancelled.wait()

        try:
            assert coordinator.inbox.pending_response_phase_counts == {"final_delivery": 1}
        finally:
            release_finalization.set()

        assert await coordinator.inbox.drain_inbox_responses(
            cancel_after_seconds=0.1,
            shutdown_intent=ORDERLY_SHUTDOWN,
        )
        await asyncio.gather(response_task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_process_shutdown_skips_interrupted_replay_persistence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    streaming: bool,
) -> None:
    """Orderly stop leaves the durable pending turn for the successor's replay instead of recording it stopped."""
    coordinator = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    generation_started = asyncio.Event()

    async def blocked_generation() -> None:
        generation_started.set()
        await asyncio.Event().wait()

    install_model_providers(monkeypatch, {"default": ScriptedModel([[Await(blocked_generation)]])})
    persist = MagicMock()
    with (
        patch("mindroom.response_turn.persist_interrupted_replay_snapshot", new=persist),
        patch.object(DeliveryGateway, "send_text", new=AsyncMock(return_value="$placeholder")),
        patch_response_runner_module(
            should_use_streaming=AsyncMock(return_value=streaming),
            typing_indicator=_noop_typing,
            apply_post_response_effects=AsyncMock(),
        ),
    ):
        task = asyncio.create_task(coordinator.generate_response(_plain_request(_target())))
        await asyncio.wait_for(generation_started.wait(), timeout=SETTLE_TIMEOUT_SECONDS)
        request_task_cancel(task, process_shutdown=True)
        with pytest.raises(asyncio.CancelledError):
            await task

    persist.assert_not_called()


@pytest.mark.asyncio
async def test_process_shutdown_blocks_terminal_delivery_after_non_streaming_generation_consumes_cancel(
    tmp_path: Path,
) -> None:
    """A provider that consumes process cancellation must still leave the durable turn for replay."""
    coordinator = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    generation_started = asyncio.Event()
    deliver_final = AsyncMock(return_value=completed_outcome())

    async def cancellation_resistant_generation(*_args: object, **_kwargs: object) -> AsyncIterator[str]:
        generation_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            assert current_task_is_process_shutdown()
        yield "late response"

    with (
        patch.object(DeliveryGateway, "deliver_final", new=deliver_final),
        _agent_turn(cancellation_resistant_generation, streaming=False),
    ):
        task = asyncio.create_task(coordinator.generate_response(_plain_request(_target())))
        await generation_started.wait()
        request_task_cancel(task, process_shutdown=True)
        with pytest.raises(asyncio.CancelledError):
            await task

    deliver_final.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_approval_pause_reaches_the_lifecycle_handler_without_an_error_log(
    tmp_path: Path,
    streaming: bool,
) -> None:
    """A native approval pause, streamed or delivered whole, reaches the suspension handler without an error log."""
    bot = _bot(tmp_path)
    coordinator = replace_response_runner_deps(bot, logger=MagicMock())
    pause = _paused_attempt()
    suspend = AsyncMock(return_value=_suspended_outcome())
    finalize = AsyncMock()

    async def paused_stream(*_args: object, **_kwargs: object) -> AsyncIterator[str]:
        yield "Checking"
        raise pause

    with (
        patch.object(coordinator.approvals, "suspend_for_approval", new=suspend),
        patch.object(DeliveryGateway, "deliver_stream", new=_consuming_delivery(stream_transport())),
        patch.object(DeliveryGateway, "finalize_streamed_response", new=finalize),
        _agent_turn(paused_stream, streaming=streaming),
    ):
        await coordinator.generate_response(_plain_request(_target()))

    suspend.assert_awaited_once()
    assert suspend.await_args.args[0].run_id == "run-paused"
    finalize.assert_not_awaited()
    coordinator.deps.logger.exception.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("streaming", "scheduled_model", "expected_model"),
    [(False, None, "large"), (True, None, "large"), (True, "cheap", "cheap")],
)
async def test_agent_turn_reuses_prepared_room_model_after_override_change(
    tmp_path: Path,
    streaming: bool,
    scheduled_model: str | None,
    expected_model: str,
) -> None:
    """An agent turn must not re-read a room default that changed after its runtime was prepared."""
    bot = _bot(tmp_path)
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    config = coordinator.deps.runtime.config
    config.models["large"] = ModelConfig(provider="openai", id="large-model")
    config.models["cheap"] = ModelConfig(provider="openai", id="cheap-model")
    set_room_model_override(
        coordinator.deps.runtime_paths,
        room_id="!room:localhost",
        model_name="large",
    )
    original_prepare = coordinator.prepare_response_runtime
    active_models: list[str | None] = []

    async def prepare_then_change_room_default(*args: object, **kwargs: object) -> object:
        runtime = await original_prepare(*args, **kwargs)  # type: ignore[arg-type]
        set_room_model_override(
            coordinator.deps.runtime_paths,
            room_id="!room:localhost",
            model_name="default",
        )
        return runtime

    async def fake_stream(ctx: ResponseTurnContext, *_args: object, **_kwargs: object) -> AsyncIterator[str]:
        active_models.append(ctx.active_model_name)
        yield "final text"

    with (
        patch.object(coordinator, "prepare_response_runtime", new=prepare_then_change_room_default),
        patch.object(
            DeliveryGateway,
            "deliver_final",
            new=AsyncMock(return_value=completed_outcome("$response", body="final text")),
        ),
        patch.object(DeliveryGateway, "deliver_stream", new=_consuming_delivery(stream_transport())),
        patch.object(
            DeliveryGateway,
            "finalize_streamed_response",
            new=AsyncMock(return_value=completed_outcome("$stream", body="final text")),
        ),
        _agent_turn(fake_stream, streaming=streaming),
    ):
        await coordinator.generate_response(replace(_plain_request(_target()), scheduled_model=scheduled_model))

    assert active_models == [expected_model]


@pytest.mark.asyncio
async def test_agent_turn_finds_its_rooms_redactions_in_the_journal(tmp_path: Path) -> None:
    """The turn driver's redaction lookup reads the tombstones the bot's own journal recorded for the room."""
    bot = _bot(tmp_path)
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    store = coordinator.deps.replies.store
    await _admit_approval_source(store, event_id="$older")
    await store.admit(
        InboundEvent(
            event_id="$redaction",
            room_id="!room:localhost",
            thread_id=None,
            kind=EventKind.REDACTION,
            event_class=EventClass.CONTEXT_ONLY,
            sender="@user:localhost",
            origin_server_ts=2,
            source={"event_id": "$redaction", "redacts": "$older", "content": {}},
        ),
        ProjectedEvent(
            event_id="$redaction",
            room_id="!room:localhost",
            thread_id=None,
            sender="@user:localhost",
            origin_server_ts=2,
            content={},
            replaces_event_id=None,
            redacts_event_id="$older",
        ),
    )
    found: list[Mapping[str, str | None]] = []

    async def fake_stream(ctx: ResponseTurnContext, *_args: object, **_kwargs: object) -> AsyncIterator[str]:
        assert ctx.redacted_history_events is not None
        found.append(await ctx.redacted_history_events(("$older", "$kept")))
        yield "final text"

    with (
        patch.object(
            DeliveryGateway,
            "deliver_final",
            new=AsyncMock(return_value=completed_outcome("$response", body="final text")),
        ),
        _agent_turn(fake_stream, streaming=False),
    ):
        await coordinator.generate_response(_plain_request(_target()))

    assert found == [{"$older": None}]


@pytest.mark.asyncio
async def test_agent_model_snapshot_precedes_locked_turn_preparation(tmp_path: Path) -> None:
    """A room-default change during locked preparation must wait for the next turn."""
    bot = _bot(tmp_path)
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    config = coordinator.deps.runtime.config
    config.models["large"] = ModelConfig(provider="openai", id="large-model")
    set_room_model_override(
        coordinator.deps.runtime_paths,
        room_id="!room:localhost",
        model_name="large",
    )
    original_prepare = coordinator._prepare_claimed_request
    active_models: list[str | None] = []
    prepared: list[bool] = []

    async def change_room_default_during_preparation(*args: object, **kwargs: object) -> ResponseRequest | SourceFate:
        set_room_model_override(
            coordinator.deps.runtime_paths,
            room_id="!room:localhost",
            model_name="default",
        )
        prepared.append(True)
        return await original_prepare(*args, **kwargs)  # type: ignore[arg-type]

    async def fake_stream(ctx: ResponseTurnContext, *_args: object, **_kwargs: object) -> AsyncIterator[str]:
        active_models.append(ctx.active_model_name)
        yield "final text"

    with (
        patch.object(coordinator, "_prepare_claimed_request", new=change_room_default_during_preparation),
        patch.object(
            DeliveryGateway,
            "deliver_final",
            new=AsyncMock(return_value=completed_outcome("$response", body="final text")),
        ),
        _agent_turn(fake_stream, streaming=False),
    ):
        await coordinator.generate_response(_plain_request(_target()))

    assert prepared == [True]
    assert active_models == ["large"]


@pytest.mark.asyncio
async def test_non_streaming_invisible_delivery_does_not_mark_substantive_reply(tmp_path: Path) -> None:
    """A failed final delivery must not turn a thinking placeholder into a substantive reply."""
    bot = _bot(tmp_path)
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    delivery = FinalDeliveryOutcome(ending=Failed(FailureCause.DELIVERY_FAILED))
    deliver_final = AsyncMock(return_value=delivery)
    timing = DispatchPipelineTiming(source_event_id="$request", room_id="!room:localhost")
    timing.mark_first_visible_reply("placeholder")
    request = replace(_plain_request(_target()), pipeline_timing=timing)

    with (
        patch.object(DeliveryGateway, "deliver_final", new=deliver_final),
        _agent_turn(streamed_answer(AsyncMock(return_value="final text")), streaming=False),
    ):
        await coordinator.generate_response(request)

    deliver_final.assert_awaited_once()
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
        ending=Failed(FailureCause.DELIVERY_FAILED),
        event_id="$existing",
        is_visible_response=True,
    )
    deliver_final = AsyncMock(return_value=delivery)
    timing = DispatchPipelineTiming(source_event_id="$request", room_id="!room:localhost")
    request = replace(
        _plain_request(_target()),
        existing_event_id="$existing",
        pipeline_timing=timing,
    )

    with (
        patch.object(DeliveryGateway, "deliver_final", new=deliver_final),
        _agent_turn(streamed_answer(AsyncMock(return_value="replacement text")), streaming=False),
    ):
        await coordinator.generate_response(request)

    deliver_final.assert_awaited_once()
    assert "first_substantive_reply" not in timing.marks
    assert "first_substantive_kind" not in timing.metadata


@pytest.mark.asyncio
async def test_streaming_response_streams_then_finalizes_through_gateway(tmp_path: Path) -> None:
    """The streaming path delivers via deliver_stream, then finalizes the same transport outcome."""
    bot = _bot(tmp_path)
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    transport = stream_transport()
    deliver_stream = _consuming_delivery(transport)
    finalize = AsyncMock(return_value=completed_outcome("$stream", body="streamed body"))

    async def fake_stream(*_args: object, **_kwargs: object) -> AsyncIterator[str]:
        yield "chunk"

    with (
        patch.object(DeliveryGateway, "deliver_stream", new=deliver_stream),
        patch.object(DeliveryGateway, "finalize_streamed_response", new=finalize),
        _agent_turn(fake_stream, streaming=True),
    ):
        assert await coordinator.generate_response(_plain_request(_target())) == Owned()

    deliver_stream.assert_awaited_once()
    assert deliver_stream.await_args.args[0].existing_event_id is None
    finalize.assert_awaited_once()
    finalize_request = finalize.await_args.args[0]
    assert finalize_request.stream_transport_outcome is transport
    assert finalize_request.initial_delivery_kind == "sent"
    assert finalize_request.identity.response_kind == "ai"


@pytest.mark.asyncio
async def test_non_streaming_reply_reports_its_delivery_start_before_delivering_it(tmp_path: Path) -> None:
    """A blocking reply notes that its delivery began before it delivers the answer."""
    coordinator = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    steps: list[str] = []

    async def deliver_final(*_args: object, **_kwargs: object) -> FinalDeliveryOutcome:
        steps.append("deliver")
        return completed_outcome("$existing", body="final text")

    with (
        patch.object(DeliveryGateway, "deliver_final", new=deliver_final),
        _delivery_starts(steps),
        _agent_turn(streamed_answer(AsyncMock(return_value="final text")), streaming=False),
    ):
        await coordinator.generate_response(_plain_request(_target()))

    assert steps == ["started", "deliver"]


@pytest.mark.asyncio
async def test_streaming_reply_reports_its_delivery_start_before_finalizing_it(tmp_path: Path) -> None:
    """A streamed reply notes that its delivery began before it finalizes the answer."""
    coordinator = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    steps: list[str] = []

    async def finalize(*_args: object, **_kwargs: object) -> FinalDeliveryOutcome:
        steps.append("finalize")
        return completed_outcome("$stream", body="streamed body")

    async def fake_stream(*_args: object, **_kwargs: object) -> AsyncIterator[str]:
        yield "chunk"

    with (
        patch.object(DeliveryGateway, "deliver_stream", new=_consuming_delivery(stream_transport())),
        patch.object(DeliveryGateway, "finalize_streamed_response", new=finalize),
        _delivery_starts(steps),
        _agent_turn(fake_stream, streaming=True),
    ):
        await coordinator.generate_response(_plain_request(_target()))

    assert steps == ["started", "finalize"]


@pytest.mark.asyncio
async def test_process_shutdown_unwinds_a_cancelled_stream_without_finalizing_it(tmp_path: Path) -> None:
    """A stream cancelled by process stop unwinds directly: nothing finalizes it and nothing records it stopped."""
    coordinator = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    generation_started = asyncio.Event()

    async def blocked_stream(*_args: object, **_kwargs: object) -> AsyncIterator[str]:
        generation_started.set()
        await asyncio.Event().wait()
        yield "unreachable"

    persist = MagicMock()
    finalize = AsyncMock(return_value=completed_outcome("$stream"))

    with (
        patch("mindroom.response_turn.persist_interrupted_replay_snapshot", new=persist),
        patch.object(DeliveryGateway, "deliver_stream", new=_consuming_delivery(stream_transport())),
        patch.object(DeliveryGateway, "finalize_streamed_response", new=finalize),
        _agent_turn(blocked_stream, streaming=True),
    ):
        task = asyncio.create_task(coordinator.generate_response(_plain_request(_target())))
        await generation_started.wait()
        request_task_cancel(task, process_shutdown=True)
        with pytest.raises(asyncio.CancelledError):
            await task

    persist.assert_not_called()
    finalize.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_shutdown_exposes_retained_streaming_response_phase(
    tmp_path: Path,
) -> None:
    """A live streaming child remains distinguishable from generic response work."""
    coordinator = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    generation_started = asyncio.Event()
    generation_cancelled = asyncio.Event()
    release_generation = asyncio.Event()

    async def retained_delivery(_request: StreamingDeliveryRequest) -> StreamTransportOutcome:
        generation_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            generation_cancelled.set()
            await release_generation.wait()
            raise
        return stream_transport()

    async def fake_stream(*_args: object, **_kwargs: object) -> AsyncIterator[str]:
        yield "chunk"

    with (
        patch.object(DeliveryGateway, "_deliver_stream", new=AsyncMock(side_effect=retained_delivery)),
        _agent_turn(fake_stream, streaming=True),
    ):
        response_task = coordinator.inbox.track_inbox_response(
            coordinator.generate_response(_plain_request(_target())),
            name="test_retained_streaming_response_phase",
            recovery_proof_ready=lambda: True,
            room_id=_target().room_id,
        )
        await generation_started.wait()
        coordinator.inbox.begin_process_shutdown()
        await generation_cancelled.wait()

        try:
            assert coordinator.inbox.pending_response_phase_counts == {"streaming_response": 1}
        finally:
            release_generation.set()

        assert await coordinator.inbox.drain_inbox_responses(
            cancel_after_seconds=0.1,
            shutdown_intent=ORDERLY_SHUTDOWN,
        )
        await asyncio.gather(response_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_process_shutdown_exposes_retained_final_delivery_phase(
    tmp_path: Path,
) -> None:
    """Terminal publication remains distinguishable after streaming has finished."""
    coordinator = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    finalization_started = asyncio.Event()
    finalization_cancelled = asyncio.Event()
    release_finalization = asyncio.Event()

    async def retained_finalization(_request: object) -> FinalDeliveryOutcome:
        finalization_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            finalization_cancelled.set()
            await release_finalization.wait()
            raise
        return completed_outcome("$stream")

    async def fake_stream(*_args: object, **_kwargs: object) -> AsyncIterator[str]:
        yield "chunk"

    with (
        patch.object(DeliveryGateway, "deliver_stream", new=_consuming_delivery(stream_transport())),
        # The gateway's public finalize enters the final-delivery phase around this step.
        patch.object(
            DeliveryGateway,
            "_finalize_streamed_response",
            new=AsyncMock(side_effect=retained_finalization),
        ),
        _agent_turn(fake_stream, streaming=True),
    ):
        response_task = coordinator.inbox.track_inbox_response(
            coordinator.generate_response(_plain_request(_target())),
            name="test_retained_final_delivery_phase",
            recovery_proof_ready=lambda: True,
            room_id=_target().room_id,
        )
        await finalization_started.wait()
        coordinator.inbox.begin_process_shutdown()
        await finalization_cancelled.wait()

        try:
            assert coordinator.inbox.pending_response_phase_counts == {"final_delivery": 1}
        finally:
            release_finalization.set()

        assert await coordinator.inbox.drain_inbox_responses(
            cancel_after_seconds=0.1,
            shutdown_intent=ORDERLY_SHUTDOWN,
        )
        await asyncio.gather(response_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_process_shutdown_skips_stream_terminalization_after_cancelled_generation_errors(
    tmp_path: Path,
) -> None:
    """A stream that replaces process cancellation with an error must not touch Matrix terminal delivery."""
    coordinator = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    generation_started = asyncio.Event()

    async def cancelled_then_failed_delivery(_request: StreamingDeliveryRequest) -> StreamTransportOutcome:
        generation_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as error:
            msg = "stream cleanup failed after cancellation"
            raise RuntimeError(msg) from error
        return stream_transport()

    async def fake_stream(*_args: object, **_kwargs: object) -> AsyncIterator[str]:
        yield "chunk"

    finalize = AsyncMock(return_value=completed_outcome("$stream"))
    end_interrupted_span = AsyncMock()

    with (
        patch.object(DeliveryGateway, "deliver_stream", new=AsyncMock(side_effect=cancelled_then_failed_delivery)),
        patch.object(DeliveryGateway, "finalize_streamed_response", new=finalize),
        patch.object(DeliveryGateway, "end_interrupted_span", new=end_interrupted_span),
        _agent_turn(fake_stream, streaming=True),
    ):
        task = asyncio.create_task(coordinator.generate_response(_plain_request(_target())))
        await generation_started.wait()
        request_task_cancel(task, process_shutdown=True)
        # The process stop unwinds the reply; the stream's own failure stays inside its attempt.
        with pytest.raises(asyncio.CancelledError):
            await task

    finalize.assert_not_awaited()
    end_interrupted_span.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_shutdown_blocks_stream_finalization_after_generation_consumes_cancel(
    tmp_path: Path,
) -> None:
    """A stream generator that consumes process cancellation cannot finalize its transport outcome."""
    coordinator = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    generation_started = asyncio.Event()
    finalize = AsyncMock(return_value=completed_outcome("$stream"))

    async def cancellation_resistant_stream(*_args: object, **_kwargs: object) -> AsyncIterator[str]:
        generation_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            assert current_task_is_process_shutdown()
        yield "late response"

    with (
        patch.object(DeliveryGateway, "deliver_stream", new=_consuming_delivery(stream_transport())),
        patch.object(DeliveryGateway, "finalize_streamed_response", new=finalize),
        _agent_turn(cancellation_resistant_stream, streaming=True),
    ):
        task = asyncio.create_task(coordinator.generate_response(_plain_request(_target())))
        await generation_started.wait()
        request_task_cancel(task, process_shutdown=True)
        with pytest.raises(asyncio.CancelledError):
            await task

    finalize.assert_not_awaited()


@pytest.mark.asyncio
async def test_streaming_placeholder_only_delivery_does_not_mark_substantive_reply(tmp_path: Path) -> None:
    """Placeholder-only stream finalization must not report visible answer text."""
    bot = _bot(tmp_path)
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    transport = stream_transport("$placeholder", "Thinking...", visible_body_state="placeholder_only")
    finalize = AsyncMock(return_value=FinalDeliveryOutcome(ending=Completed()))
    timing = DispatchPipelineTiming(source_event_id="$request", room_id="!room:localhost")
    timing.mark_first_visible_reply("placeholder")
    request = replace(_plain_request(_target()), pipeline_timing=timing)

    async def fake_stream(*_args: object, **_kwargs: object) -> AsyncIterator[str]:
        yield ""

    with (
        patch.object(DeliveryGateway, "deliver_stream", new=_consuming_delivery(transport)),
        patch.object(DeliveryGateway, "finalize_streamed_response", new=finalize),
        _agent_turn(fake_stream, streaming=True),
    ):
        await coordinator.generate_response(request)

    finalize.assert_awaited_once()
    assert finalize.await_args.args[0].stream_transport_outcome is transport
    assert timing.metadata["first_visible_kind"] == "placeholder"
    assert "first_substantive_reply" not in timing.marks
    assert "first_substantive_kind" not in timing.metadata


@pytest.mark.asyncio
async def test_streaming_midstream_failure_records_the_stopped_turn_and_leaves_its_span_the_ending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A delivery that fails mid-stream stops reading: the turn is recorded stopped once, nothing finalizes the stream."""
    bot = _bot(tmp_path)
    # Mock the logger collaborator: the production rich traceback renderer is pathologically
    # slow on mock-laden tracebacks, and the log call itself is part of the pinned fallback.
    coordinator = replace_response_runner_deps(bot, logger=MagicMock())
    install_model_providers(monkeypatch, {"default": ScriptedModel([answer("partial body and the rest", chunk=8)])})

    async def failing_delivery(request: StreamingDeliveryRequest) -> StreamTransportOutcome:
        stream = request.response_stream
        shown = ""
        async for chunk in stream:
            if isinstance(chunk, TextDelta):
                shown += chunk.text
            if shown:
                break
        # The streamer closes the response stream before it reports the failure.
        await cast("Any", stream).aclose()
        raise StreamingDeliveryError(RuntimeError("boom"))

    finalize = AsyncMock()
    persist = MagicMock()
    request = replace(
        _plain_request(_target()),
        model_prompt="hello\n\n<mindroom_message_context>persist me</mindroom_message_context>",
    )
    with (
        patch.object(DeliveryGateway, "deliver_stream", new=AsyncMock(side_effect=failing_delivery)),
        patch.object(DeliveryGateway, "finalize_streamed_response", new=finalize),
        patch("mindroom.response_turn.persist_interrupted_replay_snapshot", new=persist),
        _agent_turn(None, streaming=True),
    ):
        await coordinator.generate_response(request)

    # The failure does not propagate: it is logged, and the reply's span exit writes the error.
    coordinator.deps.logger.exception.assert_any_call("Error during response generation", error="boom")
    finalize.assert_not_awaited()
    # The partial reply was recorded as one interrupted-replay snapshot of the agent's session.
    persist.assert_called_once()
    snapshot = persist.call_args.kwargs["snapshot"]
    assert "<mindroom_message_context>persist me</mindroom_message_context>" in snapshot.user_message
    assert snapshot.partial_text == "partial"
    assert snapshot.original_status is RunStatus.cancelled
    assert persist.call_args.kwargs["session"].kind == "agent"


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
    assert {message.event_id for message in queued_signal.pending_message_snapshot()} == {"$second"}

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
@pytest.mark.parametrize("consumed", [False, True])
async def test_queued_response_lifecycle_reservation_cancellation_does_not_leak_lock(*, consumed: bool) -> None:
    """Cancelling a queued reservation removes it without stealing the released lock."""
    coordinator = ResponseLifecycleCoordinator()
    envelope = _queued_envelope("$interactive")
    lifecycle_lock = coordinator._response_lifecycle_lock(envelope.target)
    await lifecycle_lock.acquire()
    reservation = await coordinator.reserve_response_lifecycle(envelope)
    queued_signal = coordinator._get_or_create_queued_signal(envelope.target)
    assert {message.event_id for message in queued_signal.pending_message_snapshot()} == {envelope.source_event_id}
    assert queued_signal.has_active_response_turn()

    if consumed:
        with response_lifecycle_reservation_context(reservation):
            response = asyncio.create_task(
                coordinator.run_locked_response(
                    target=envelope.target,
                    response_envelope=envelope,
                    pipeline_timing=None,
                    locked_operation=lambda _target: asyncio.sleep(0, result="cancelled"),
                ),
            )
        await asyncio.sleep(0)
        assert reservation._consumed
        response.cancel()
        with pytest.raises(asyncio.CancelledError):
            await response

    await reservation.release()
    await reservation.release()
    assert {message.event_id for message in queued_signal.pending_message_snapshot()} == set()
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
        with response_lifecycle_reservation_context(reservation):
            with pytest.raises(ValueError, match="different coordinator"):
                await other_coordinator.run_locked_response(
                    target=envelope.target,
                    response_envelope=envelope,
                    pipeline_timing=None,
                    locked_operation=lambda _target: asyncio.sleep(0),
                )
            with pytest.raises(ValueError, match="target does not match"):
                await coordinator.run_locked_response(
                    target=other_target,
                    response_envelope=replace(envelope, target=other_target),
                    pipeline_timing=None,
                    locked_operation=lambda _target: asyncio.sleep(0),
                )
            assert (
                await coordinator.run_locked_response(
                    target=envelope.target,
                    response_envelope=envelope,
                    pipeline_timing=None,
                    locked_operation=lambda _target: asyncio.sleep(0, result="owned"),
                )
                == "owned"
            )
        with response_lifecycle_reservation_context(reservation), pytest.raises(RuntimeError, match="already consumed"):
            await coordinator.run_locked_response(
                target=envelope.target,
                response_envelope=envelope,
                pipeline_timing=None,
                locked_operation=lambda _target: asyncio.sleep(0),
            )
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
    reservation.release()
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


_SUSPENDED = FinalDeliveryOutcome(ending=AwaitingApproval(), event_id="$response", is_visible_response=True)
_OWED = FinalDeliveryOutcome(ending=Owed())


def _settle(
    coordinator: ResponseRunner,
    request: ResponseRequest,
    progress: TurnProgress,
    *,
    lifecycle: ResponseLifecycle | None = None,
    response_function: Callable[[str | None], Coroutine[Any, Any, FinalDeliveryOutcome]] | None = None,
    build_post_response_outcome: Callable[[FinalDeliveryOutcome], ResponseOutcome] | None = None,
) -> Coroutine[Any, Any, None]:
    """Return the locked turn's run and settlement of ``request``, through ``lifecycle`` or one built for it.

    Its attempt runs ``response_function``, and its post-response outcome is a plain ``ResponseOutcome`` unless given.
    """
    return coordinator.locked.run_and_settle_locked_response(
        request,
        target=request.response_envelope.target,
        lifecycle=coordinator.locked.build_lifecycle(request, response_kind="ai") if lifecycle is None else lifecycle,
        progress=progress,
        response_function=response_function or AsyncMock(),
        build_post_response_outcome=build_post_response_outcome or (lambda _outcome: ResponseOutcome()),
        post_response_deps=lambda: PostResponseEffectsDeps(logger=get_logger("tests.post_response")),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("delivery_outcome", "run_succeeded", "process_shutdown_requested", "applies_effects", "effects_error"),
    [
        (completed_outcome(), True, False, True, None),
        (
            FinalDeliveryOutcome(ending=Cancelled("user_stop"), event_id="$response", is_visible_response=True),
            False,
            False,
            True,
            None,
        ),
        (
            FinalDeliveryOutcome(
                ending=Failed(FailureCause.DELIVERY_FAILED),
                event_id="$response",
                is_visible_response=True,
            ),
            False,
            False,
            True,
            None,
        ),
        (_SUSPENDED, False, False, False, None),
        (_OWED, True, False, True, None),
        (_OWED, True, True, False, None),
        (_OWED, True, False, True, asyncio.CancelledError),
        (_OWED, True, False, True, RuntimeError),
    ],
    ids=[
        "completed",
        "cancelled",
        "error",
        "approval_pause",
        "owed_answer",
        "owed_answer_during_process_shutdown",
        "owed_answer_whose_effects_are_cancelled",
        "owed_answer_whose_effects_fail",
    ],
)
async def test_response_settlement_finalizes_once_and_records_the_ending(
    tmp_path: Path,
    delivery_outcome: FinalDeliveryOutcome,
    *,
    run_succeeded: bool,
    process_shutdown_requested: bool,
    applies_effects: bool,
    effects_error: type[BaseException] | None,
) -> None:
    """Each outcome finalizes once and records its ending on the turn's progress.

    A suspended reply applies its run's effects only when its run finished, as an owed answer's did, and an effect
    that fails or is cancelled leaves the answer to the outbox.
    """
    bot = _bot(tmp_path)
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    request = _plain_request(_target())
    progress = TurnProgress()
    post_effects = AsyncMock(side_effect=effects_error)
    build_post_outcome = MagicMock(return_value=ResponseOutcome(run_succeeded=run_succeeded))

    async def generate(_message_id: str | None) -> FinalDeliveryOutcome:
        return delivery_outcome

    lifecycle = coordinator.locked.build_lifecycle(request, response_kind="ai")
    lifecycle.deps = replace(
        lifecycle.deps,
        process_shutdown_requested=lambda: process_shutdown_requested,
    )
    finalize = AsyncMock(wraps=lifecycle.finalize)
    with (
        patch.object(LockedTurns, "_run_cancellable_response", new=attempt_in_place()),
        patch.object(lifecycle, "finalize", new=finalize),
        patch_response_runner_module(apply_post_response_effects=post_effects),
    ):
        await _settle(
            coordinator,
            request,
            progress,
            lifecycle=lifecycle,
            response_function=generate,
            build_post_response_outcome=build_post_outcome,
        )

    assert progress.ending == delivery_outcome.ending
    build_post_outcome.assert_called_once_with(delivery_outcome)
    finalize.assert_awaited_once()
    assert post_effects.await_count == int(applies_effects)


@pytest.mark.parametrize(
    ("turn_visibility", "reloaded_visibility"),
    [(False, True), (True, False)],
)
@pytest.mark.asyncio
async def test_approval_handoff_uses_visibility_frozen_for_the_turn(
    tmp_path: Path,
    turn_visibility: bool,
    reloaded_visibility: bool,
) -> None:
    """A config reload during the turn cannot reinterpret the pause the turn hands to its approval."""
    bot = _bot(tmp_path)
    unique_room_send_responses(bot.client)
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    agent_config = coordinator.deps.runtime.config.agents[coordinator.deps.agent_name]
    agent_config.show_tool_calls = turn_visibility
    tool = ToolExecution(tool_call_id="call-1", tool_name="inspect")
    trace = ToolTraceEntry(type="tool_call_started", tool_name="inspect", tool_call_id="call-1")
    marker = "\n\n🔧 `inspect` [1] ⏳"
    shown = f"Before.{marker}" if turn_visibility else "Before."
    error = ResponsePausedForApproval(
        PausedAttempt(
            session_id="session-1",
            run_id="run-1",
            requirements=(RunRequirement(tool),),
            response_text=shown,
            tool_trace=(trace,),
            toolkit_owners={("general", "inspect"): "test_toolkit"},
        ),
    )
    error.capture_presentation(
        StreamingPresentation(response_text=shown, tool_trace=(trace,) if turn_visibility else ()),
    )

    async def pause_after_reload(**_kwargs: object) -> None:
        agent_config.show_tool_calls = reloaded_visibility
        raise error

    suspend = AsyncMock(
        return_value=FinalDeliveryOutcome(ending=AwaitingApproval(), event_id="$response", is_visible_response=True),
    )
    with (
        patch.object(LockedTurns, "_run_cancellable_response", new=AsyncMock(side_effect=pause_after_reload)),
        patch.object(coordinator.approvals, "suspend_for_approval", new=suspend),
        patch_response_runner_module(
            should_use_streaming=AsyncMock(return_value=False),
            typing_indicator=_noop_typing,
            apply_post_response_effects=AsyncMock(),
        ),
    ):
        await coordinator.generate_response(_plain_request(_target(thread_id="$thread")))

    [handoff] = suspend.await_args_list
    paused = handoff.args[0]
    assert handoff.kwargs["show_tool_calls"] is turn_visibility
    assert paused.response_text == shown
    assert paused.tool_trace == (trace,)


@pytest.mark.asyncio
async def test_terminal_settlement_records_landed_interruption_before_rethrowing_cancel(tmp_path: Path) -> None:
    """A deferred sync-restart cancel whose note landed should finalize once, run post-response effects, then re-raise."""
    bot = _bot(tmp_path)
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    request = _plain_request(_target(thread_id="$thread"))
    progress = TurnProgress()
    progress.note_delivery_started("$response")
    post_effects = AsyncMock(return_value=None)
    lifecycle = coordinator.locked.build_lifecycle(request, response_kind="ai")
    finalize = AsyncMock(wraps=lifecycle.finalize)

    async with _ended_with_its_note(coordinator, request):
        with (
            patch.object(
                LockedTurns,
                "_run_cancellable_response",
                new=AsyncMock(side_effect=asyncio.CancelledError("sync_restart")),
            ),
            patch.object(lifecycle, "finalize", new=finalize),
            patch_response_runner_module(apply_post_response_effects=post_effects),
            pytest.raises(asyncio.CancelledError, match="sync_restart"),
        ):
            await _settle(coordinator, request, progress, lifecycle=lifecycle)

    assert progress.ending == Cancelled("sync_restart")
    finalize.assert_awaited_once()
    post_effects.assert_awaited_once()


@pytest.mark.asyncio
async def test_process_shutdown_propagates_from_visible_response_hooks(
    tmp_path: Path,
) -> None:
    """Orderly stop must not suppress hook cancellation and continue post-response effects."""
    coordinator = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    request = _plain_request(_target(thread_id="$thread"))
    lifecycle = coordinator.locked.build_lifecycle(request, response_kind="ai")
    hook_started = asyncio.Event()

    async def blocked_hook(**_kwargs: object) -> None:
        hook_started.set()
        await asyncio.Event().wait()

    lifecycle.deps.response_hooks._emit_after_response = AsyncMock(
        side_effect=blocked_hook,
    )
    post_effects = AsyncMock()
    outcome = completed_outcome()
    with patch_response_runner_module(apply_post_response_effects=post_effects):
        task = asyncio.create_task(
            lifecycle.finalize(
                outcome,
                build_post_response_outcome=lambda _outcome: ResponseOutcome(),
                post_response_deps=lambda: PostResponseEffectsDeps(
                    logger=get_logger("tests.post_response"),
                ),
            ),
        )
        await hook_started.wait()
        request_task_cancel(task, process_shutdown=True)
        with pytest.raises(asyncio.CancelledError):
            await task

    post_effects.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_shutdown_skips_terminal_settlement_for_durable_replay(
    tmp_path: Path,
) -> None:
    """Orderly stop must not wait on Matrix cleanup after durable response cancellation."""
    coordinator = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    request = _plain_request(_target(thread_id="$thread"))
    progress = TurnProgress()
    attempt_started = asyncio.Event()
    release_settlement = asyncio.Event()

    async def cancelled_attempt(**_kwargs: object) -> None:
        attempt_started.set()
        await asyncio.Event().wait()

    async def blocked_settlement(**_kwargs: object) -> None:
        await release_settlement.wait()

    with (
        patch.object(
            LockedTurns,
            "_run_cancellable_response",
            new=AsyncMock(side_effect=cancelled_attempt),
        ),
        patch.object(
            LockedTurns,
            "_finalize_locked_outcome",
            new=AsyncMock(side_effect=blocked_settlement),
        ) as settle,
    ):
        task = asyncio.create_task(_settle(coordinator, request, progress))
        await attempt_started.wait()
        request_task_cancel(task, process_shutdown=True)
        try:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(task), timeout=0.1)
        finally:
            release_settlement.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    settle.assert_not_awaited()


@pytest.mark.asyncio
async def test_uncommitted_interruption_rethrows_cancel_after_its_post_response_effects(tmp_path: Path) -> None:
    """Checkpoint replay must remain actionable when no terminal recovery note landed."""
    coordinator = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    request = _plain_request(_target(thread_id="$thread"))
    progress = TurnProgress()
    progress.note_delivery_started("$response")
    post_effects = AsyncMock(return_value=None)

    with (
        patch.object(
            LockedTurns,
            "_run_cancellable_response",
            new=AsyncMock(side_effect=asyncio.CancelledError("sync_restart")),
        ),
        patch_response_runner_module(apply_post_response_effects=post_effects),
        pytest.raises(asyncio.CancelledError, match="sync_restart"),
    ):
        await _settle(coordinator, request, progress)

    post_effects.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancel_cleanup_error_leaves_the_source_pending_for_replay(tmp_path: Path) -> None:
    """A failed cancellation cleanup must preserve replay instead of deduping the stale placeholder."""
    coordinator = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    request = _plain_request(_target(thread_id="$thread"))
    progress = TurnProgress()
    # The restart's cancellation ended the turn, and cleaning up its placeholder failed, so no note landed.
    cancelled = FinalDeliveryOutcome(
        ending=Cancelled("sync_restart"),
        event_id="$placeholder",
        is_visible_response=True,
    )

    with (
        patch.object(
            LockedTurns,
            "_run_cancellable_response",
            new=AsyncMock(return_value=cancelled),
        ),
        patch_response_runner_module(apply_post_response_effects=AsyncMock()),
    ):
        await _settle(coordinator, request, progress)

    assert progress.ending == Cancelled("sync_restart")
    # No reply's records hold the source, so it stays pending for its replay.
    assert await coordinator.deps.replies.store.replies.for_sources(("$event",)) is None


@pytest.mark.asyncio
async def test_terminal_send_cancellation_leaves_the_ending_to_the_span_exit(tmp_path: Path) -> None:
    """A restart cancel during a finished stream's terminal edit raises out of finalize, which writes no ending.

    The reply's span exit then notes the interruption, and the source stays unhandled until that note lands.
    """
    coordinator = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    target = _target(thread_id="$thread")
    records = FakeReplyRecords(edit=AsyncMock(side_effect=asyncio.CancelledError("sync_restart")))
    streaming = StreamingResponse(
        target=target,
        config=coordinator.deps.runtime.config,
        runtime_paths=coordinator.deps.runtime_paths,
        **records.hooks(),
    )
    streaming.event_id = "$response"
    streaming.accumulated_text = "partial answer"
    delivered = DeliveredMatrixEvent(
        event_id="$response",
        content_sent={"body": "partial answer"},
    )
    with patch("mindroom.streaming.edit_message_result", new=AsyncMock(return_value=delivered)):
        assert await streaming._send_or_edit_message(coordinator.locked.client())

    with pytest.raises(asyncio.CancelledError, match="sync_restart"):
        await streaming.finalize(coordinator.locked.client())
    assert [write.kind for write in records.writes] == ["progress", "progress_delivered", "terminal"]


@pytest.mark.asyncio
async def test_uncommitted_interruption_remains_unhandled_without_outer_cancel(tmp_path: Path) -> None:
    """A cancelled outcome needs a landed interruption note before dedup."""
    coordinator = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    target = _target(thread_id="$thread")
    request = _plain_request(target)
    progress = TurnProgress()
    cancelled = FinalDeliveryOutcome(
        ending=Cancelled("sync_restart"),
        event_id="$response",
        is_visible_response=True,
        final_visible_body=RESTART_INTERRUPTED_RESPONSE_NOTE,
    )

    with (
        patch.object(
            LockedTurns,
            "_run_cancellable_response",
            new=AsyncMock(return_value=cancelled),
        ),
        patch_response_runner_module(apply_post_response_effects=AsyncMock()),
    ):
        await _settle(coordinator, request, progress)

    assert progress.ending == Cancelled("sync_restart")
    # No reply's records hold the source, so it stays pending for its replay.
    assert await coordinator.deps.replies.store.replies.for_sources(("$event",)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("thread_id", "ending", "final_visible_body"),
    [
        ("$thread", Cancelled("interrupted"), INTERRUPTED_RESPONSE_NOTE),
        (None, Cancelled("sync_restart"), RESTART_INTERRUPTED_RESPONSE_NOTE),
    ],
    ids=["interrupted", "threadless-restart"],
)
async def test_landed_terminal_interruption_settles_the_turn(
    tmp_path: Path,
    thread_id: str | None,
    ending: Cancelled,
    final_visible_body: str,
) -> None:
    """A landed interruption is terminal, so the turn is handled."""
    bot = _bot(tmp_path)
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    request = _plain_request(_target(thread_id=thread_id))
    progress = TurnProgress()
    cancelled = FinalDeliveryOutcome(
        ending=ending,
        event_id="$response",
        is_visible_response=True,
        final_visible_body=final_visible_body,
        delivery_kind="edited",
    )

    async with _ended_with_its_note(coordinator, request):
        with (
            patch.object(
                LockedTurns,
                "_run_cancellable_response",
                new=AsyncMock(return_value=cancelled),
            ),
            patch_response_runner_module(apply_post_response_effects=AsyncMock()),
        ):
            await _settle(coordinator, request, progress)

    assert progress.ending == ending
    # The reply that shows the interruption's note ended, and its records settle the turn.
    reply = await coordinator.deps.replies.store.replies.for_sources(("$event",))
    assert reply is not None
    assert reply.terminal


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "delivery_outcome",
    [
        completed_outcome(),
        FinalDeliveryOutcome(
            ending=Failed(FailureCause.DELIVERY_FAILED),
            event_id="$response",
            is_visible_response=True,
        ),
    ],
    ids=["completed", "error"],
)
async def test_terminal_settlement_late_cancel_keeps_settled_outcome_canonical(
    tmp_path: Path,
    delivery_outcome: FinalDeliveryOutcome,
) -> None:
    """A cancel that lands as the attempt returns its ending keeps that ending instead of reporting a cancellation."""
    bot = _bot(tmp_path)
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    request = _plain_request(_target())
    progress = TurnProgress()
    progress.note_delivery_started("$response")
    post_effects = AsyncMock(return_value=None)
    lifecycle = coordinator.locked.build_lifecycle(request, response_kind="ai")
    finalize = AsyncMock(wraps=lifecycle.finalize)
    settling: list[asyncio.Task[None]] = []

    async def delivered_then_cancelled(_message_id: str | None) -> FinalDeliveryOutcome:
        # The restart's cancel reaches the settling turn as its attempt returns what it delivered.
        settling[0].cancel(SYNC_RESTART_CANCEL_MSG)
        return delivery_outcome

    async with response_span(coordinator, request, placeholder_event_id="$response"):
        with (
            patch.object(lifecycle, "finalize", new=finalize),
            patch_response_runner_module(apply_post_response_effects=post_effects),
        ):
            settling.append(
                asyncio.create_task(
                    _settle(
                        coordinator,
                        request,
                        progress,
                        lifecycle=lifecycle,
                        response_function=delivered_then_cancelled,
                    ),
                ),
            )
            await asyncio.gather(settling[0], return_exceptions=True)

    assert progress.ending == delivery_outcome.ending
    finalize.assert_awaited_once()
    post_effects.assert_awaited_once()


@pytest.mark.asyncio
async def test_terminal_settlement_settles_generation_error_after_post_effects(tmp_path: Path) -> None:
    """A pre-delivery generation error settles as the turn's failure, finalizes once, and runs effects."""
    bot = _bot(tmp_path)
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    request = _plain_request(_target())
    progress = TurnProgress()
    post_effects = AsyncMock()
    build_post_outcome = MagicMock(return_value=ResponseOutcome())
    lifecycle = coordinator.locked.build_lifecycle(request, response_kind="ai")
    finalize = AsyncMock(wraps=lifecycle.finalize)

    with (
        patch.object(
            LockedTurns,
            "_run_cancellable_response",
            new=AsyncMock(side_effect=RuntimeError("generation failed")),
        ),
        patch.object(lifecycle, "finalize", new=finalize),
        patch_response_runner_module(apply_post_response_effects=post_effects),
    ):
        await _settle(
            coordinator,
            request,
            progress,
            lifecycle=lifecycle,
            build_post_response_outcome=build_post_outcome,
        )

    assert progress.ending == Failed(FailureCause.ERROR, "generation failed")
    build_post_outcome.assert_called_once()
    assert build_post_outcome.call_args.args[0].ending == progress.ending
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
        return completed_outcome("$response", body="final text")

    async def fake_post_effects(final_outcome: FinalDeliveryOutcome, *_args: object) -> None:
        order.append("post_effects")
        effect_outcomes.append(final_outcome)

    with (
        patch.object(DeliveryGateway, "send_text", new=AsyncMock(side_effect=fake_send_text)),
        patch.object(DeliveryGateway, "deliver_final", new=AsyncMock(side_effect=fake_deliver_final)),
        patch_response_runner_module(
            stream_agent_response=streamed_answer(AsyncMock(side_effect=fake_ai_response)),
            should_use_streaming=AsyncMock(return_value=False),
            typing_indicator=_noop_typing,
            apply_post_response_effects=AsyncMock(side_effect=fake_post_effects),
        ),
    ):
        assert await coordinator.generate_response(_plain_request(_target())) == Owned()

    assert order == ["placeholder", "generate", "deliver_final", "post_effects"]
    assert effect_outcomes[0].ending == Completed()
    assert effect_outcomes[0].final_visible_event_id == "$response"


@pytest.mark.asyncio
async def test_delivery_failure_emits_cancelled_hook_and_passes_error_outcome_to_effects(tmp_path: Path) -> None:
    """A failed delivery emits the cancelled hook, skips after-response, and forwards the error outcome."""
    bot = _bot(tmp_path)
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    error_outcome = FinalDeliveryOutcome(ending=Failed(FailureCause.DELIVERY_FAILED))
    effect_outcomes: list[FinalDeliveryOutcome] = []

    async def fake_post_effects(final_outcome: FinalDeliveryOutcome, *_args: object) -> None:
        effect_outcomes.append(final_outcome)

    send_text = AsyncMock(return_value="$placeholder")
    with (
        patch.object(DeliveryGateway, "send_text", new=send_text),
        patch.object(DeliveryGateway, "deliver_final", new=AsyncMock(return_value=error_outcome)),
        patch.object(
            bot._delivery_gateway.deps.response_hooks,
            "_emit_after_response",
            new=AsyncMock(),
        ) as mock_after,
        patch.object(
            bot._delivery_gateway.deps.response_hooks,
            "_emit_cancelled_response",
            new=AsyncMock(),
        ) as mock_cancelled,
        patch_response_runner_module(
            stream_agent_response=streamed_answer(AsyncMock(return_value="final text")),
            should_use_streaming=AsyncMock(return_value=False),
            typing_indicator=_noop_typing,
            apply_post_response_effects=AsyncMock(side_effect=fake_post_effects),
        ),
    ):
        assert await coordinator.generate_response(_plain_request(_target())) == Owned()

    # The reply ends with the delivery-failed note in place of the answer it could not deliver.
    placeholder, note = (call.args[0].response_text for call in send_text.await_args_list)
    assert placeholder == "Thinking..."
    assert note.endswith("Response delivery failed. Please retry.")
    mock_after.assert_not_awaited()
    mock_cancelled.assert_awaited_once()
    assert mock_cancelled.await_args.kwargs["failure_reason"] == "delivery_failed"
    # The effects step still runs, but receives the error outcome so success effects are gated off.
    assert [outcome.ending for outcome in effect_outcomes] == [Failed(FailureCause.DELIVERY_FAILED)]


@pytest.mark.asyncio
async def test_apply_post_response_effects_gates_success_only_side_effects() -> None:
    """Memory persistence and run-event linkage run on success and stay off after a failed delivery."""
    memory_calls: list[str] = []
    persisted: list[tuple[str, str]] = []

    async def persist_response_event_id(run_id: str, event_id: str) -> None:
        await asyncio.sleep(0)
        persisted.append((run_id, event_id))

    deps = PostResponseEffectsDeps(
        logger=get_logger("tests.post_effects"),
        queue_memory_persistence=lambda: memory_calls.append("memory"),
        persist_response_event_id=persist_response_event_id,
    )

    await apply_post_response_effects(
        completed_outcome("$response", body="ok"),
        ResponseOutcome(response_run_id="run-1", run_succeeded=True),
        deps,
    )
    assert memory_calls == ["memory"]
    assert persisted == [("run-1", "$response")]

    await apply_post_response_effects(
        FinalDeliveryOutcome(ending=Failed(FailureCause.DELIVERY_FAILED)),
        ResponseOutcome(response_run_id="run-1", run_succeeded=False),
        deps,
    )
    # The failed delivery added neither memory persistence nor run-event linkage.
    assert memory_calls == ["memory"]
    assert persisted == [("run-1", "$response")]


@pytest.mark.asyncio
async def test_generic_retry_reuses_cancelled_offloaded_proof() -> None:
    """A retry retains the cancelling store read until it can safely be replaced."""
    inbox = _inbox()
    started = asyncio.Event()
    proof_started = asyncio.Event()
    proof_cancelled = asyncio.Event()
    release_proof = asyncio.Event()
    proof_calls = 0

    async def response() -> None:
        started.set()
        await asyncio.Event().wait()

    async def proof() -> bool:
        nonlocal proof_calls
        proof_calls += 1
        proof_started.set()
        try:
            await release_proof.wait()
        except asyncio.CancelledError:
            proof_cancelled.set()
            await release_proof.wait()
            raise
        return True

    task = inbox.track_inbox_response(
        response(),
        name="generic_offloaded",
        recovery_proof_ready=proof,
        source_event_ids=("$source",),
        room_id=_target().room_id,
    )
    await started.wait()
    with pytest.raises(ResponseShutdownTimeoutError):
        await inbox.drain_inbox_responses(cancel_after_seconds=0.01)
    await proof_started.wait()
    await proof_cancelled.wait()
    assert inbox.pending_inbox_response_count == 1
    assert not inbox.has_live_inbox_response("$source")
    second = asyncio.create_task(inbox.drain_inbox_responses(cancel_after_seconds=0.1))
    await asyncio.sleep(0)
    assert proof_calls == 1
    assert inbox.pending_inbox_response_count == 1
    assert not inbox.has_live_inbox_response("$source")
    release_proof.set()
    assert await second
    assert proof_calls == 2
    assert inbox.pending_inbox_response_count == 0
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelled_generic_drain_keeps_proof_owner() -> None:
    """Cancelling a drain cannot orphan a proof that still uses shared resources."""
    inbox = _inbox()
    started = asyncio.Event()
    proof_started = asyncio.Event()
    release_proof = asyncio.Event()
    proof_calls = 0

    async def response() -> None:
        started.set()
        await asyncio.Event().wait()

    async def proof() -> bool:
        nonlocal proof_calls
        proof_calls += 1
        proof_started.set()
        while not release_proof.is_set():
            with suppress(asyncio.CancelledError):
                await release_proof.wait()
        return True

    task = inbox.track_inbox_response(
        response(),
        name="cancelled_generic",
        recovery_proof_ready=proof,
        source_event_ids=("$source",),
        room_id=_target().room_id,
    )
    await started.wait()
    draining = asyncio.create_task(inbox.drain_inbox_responses(cancel_after_seconds=0.1))
    await proof_started.wait()
    draining.cancel()
    with pytest.raises(asyncio.CancelledError):
        await draining
    assert inbox.pending_inbox_response_count == 1
    assert not inbox.has_live_inbox_response("$source")
    release_proof.set()
    assert await inbox.drain_inbox_responses(cancel_after_seconds=0.1)
    assert proof_calls == 1
    assert inbox.pending_inbox_response_count == 0
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("completed_before_promotion", [False, True])
async def test_generic_to_process_proof_promotion_retries_false_result(completed_before_promotion: bool) -> None:
    """Orderly shutdown keeps checking a generic proof promoted while its read is pending."""
    inbox = _inbox()
    started = asyncio.Event()
    proof_started = asyncio.Event()
    release_proof = asyncio.Event()
    proof_calls = 0

    async def response() -> None:
        started.set()
        await asyncio.Event().wait()

    async def proof() -> bool:
        nonlocal proof_calls
        proof_calls += 1
        if proof_calls > 1:
            return True
        proof_started.set()
        while not release_proof.is_set():
            with suppress(asyncio.CancelledError):
                await release_proof.wait()
        return False

    task = inbox.track_inbox_response(
        response(),
        name="promoted_generic",
        recovery_proof_ready=proof,
        source_event_ids=("$source",),
        room_id=_target().room_id,
    )
    await started.wait()
    with pytest.raises(ResponseShutdownTimeoutError):
        await inbox.drain_inbox_responses(cancel_after_seconds=0.01)
    await proof_started.wait()
    if completed_before_promotion:
        release_proof.set()
        await asyncio.sleep(0)
    inbox.begin_process_shutdown()
    release_proof.set()
    assert await inbox.drain_inbox_responses(cancel_after_seconds=0.1, shutdown_intent=ORDERLY_SHUTDOWN)
    assert proof_calls == 2
    assert inbox.pending_inbox_response_count == 0
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_overlapping_drains_keep_snapshot_ownership() -> None:
    """One drain may consume ownership while another still holds its terminal record."""
    inbox = _inbox()
    first_started = asyncio.Event()
    second_started = asyncio.Event()
    release_second = asyncio.Event()

    async def first_response() -> None:
        first_started.set()
        await asyncio.Event().wait()

    async def second_response() -> None:
        second_started.set()
        await release_second.wait()

    first = inbox.track_inbox_response(
        first_response(),
        name="first_snapshot",
        recovery_proof_ready=lambda: True,
        room_id="!room:example.org",
    )
    await first_started.wait()
    first_drain = asyncio.create_task(inbox.drain_inbox_responses(cancel_after_seconds=0.01))
    await asyncio.sleep(0)
    second = inbox.track_inbox_response(
        second_response(),
        name="second_snapshot",
        recovery_proof_ready=lambda: True,
        room_id="!room:example.org",
    )
    await second_started.wait()
    second_drain = asyncio.create_task(inbox.drain_inbox_responses(cancel_after_seconds=0.1))
    assert not await first_drain
    assert inbox.pending_inbox_response_count == 1
    assert not inbox.has_live_inbox_response("$source")
    release_second.set()
    assert await second_drain
    assert inbox.pending_inbox_response_count == 0
    await asyncio.gather(first, second, return_exceptions=True)


@pytest.mark.asyncio
async def test_tools_get_the_reply_correlation_when_the_request_names_none(tmp_path: Path) -> None:
    """Tools see the reply's identity even for requests without one, such as interactive selections."""
    coordinator = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    build_turn_context = coordinator.deps.tool_runtime.build_turn_context
    observed: list[str | None] = []

    def recording_build_turn_context(*args: object, correlation_id: str | None = None, **kwargs: object) -> object:
        observed.append(correlation_id)
        return build_turn_context(*args, correlation_id=correlation_id, **kwargs)

    with patch.object(
        coordinator.deps.tool_runtime,
        "build_turn_context",
        side_effect=recording_build_turn_context,
    ):
        await coordinator.prepare_response_runtime(
            _plain_request(_target(reply_to_event_id="$selection")),
            show_tool_calls=True,
        )

    assert observed == ["$selection"]


async def _cold_cache_reply_model(
    tmp_path: Path,
    *,
    scheduled_model: str | None = None,
    room_model: str | None = None,
    thread_model: str | None = None,
    media: MediaInputs | None = None,
    inherit_cheap_model: bool = False,
    thread_id: str | None = "$event",
    replies: int = 1,
) -> tuple[list[str | None], list[dict[str, object]]]:
    """Run replies for a cold-cache agent and return the models they used and the selector calls."""
    bot = _bot(tmp_path)
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    config = coordinator.deps.runtime.config
    config.models["cheap"] = ModelConfig(provider="openai", id="cheap-model")
    config.models["cheap2"] = ModelConfig(provider="openai", id="cheap2-model")
    config.agents["general"] = config.agents["general"].model_copy(
        update={"cheap_model": None if inherit_cheap_model else "cheap", "cold_cache": ColdCacheConfig()},
    )
    if inherit_cheap_model:
        config.defaults.cheap_model = "cheap"
    if room_model is not None:
        set_room_model_override(coordinator.deps.runtime_paths, room_id="!room:localhost", model_name=room_model)
    if thread_model is not None:
        set_thread_model_override(
            coordinator.deps.runtime_paths,
            thread_id="$event",
            model_name=thread_model,
            entity_names=("general",),
            config=config,
        )
    selector_calls: list[dict[str, object]] = []
    active_models: list[str | None] = []

    async def fake_select(_settings: ColdCacheConfig, *, cheap_model: str, **kwargs: object) -> str:
        selector_calls.append({"cheap_model": cheap_model, **kwargs})
        return cheap_model

    async def fake_ai_response(ctx: ResponseTurnContext, *_args: object, **_kwargs: object) -> str:
        active_models.append(ctx.active_model_name)
        return "final text"

    request = replace(
        _plain_request(_target(thread_id=thread_id)),
        scheduled_model=scheduled_model,
        media=media,
    )
    with (
        patch("mindroom.response_runner.select_cold_cache_model", new=fake_select),
        patch.object(DeliveryGateway, "send_text", new=AsyncMock(return_value="$placeholder")),
        patch.object(
            DeliveryGateway,
            "deliver_final",
            new=AsyncMock(return_value=completed_outcome("$response", body="final text")),
        ),
        patch_response_runner_module(
            stream_agent_response=streamed_answer(fake_ai_response),
            should_use_streaming=AsyncMock(return_value=False),
            typing_indicator=_noop_typing,
        ),
    ):
        for _ in range(replies):
            await coordinator.generate_response(request)
    return active_models, selector_calls


@pytest.mark.asyncio
async def test_cold_cache_model_used_for_reply(tmp_path: Path) -> None:
    """An accepted cold-cache judgment replies with the smaller model, judged on this thread and prompt."""
    active_models, selector_calls = await _cold_cache_reply_model(tmp_path)
    assert active_models == ["cheap"]
    assert len(selector_calls) == 1
    call = selector_calls[0]
    assert call["agent_name"] == "general"
    assert call["agent_model"] == "default"
    assert call["cheap_model"] == "cheap"
    assert call["idle_seconds"] is None
    assert call["context"] == ()
    assert call["prompt"] == "hello"


@pytest.mark.asyncio
async def test_cold_cache_switch_stays_with_the_thread(tmp_path: Path) -> None:
    """An accepted switch is saved as the thread's model, so later turns stay on it without another judgment."""
    active_models, selector_calls = await _cold_cache_reply_model(tmp_path, replies=2)
    assert active_models == ["cheap", "cheap"]
    assert len(selector_calls) == 1


@pytest.mark.asyncio
async def test_cold_cache_failed_switch_save_keeps_agent_model(tmp_path: Path) -> None:
    """A switch that cannot be saved is skipped, and the reply still runs on the agent model."""
    with patch("mindroom.response_runner.set_thread_model_override", side_effect=OSError("disk full")):
        active_models, selector_calls = await _cold_cache_reply_model(tmp_path)
    assert len(selector_calls) == 1
    assert active_models == ["default"]


@pytest.mark.asyncio
async def test_cold_cache_uses_default_cheap_model(tmp_path: Path) -> None:
    """An agent without its own cheap_model switches to defaults.cheap_model."""
    active_models, selector_calls = await _cold_cache_reply_model(tmp_path, inherit_cheap_model=True)
    assert active_models == ["cheap"]
    assert [call["cheap_model"] for call in selector_calls] == ["cheap"]


@pytest.mark.asyncio
async def test_cold_cache_skipped_in_room_mode(tmp_path: Path) -> None:
    """Without a thread there is nothing to switch for good, so room-mode replies keep the agent model."""
    active_models, selector_calls = await _cold_cache_reply_model(tmp_path, thread_id=None)
    assert selector_calls == []
    assert active_models == ["default"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("override", "expected_model"),
    [
        ({"scheduled_model": "cheap2"}, "cheap2"),
        ({"room_model": "cheap2"}, "cheap2"),
        # An override naming the agent's own model is still an explicit choice.
        ({"room_model": "default"}, "default"),
        ({"thread_model": "default"}, "default"),
        ({"media": MediaInputs(images=[Image(content=b"png")])}, "default"),
    ],
)
async def test_cold_cache_skipped_for_override(
    tmp_path: Path,
    override: dict[str, object],
    expected_model: str,
) -> None:
    """Explicit model choices and media turns never consult the cold-cache judge."""
    active_models, selector_calls = await _cold_cache_reply_model(tmp_path, **override)
    assert selector_calls == []
    assert active_models == [expected_model]


@pytest.mark.asyncio
@pytest.mark.parametrize("edit_regeneration", [False, True])
async def test_cold_cache_skipped_for_edit_regeneration(tmp_path: Path, edit_regeneration: bool) -> None:
    """An edit regenerates the answer the agent model just gave, so its cache is warm and no judge runs."""
    bot = _bot(tmp_path)
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    config = coordinator.deps.runtime.config
    config.models["cheap"] = ModelConfig(provider="openai", id="cheap-model")
    config.agents["general"] = config.agents["general"].model_copy(
        update={"cheap_model": "cheap", "cold_cache": ColdCacheConfig()},
    )
    select = AsyncMock(return_value="cheap")
    request = replace(_plain_request(_target(thread_id="$event")), edit_regeneration=edit_regeneration)
    with (
        patch("mindroom.response_runner.select_cold_cache_model", new=select),
        patch("mindroom.response_runner.set_thread_model_override"),
    ):
        chosen = await coordinator._cold_cache_model(
            request,
            runtime_model=TurnModel(
                model_name="default",
                requested_model_name="default",
                context_window=None,
                overridden=False,
            ),
            thread_id="$event",
        )
    assert (chosen, select.await_count) == (("default", 0) if edit_regeneration else ("cheap", 1))


@pytest.mark.asyncio
async def test_scheduled_model_overrides_room_default_for_one_response(tmp_path: Path) -> None:
    """A schedule uses its model even with a room override, without changing later turns."""
    bot = _bot(tmp_path)
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    config = coordinator.deps.runtime.config
    config.models["large"] = ModelConfig(provider="openai", id="large-model")
    config.models["cheap"] = ModelConfig(provider="openai", id="cheap-model")
    set_room_model_override(
        coordinator.deps.runtime_paths,
        room_id="!room:localhost",
        model_name="large",
    )
    active_models: list[str | None] = []

    async def fake_ai_response(ctx: ResponseTurnContext, *_args: object, **_kwargs: object) -> str:
        active_models.append(ctx.active_model_name)
        return "final text"

    with (
        patch.object(DeliveryGateway, "send_text", new=AsyncMock(return_value="$placeholder")),
        patch.object(
            DeliveryGateway,
            "deliver_final",
            new=AsyncMock(return_value=completed_outcome("$response", body="final text")),
        ),
        patch_response_runner_module(
            stream_agent_response=streamed_answer(fake_ai_response),
            should_use_streaming=AsyncMock(return_value=False),
            typing_indicator=_noop_typing,
        ),
    ):
        await coordinator.generate_response(replace(_plain_request(_target()), scheduled_model="cheap"))

    assert active_models == ["cheap"]
    runtime = await coordinator.prepare_response_runtime(_plain_request(_target()), show_tool_calls=True)
    assert runtime.active_model_name == "large"


@pytest.mark.asyncio
@pytest.mark.parametrize("authorized", [True, False])
@pytest.mark.parametrize("call_count", [1, 2])
async def test_cli_approval_claims_native_owner_and_wakes_without_source_lock(
    tmp_path: Path,
    authorized: bool,
    call_count: int,
) -> None:
    """A live CLI waiter gets the same one-attempt claim before any effects."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    store = runner.deps.replies.store
    await _admit_approval_source(store)
    request = _plain_request(_target(thread_id="$thread"), source_event_id="$source")
    tools = tuple(
        ToolExecution(
            tool_call_id=f"inner-{index}",
            tool_name="dangerous",
            tool_args={"value": index},
            requires_confirmation=True,
            approval_type="mindroom_policy",
        )
        for index in range(call_count)
    )
    paused = _ordered_pause(
        PausedAttempt(
            session_id="session-1",
            run_id="run-1",
            requirements=tuple(RunRequirement(item) for item in tools),
            toolkit_owners={("general", "dangerous"): "test_toolkit"},
            cli_call={"kind": "agent_cli", "call_id": "inner", "parent_bash_call_id": "bash"},
        ),
    )
    identity = runner.deps.tool_runtime.build_execution_identity(
        target=request.response_envelope.target,
        user_id=request.user_id,
    )
    waiter = asyncio.Event()
    runner.cli_waiters.register(("$source",), waiter)

    async def publish(continuation, plan, **kwargs):  # noqa: ANN001, ANN003, ANN202, ARG001
        await store.approvals.activate_approval_continuation(continuation.approval_id, expected_generation=0)
        assert await runner.handoff_approval_source("$source") is False
        assert waiter.is_set()

    with (
        patch("mindroom.approval.response.resolve_tool_approval_approver", return_value="@user:localhost"),
        patch("mindroom.approval.response.evaluate_tool_approval", new=AsyncMock(return_value=(False, 60.0))),
        patch.object(runner.approvals, "_publish_generation", new=publish),
        patch.object(LockedTurns, "request_remains_authorized", new=AsyncMock(return_value=authorized)),
    ):
        async with _in_reply_span(runner, request) as (_edit_text, _send_text):
            operation = runner.approvals._cli_approval_waits.wait(
                paused,
                waiter=waiter,
                source=request.response_envelope.source_event_id,
                target=request.response_envelope.target,
                publish=partial(
                    runner.approvals.suspend_for_approval,
                    request=request,
                    target=request.response_envelope.target,
                    execution_identity=identity,
                    history_scope=runner.deps.state_writer.history_scope(),
                    entity_kind="agent",
                    show_tool_calls=True,
                ),
                authorize=lambda: runner.locked.request_remains_authorized(request),
            )
            if authorized:
                resolved = await operation
                assert len(resolved) == call_count
                assert all(item.confirmation is True for item in resolved)
                current = await store.approvals.approval_continuation_for_source("$source")
                assert current.state == "claimed"
                assert current.cli_call == paused.cli_call
                assert await claim_continuation(store, current.approval_id, runtime_generation="rival") is None
            else:
                with pytest.raises(PermissionError):
                    await operation
                current = await store.approvals.approval_continuation_for_source("$source")
                assert current.state != "claimed"


@pytest.mark.asyncio
async def test_cli_wait_reports_failed_card_publication(tmp_path: Path) -> None:
    """A CLI caller sees why its approval card failed, not a generic ownership loss."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    await _admit_approval_source(runner.deps.replies.store)
    request = _plain_request(_target(thread_id="$thread"), source_event_id="$source")
    tool = ToolExecution(
        tool_call_id="inner",
        tool_name="dangerous",
        requires_confirmation=True,
        approval_type="mindroom_policy",
    )
    paused = _ordered_pause(
        PausedAttempt(
            session_id="session-1",
            run_id="run-1",
            requirements=(RunRequirement(tool),),
            toolkit_owners={("general", "dangerous"): "test_toolkit"},
            cli_call={"kind": "agent_cli", "call_id": "inner", "parent_bash_call_id": "bash"},
        ),
    )

    async def publish(continuation, plan, **kwargs):  # noqa: ANN001, ANN003, ANN202, ARG001
        msg = "approval card send failed"
        raise RuntimeError(msg)

    with (
        patch("mindroom.approval.response.resolve_tool_approval_approver", return_value="@user:localhost"),
        patch("mindroom.approval.response.evaluate_tool_approval", new=AsyncMock(return_value=(False, 60.0))),
        patch.object(runner.approvals, "_publish_generation", new=publish),
        pytest.raises(RuntimeError, match="approval card send failed"),
    ):
        async with _in_reply_span(runner, request) as (_edit_text, _send_text):
            await runner.approvals._cli_approval_waits.wait(
                paused,
                waiter=asyncio.Event(),
                source="$source",
                target=request.response_envelope.target,
                publish=partial(
                    runner.approvals.suspend_for_approval,
                    request=request,
                    target=request.response_envelope.target,
                    execution_identity=runner.deps.tool_runtime.build_execution_identity(
                        target=request.response_envelope.target,
                        user_id=request.user_id,
                    ),
                    history_scope=runner.deps.state_writer.history_scope(),
                    entity_kind="agent",
                    show_tool_calls=True,
                ),
                authorize=AsyncMock(return_value=True),
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_source", ["user_stop", "interrupted", "shutdown", "rival"])
async def test_cli_approval_scope_retains_only_shutdown_waits(tmp_path: Path, cancel_source: str) -> None:
    """The real response task, not its spawned tool task, owns cancellation provenance."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    store = runner.deps.replies.store
    await _admit_approval_source(store)
    request = _plain_request(_target(thread_id="$thread"), source_event_id="$source")
    target = request.response_envelope.target
    continuation = approval_continuation(
        approval_id="cli-wait",
        run_id="run",
        session_id=target.session_id,
        room_id=target.room_id,
        thread_id=target.resolved_thread_id,
        requester_id=request.user_id,
        response_event_id="$response",
        sources=request.sources,
        state="claimed" if cancel_source == "rival" else "waiting",
        runtime_generation="other-process" if cancel_source == "rival" else None,
        cli_call={"kind": "agent_cli"},
    )
    assert await paused_for_approval(store, continuation) is not None
    runtime = PreparedResponseRuntime(
        media_inputs=MediaInputs(),
        model_prompt="test",
        active_model_name="default",
        show_tool_calls=False,
        tool_context=runner.deps.tool_runtime.build_turn_context(target, user_id=request.user_id),
    )
    started = asyncio.Event()

    async def wait_for_decision(*_args: object, **_kwargs: object) -> tuple[RunRequirement, ...]:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError

    async def response() -> None:
        async with runner.approvals.cli_approval_scope(
            runtime,
            request=request,
            progress=TurnProgress(),
            history_scope=runner.deps.state_writer.history_scope(),
        ) as scoped:
            context = scoped.tool_context
            assert context.cli_approval_handler is not None
            await context.cli_approval_handler(cast("PausedAttempt", None))

    with patch.object(runner.approvals._cli_approval_waits, "wait", new=wait_for_decision):
        task = asyncio.create_task(response())
        await started.wait()
        if cancel_source == "shutdown":
            request_task_cancel(task, process_shutdown=True)
        else:
            task.cancel(cancel_source)
        await asyncio.gather(task, return_exceptions=True)
    current = await store.approvals.approval_continuation("cli-wait")
    assert current is not None
    assert current.state == (
        "claimed" if cancel_source == "rival" else "waiting" if cancel_source == "shutdown" else "failing"
    )
    assert (
        current.failure
        == {
            "shutdown": None,
            "rival": None,
            "user_stop": ApprovalFailure(rl.ApprovalFailureKind.USER_STOP),
            "interrupted": ApprovalFailure(rl.ApprovalFailureKind.INTERRUPTED),
        }[cancel_source]
    )


@pytest.mark.asyncio
async def test_cli_approval_scope_without_cli_pause_reads_no_approval_state(tmp_path: Path) -> None:
    """Standard responses pass through the CLI scope without touching approval storage."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    request = _plain_request(_target(thread_id="$thread"), source_event_id="$source")
    target = request.response_envelope.target
    runtime = PreparedResponseRuntime(
        media_inputs=MediaInputs(),
        model_prompt="test",
        active_model_name="default",
        show_tool_calls=False,
        tool_context=runner.deps.tool_runtime.build_turn_context(target, user_id=request.user_id),
    )
    progress = TurnProgress(ending=AwaitingApproval())
    with patch.object(
        type(runner.approvals._cli_approval_waits.store),
        "approval_continuation_for_source",
        new=AsyncMock(side_effect=AssertionError("standard responses own no CLI approval state")),
    ):
        async with runner.approvals.cli_approval_scope(
            runtime,
            request=request,
            progress=progress,
            history_scope=runner.deps.state_writer.history_scope(),
        ):
            pass


@pytest.mark.asyncio
@pytest.mark.parametrize("ready", [False, True])
@pytest.mark.parametrize("live", [False, True])
async def test_expired_cli_grant_hands_exact_approval_to_native_resume(  # noqa: C901, PLR0915 - one native expiry/teardown race
    tmp_path: Path,
    ready: bool,
    live: bool,
) -> None:
    """Expiry leaves the same durable card unclaimed and detaches the live waiter."""
    bot = _bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    store = runner.deps.replies.store
    await _admit_approval_source(store)
    request = _plain_request(_target(thread_id="$thread"), source_event_id="$source")
    target = request.response_envelope.target
    runtime = PreparedResponseRuntime(
        media_inputs=MediaInputs(),
        model_prompt="test",
        active_model_name="default",
        show_tool_calls=False,
        tool_context=runner.deps.tool_runtime.build_turn_context(target, user_id=request.user_id),
    )
    tool = ToolExecution(
        tool_call_id="inner",
        tool_name="dangerous",
        tool_args={"value": 7},
        requires_confirmation=True,
        approval_type="mindroom_policy",
    )
    paused = _ordered_pause(
        PausedAttempt(
            session_id=target.session_id,
            run_id="run-1",
            requirements=(RunRequirement(tool),),
            toolkit_owners={("general", "dangerous"): "test_toolkit"},
            cli_call={
                "kind": "agent_cli",
                "call_id": "inner",
                "parent_bash_call_id": "bash",
                "arguments": {"value": 7},
            },
        ),
    )
    published = []
    retry = MagicMock()
    runner = replace_response_runner_deps(bot, retry_approval_sources=retry)

    async def publish(continuation, plan, **kwargs):  # noqa: ANN001, ANN003, ANN202, ARG001
        published.append(continuation.approval_id)
        if live:
            assert await store.approvals.reserve_reply_cards(
                continuation_principal_id=store.principal_id,
                continuation_id=continuation.approval_id,
                expected_generation=0,
                cards=(
                    ApprovalCardReservation(
                        delivery_id="expiry-card",
                        tool_call_id="inner",
                        event_type="io.mindroom.tool_approval",
                        approver_user_id="@user:example.org",
                        approvable=True,
                        payload={
                            "approval_id": "expiry-card",
                            "continuation_id": continuation.approval_id,
                            "continuation_generation": 0,
                            "tool_call_id": "inner",
                            "tool_name": "dangerous",
                            "arguments": {"value": 7},
                            "status": "pending",
                        },
                    ),
                ),
            )
            assert await store.claim_matrix_delivery(delivery_id="expiry-card", stage=DeliveryStage.INITIAL) is not None
            await store.record_matrix_delivery_device(
                delivery_id="expiry-card",
                stage=DeliveryStage.INITIAL,
                device_id="device",
            )
            await store.acknowledge_matrix_delivery(
                delivery_id="expiry-card",
                stage=DeliveryStage.INITIAL,
                event_id="$card",
                delivered_projections=(),
            )
        elif ready:
            await store.approvals.activate_approval_continuation(continuation.approval_id, expected_generation=0)

    clock = SimpleNamespace(now=cli_approval_waits.time.time_ns())
    original_timeout = asyncio.timeout
    entered = asyncio.Event()
    stopped = asyncio.Event()
    observed = []
    real_read = store.approvals.approval_continuation_for_source
    raced = []

    @asynccontextmanager
    async def observe_timeout(delay):  # noqa: ANN001, ANN202
        async with original_timeout(delay) as timeout:
            if live and delay is not None and delay > 3600:
                observed.append(timeout)
                entered.set()
            yield timeout

    async def read_source(_store, source):  # noqa: ANN001, ANN202
        if live and ready and published and not runner.cli_waiters._waiters and not raced:
            raced.append(True)
            decision = await resolve_card_event(
                store.approvals,
                room_id=_target().room_id,
                card_event_id="$card",
                requested_status="approved",
                reason=None,
                metadata=ApprovalDecisionMetadata(resolved_by=request.user_id),
            )
            assert decision.recorded
            assert decision.continuation_ready
        return await real_read(source)

    with (
        patch.object(cli_approval_waits, "time", SimpleNamespace(time_ns=lambda: clock.now)),
        patch.object(asyncio, "timeout", observe_timeout),
        patch.object(type(store.approvals), "approval_continuation_for_source", read_source),
        patch("mindroom.approval.response.resolve_tool_approval_approver", return_value="@user:localhost"),
        patch(
            "mindroom.approval.response.evaluate_tool_approval",
            new=AsyncMock(return_value=(live or not ready, 60.0)),
        ),
        patch.object(runner.approvals, "_publish_generation", new=publish),
        patch.object(runner.approvals, "_request_failure", new=AsyncMock()) as fail,
    ):
        async with _in_reply_span(runner, request) as (_edit_text, _send_text):
            async with response_cli_lifetime() as lifetime:
                lifetime.grant_expires_at_ns = clock.now + 86400 * 10**9 if live else 1
                catalog = await _catalog(tmp_path / "owner", [])
                owner = live_turn_tools_for_test(
                    CliTurnOwner(
                        catalog.runtime_context.execution_identity,
                        "turn",
                        "run",
                    ),
                    catalog=catalog,
                    authorize=AsyncMock(),
                    output_file_policy=ToolOutputFilePolicy(tmp_path / "owner" / "workspace"),
                )
                registry = TurnToolRegistry()
                lifetime.register(owner, registry)
                grant = owner.issue(now_ns=clock.now, expires_at_ns=clock.now + 86400 * 10**9)
                async with runner.approvals.cli_approval_scope(
                    runtime,
                    request=request,
                    progress=TurnProgress(),
                    history_scope=runner.deps.state_writer.history_scope(),
                ) as bound:
                    context = bound.tool_context
                    assert context.cli_approval_handler is not None
                    if live:

                        async def wait_for_approval() -> str:
                            await context.cli_approval_handler(paused)
                            pytest.fail("Expired owner must not receive execution authority")

                        outcomes = []

                        async def source():  # noqa: ANN202
                            try:
                                # The tool call turns the pause the tool raised into a failed outcome; the side channel
                                # must still suspend.
                                outcome = await execute_tool_call(
                                    adapt_callable(wait_for_approval),
                                    {},
                                    ToolCallContext(run_id="run-1", session_id=target.session_id, user_id=None),
                                    middleware=(),
                                )
                                outcomes.append(outcome)
                                yield "swallowed tool error"
                                await asyncio.Event().wait()
                            finally:
                                stopped.set()

                        stream = stream_cli_events(source())
                        pulling = asyncio.create_task(anext(stream))
                        await asyncio.wait_for(entered.wait(), timeout=2)
                        before = await real_read("$source")
                        assert before is not None
                        assert before.state == "waiting"
                        assert registry.resolve("Bearer " + grant.raw_token, now_ns=clock.now) is owner
                        clock.now = lifetime.grant_expires_at_ns
                        observed[0].reschedule(asyncio.get_running_loop().time())
                        with pytest.raises(ResponsePausedForApproval) as caught:
                            await asyncio.wait_for(pulling, timeout=2)
                        await stream.aclose()
                        assert stopped.is_set()
                        assert [outcome.is_error for outcome in outcomes] == [True]
                    else:
                        with pytest.raises(ResponsePausedForApproval) as caught:
                            await asyncio.wait_for(context.cli_approval_handler(paused), timeout=1)
                    assert caught.value.paused.cli_call == paused.cli_call
                    outcome = await runner.approvals.suspend_for_approval(
                        caught.value.paused,
                        request=request,
                        target=target,
                        execution_identity=runtime.tool_context.execution_identity,
                        entity_kind="agent",
                        history_scope=runner.deps.state_writer.history_scope(),
                        show_tool_calls=False,
                    )
                    assert outcome.ending == AwaitingApproval()
            with pytest.raises(CliAuthenticationError):
                registry.resolve("Bearer " + grant.raw_token, now_ns=grant.expires_at_ns - 1)
            current = await store.approvals.approval_continuation_for_source("$source")
            assert current is not None
            assert current.approval_id == published[0]
            assert len(published) == 1
            assert current.state == ("ready" if ready else "waiting")
            assert current.cli_call == paused.cli_call
            assert not runner.cli_waiters._waiters
            fail.assert_not_awaited()
            assert retry.called is ready
            assert bool(raced) is (live and ready)
            if live:
                assert current.calls[0].tool_call_id == "inner"
                assert current.runtime_generation is None


@pytest.mark.asyncio
@pytest.mark.parametrize("spent", [1, 2])
async def test_cli_pause_after_control_rebuild_persists_spent_continuation_budget(
    tmp_path: Path,
    spent: int,
) -> None:
    """A live hidden approval checkpoint retains the parent's consumed continuation attempt."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    store = runner.deps.replies.store
    await _admit_approval_source(store)
    request = _plain_request(_target(thread_id="$thread"), source_event_id="$source")
    target = request.response_envelope.target
    tool_context = runner.deps.tool_runtime.build_turn_context(target, user_id=request.user_id)
    tool = ToolExecution(
        tool_call_id="hidden",
        tool_name="dangerous",
        tool_args={},
        requires_confirmation=True,
        approval_type="mindroom_policy",
    )
    paused = _ordered_pause(
        PausedAttempt(
            session_id=target.session_id,
            run_id="run-2",
            requirements=(RunRequirement(tool),),
            toolkit_owners={("general", "dangerous"): "test_toolkit"},
            cli_call={"kind": "agent_cli", "call_id": "hidden", "parent_bash_call_id": "bash"},
        ),
    )
    attempts = []

    async def attempt(_run, _continuation):  # noqa: ANN001, ANN202
        attempts.append(True)
        if len(attempts) <= spent:
            return CompletedAttempt(control_calls=(_model_switch_execution("after-toolcall").call,))
        lifetime = current_cli_lifetime()
        assert lifetime is not None
        lifetime.grant_expires_at_ns = 1
        await runner.approvals._cli_approval_waits.wait(
            paused,
            waiter=asyncio.Event(),
            source=request.response_envelope.source_event_id,
            target=target,
            publish=partial(
                runner.approvals.suspend_for_approval,
                request=request,
                target=target,
                execution_identity=tool_context.execution_identity,
                history_scope=runner.deps.state_writer.history_scope(),
                entity_kind="agent",
                show_tool_calls=False,
            ),
            authorize=lambda: runner.locked.request_remains_authorized(request),
        )
        pytest.fail("expired live approval must suspend")

    async def streamed_attempt(run, continuation):  # noqa: ANN001, ANN202
        yield AttemptResolved(await attempt(run, continuation))

    async def execute() -> None:
        async for _chunk in stream_response_turn(
            _ctx(),
            _streaming_adapter(streamed_attempt),
            TurnSinks(),
            continuation=_continuation(),
        ):
            pass

    with (
        patch("mindroom.approval.response.resolve_tool_approval_approver", return_value="@user:localhost"),
        patch("mindroom.approval.response.evaluate_tool_approval", new=AsyncMock(return_value=(True, 60.0))),
        patch.object(runner.approvals, "_publish_generation", new=AsyncMock()),
        pytest.raises(ResponsePausedForApproval) as raised,
    ):
        async with _in_reply_span(runner, request) as (_edit_text, _send_text):
            await execute()
    assert len(attempts) == spent + 1
    stored = await store.approvals.approval_continuation_for_source("$source")
    assert stored is not None
    assert stored.continuation_count == spent
    assert raised.value.paused.continuation_count == spent


@pytest.mark.asyncio
@pytest.mark.parametrize("suspend", [None, "live", "native"])
async def test_claimed_cli_recovery_owns_chained_approval_scope(tmp_path: Path, suspend: str | None) -> None:
    """Recovered execution advances the native claim and preserves a new pending pause."""
    bot = _bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    store = runner.deps.replies.store
    await _admit_approval_source(store)
    target = _target(thread_id="$thread")
    request = _plain_request(target, source_event_id="$source")
    identity = runner.deps.tool_runtime.build_execution_identity(target=target, user_id=request.user_id)
    continuation = approval_continuation(
        approval_id="recovery-chain",
        run_id="saved-run",
        session_id=target.session_id,
        room_id=target.room_id,
        thread_id=target.resolved_thread_id,
        requester_id=request.user_id,
        sources=request.sources,
        execution_identity=identity,
        cli_call={"kind": "agent_cli"},
        show_tool_calls=False,
    )
    assert await paused_for_approval(store, continuation) is not None
    published = []
    decisions: list[asyncio.Task[None]] = []

    async def publish(current, plan, **kwargs):  # noqa: ANN001, ANN003, ANN202, ARG001
        published.append(current.generation)

        async def decide() -> None:
            await asyncio.sleep(0)
            await store.approvals.activate_approval_continuation(
                current.approval_id,
                expected_generation=current.generation,
            )
            bot.retry_approval_sources(current.room_id, current.source_event_ids)

        decision = asyncio.create_task(decide())
        decisions.append(decision)

    async def recover(_self, current, *, tool_context, **kwargs):  # noqa: ANN001, ANN003, ANN202, ARG001
        assert tool_context.cli_approval_handler is not None
        for index in range(2):
            tool = ToolExecution(
                tool_call_id=f"next-{index}",
                tool_name="dangerous",
                requires_confirmation=True,
                approval_type="mindroom_policy",
            )
            paused = PausedAttempt(
                session_id=target.session_id,
                run_id="saved-run",
                requirements=(RunRequirement(tool),),
                toolkit_owners={("general", "dangerous"): "test_toolkit"},
                cli_call={"kind": "agent_cli", "call_id": tool.tool_call_id, "parent_bash_call_id": "old-bash"},
            )
            if suspend == "native":
                return paused
            if suspend == "live":
                async with response_cli_lifetime() as lifetime:
                    lifetime.grant_expires_at_ns = 1
                    try:
                        await tool_context.cli_approval_handler(paused)
                    except ResponsePausedForApproval as error:
                        return error.paused
            else:
                resolved = await tool_context.cli_approval_handler(paused)
                assert resolved[0].confirmation is True
        return CompletedApprovalRun(response_text="finished both approvals", metadata_content={})

    with (
        patch.object(type(runner.approvals._approval_execution), "continue_run", new=recover),
        patch.object(runner.approvals, "_publish_generation", new=publish),
        patch.object(LockedTurns, "request_remains_authorized", new=AsyncMock(return_value=True)),
        patch("mindroom.approval.response.resolve_tool_approval_approver", return_value="@user:localhost"),
        patch("mindroom.approval.response.evaluate_tool_approval", new=AsyncMock(return_value=(False, 60.0))),
        patch.object(
            DeliveryGateway,
            "deliver_final",
            new=AsyncMock(return_value=completed_outcome("$waiting", body="finished")),
        ),
    ):
        completed: list[tuple[FinalDeliveryOutcome, ApprovalContinuation]] = []
        async with resumed_approval(bot, continuation) as claimed:

            async def recover_owned() -> None:
                completed.append(
                    await runner.approvals._execute_claimed_approval(
                        claimed,
                        request=request,
                        target=target,
                        identity=runner.locked.build_lifecycle(request, response_kind="ai").identity,
                    ),
                )

            recovery = runner.inbox.track_inbox_response(
                recover_owned(),
                name="approval_recovery_chain",
                room_id=target.room_id,
                recovery_proof_ready=lambda: True,
                source_event_ids=claimed.source_event_ids,
            )
            await asyncio.wait_for(recovery, timeout=2)
            await asyncio.gather(*decisions)
        result, current = completed[0]
    assert result.ending == (AwaitingApproval() if suspend else Completed())
    assert published == ([1] if suspend else [1, 2])
    saved = await store.approvals.approval_continuation(continuation.approval_id)
    # A new pause stays ready; completion is left for the lifecycle owner to settle after post-response effects.
    assert (saved.state, saved.cli_call["call_id"]) == (("ready", "next-0") if suspend else ("claimed", "next-1"))
    assert current.generation == (1 if suspend else 2)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [None, "partial", "missing_source", "media", "secret", "oversized", "degraded", "refresh_error"],
)
async def test_mid_turn_uses_refreshed_public_context_before_current_sources(
    tmp_path: Path,
    failure: str | None,
) -> None:
    """A resumed turn retains its task and corrections, excluding future and private input."""
    bot = _bot(tmp_path)
    history = [
        make_visible_message(
            sender="@user:localhost",
            body="Sleep thirty times for two seconds each",
            event_id="$root",
        ),
        make_visible_message(
            sender="@user:localhost",
            body="Actually, make each sleep three seconds",
            event_id="$correction",
        ),
        make_visible_message(sender="@user:localhost", body="Continue", event_id="$resume"),
        make_visible_message(sender="@user:localhost", body="Future queued text", event_id="$future"),
    ]
    if failure == "missing_source":
        history.pop(2)
    if failure == "media":
        history[0].content["msgtype"] = "m.image"
    if failure == "secret":
        history[0].body = "My API key is sk-" + "a" * 48
    if failure == "oversized":
        history[0].body = "Sleep thirty times for two seconds each. " + "x" * 16001 + " Log every sleep."
    refreshed = ThreadHistoryResult(
        history,
        is_full_history=failure != "partial",
        diagnostics={"thread_read_degraded": True} if failure == "degraded" else {},
    )
    resolver = MagicMock(spec=ConversationResolver)
    resolver.fetch_thread_history = AsyncMock(return_value=refreshed)
    if failure == "refresh_error":
        resolver.fetch_thread_history.side_effect = RuntimeError("Unavailable")
    runner = runner_for(replace(unwrap_extracted_collaborator(bot._response_runner).deps, resolver=resolver))
    target = _target(thread_id="$root", reply_to_event_id="$resume")
    envelope = _envelope(target, source_event_id="$resume")
    request = ResponseRequest(
        sources=ResponseSources(pending_event_ids=("$resume",), logical_source_event_ids=("$resume",)),
        thread_history=[
            make_visible_message(sender="@user:localhost", body="Stale input", event_id="$stale"),
            make_visible_message(sender="@user:localhost", body="Continue", event_id="$resume"),
        ],
        prompt="Continue",
        user_id="@user:localhost",
        response_envelope=envelope,
    )
    payloads = []

    async def evaluate(judgment: JudgmentRequest) -> bool:
        assert judgment.body is not None
        payloads.append(json.loads(judgment.body))
        return True

    gate = MidTurnGate(active_text="Continue", evaluate=evaluate)
    with queued_message_signal_context(None, mid_turn_gate=gate):
        await runner._prepare_request_after_lock(request)
        finish = await gate.defers((QueuedMessage("$new", "Only two more sleeps, please"),), current=lambda: True)
    if failure not in {None, "oversized", "media"}:
        assert not finish
        assert payloads == []
    else:
        assert finish
        conversation = payloads[0]["state"]["conversation"]
        evidence = conversation[-1]["text"]
        assert conversation[1:-1] == [{"role": "user", "text": "Actually, make each sleep three seconds"}]
        root = conversation[0]["text"]
        # Earlier media reaches the judge as a placeholder rather than disabling judgment.
        assert root.startswith("[image: " if failure == "media" else "Sleep thirty times for two seconds each")
        if failure == "oversized":
            # A long message reaches the judge as its start and end rather than disabling judgment.
            assert root.endswith("Log every sleep.")
            assert "characters omitted" in root
        assert evidence.startswith("<active_request>\nContinue\n</active_request>\n")
        assert "Stale input" not in str(payloads)
        assert "Future queued text" not in str(payloads)


@pytest.mark.asyncio
async def test_approval_continuation_refreshes_mid_turn_context_without_replacing_native_input(tmp_path: Path) -> None:
    """Approval replay also supplies the judge's public task context before resuming tools."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    runner.deps.runtime.config.agents["general"].mid_turn = MidTurnConfig.model_validate(
        {"judgment": {"provider": "typesafe"}},
    )
    continuation = approval_continuation(
        approval_id="approval-context",
        thread_id="$root",
        sources=ResponseSources(("$resume",), ("$resume",)),
    )
    target = _target(thread_id="$root", reply_to_event_id="$resume")
    original = runner.approvals.approval_response_request(continuation, target=target)
    refreshed = ThreadHistoryResult(
        [
            make_visible_message(sender="@user:localhost", body="Thirty sleeps", event_id="$root"),
            make_visible_message(sender="@user:localhost", body="Continue", event_id="$resume"),
        ],
        is_full_history=True,
    )
    captured = []

    async def evaluate(request: JudgmentRequest) -> bool:
        assert request.body is not None
        captured.append(json.loads(request.body)["state"]["conversation"])
        return True

    gate = MidTurnGate(active_text="Continue", evaluate=evaluate, conversation_context=None)

    with (
        queued_message_signal_context(None, mid_turn_gate=gate),
        patch.object(runner.deps.resolver, "fetch_thread_history", new=AsyncMock(return_value=refreshed)),
        patch.object(
            LockedTurns,
            "build_lifecycle",
            side_effect=RuntimeError("stop before generation"),
        ) as build_lifecycle,
    ):
        with pytest.raises(RuntimeError, match="stop before generation"):
            await runner.approvals._run_claimed_approval_lifecycle(continuation, target=target)
        assert await gate.defers((QueuedMessage("$new", "Thanks"),), current=lambda: True)
    assert build_lifecycle.call_args.args[0].thread_history == original.thread_history
    assert captured[0][0] == {"role": "user", "text": "Thirty sleeps"}


@pytest.mark.asyncio
@pytest.mark.parametrize(("thread_id", "finish"), [(None, False), ("$root", False), ("$resume", True)])
async def test_mid_turn_empty_history_only_allows_a_proven_new_thread(
    tmp_path: Path,
    thread_id: str | None,
    *,
    finish: bool,
) -> None:
    """Missing room or existing-thread context cannot authorize continuing an unknown task."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    target = _target(thread_id=thread_id, reply_to_event_id="$resume")
    request = replace(_plain_request(target, source_event_id="$resume"), prompt="Continue")
    evaluated = []

    async def evaluate(judgment: JudgmentRequest) -> bool:
        evaluated.append(judgment)
        return True

    gate = MidTurnGate(active_text="Continue", evaluate=evaluate, conversation_context=None)
    with (
        queued_message_signal_context(None, mid_turn_gate=gate),
        patch.object(
            runner.deps.resolver,
            "fetch_thread_history",
            new=AsyncMock(return_value=ThreadHistoryResult([], is_full_history=True)),
        ),
    ):
        await runner._prepare_request_after_lock(request)
        assert (
            await gate.defers((QueuedMessage("$new", "Only two more sleeps, please"),), current=lambda: True) is finish
        )
    assert len(evaluated) == int(finish)


@pytest.mark.asyncio
async def test_stop_while_progress_drains_lands_no_progress_edit_after_settlement(tmp_path: Path) -> None:
    """A stop during progress shutdown ends the in-flight edit before the reply settles as cancelled."""
    bot = _bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    # The turn whose answer the approval paused, which the Stop reaction names.
    await bot._turn_store.record_pending_turn(
        TurnRecord.create(
            ("$source",),
            response_event_id="$waiting",
            completed=False,
            response_owner="general",
            requester_id="@user:localhost",
            conversation_target=_target(thread_id="$thread", reply_to_event_id="$source"),
        ),
    )
    client = runner.locked.client()
    progress_started = asyncio.Event()
    settlement_started = asyncio.Event()
    exiting = asyncio.Event()
    in_flight: list[str] = []
    landed: list[str] = []

    async def slow_progress_send(**kwargs: object) -> nio.RoomSendResponse:
        new_content = cast("dict[str, Any]", kwargs["content"]).get("m.new_content")
        if new_content is None:
            return send(**kwargs)
        status = new_content[STREAM_STATUS_KEY]
        in_flight.append(status)
        try:
            if status == STREAM_STATUS_STREAMING:
                progress_started.set()
                await settlement_started.wait()
            else:
                settlement_started.set()
            landed.append(status)
            return send(**kwargs)
        finally:
            in_flight.remove(status)

    async def continue_call(
        _continuation: ApprovalContinuation,
        *,
        progress: ProgressPublisher | None,
        **_kwargs: object,
    ) -> CompletedApprovalRun:
        assert progress is not None
        await progress(StructuredStreamChunk(content="Checking the report."))
        await progress_started.wait()
        exiting.set()
        return CompletedApprovalRun(response_text="The report is clean.", metadata_content={})

    async with _resumed_streamable_approval(bot, requester_online=True) as claimed:
        send = client.room_send.side_effect
        client.room_send.side_effect = slow_progress_send
        with (
            patch.object(runner.approvals, "_continue_entity_call", new=continue_call),
            patch(
                "mindroom.approval.response.approval_manager.get_approval_store",
                return_value=MagicMock(expire_continuation_cards=AsyncMock(return_value=True)),
            ),
        ):
            lifecycle = asyncio.create_task(
                runner.approvals._run_claimed_approval_lifecycle(
                    claimed,
                    target=_target(thread_id="$thread", reply_to_event_id="$source"),
                ),
            )
            async with asyncio.timeout(5):
                await exiting.wait()
            assert await bot._user_stop_reconciler.finalize("$waiting", 7, room_id="!room:localhost")
            async with asyncio.timeout(5):
                ending = await lifecycle
            assert in_flight == []

    assert ending == Cancelled("user_stop")
    assert landed == [STREAM_STATUS_CANCELLED]
    assert _approval_reply_edits(client)[-1] == (
        STREAM_STATUS_CANCELLED,
        "Checking the report.\n\n**[Response cancelled by user]**",
    )


@dataclass
class _RecordedSkillReviews:
    """The orchestrator's skill-review runner as a response sees it, recording what the response asked of it."""

    cancelled: list[dict[str, object]] = field(default_factory=list)
    counted: list[dict[str, object]] = field(default_factory=list)
    fail: bool = False

    def cancel(self, _config: object, **scope: object) -> None:
        self.cancelled.append(scope)

    async def count(self, _config: object, **counted: object) -> None:
        self.counted.append(counted)
        if self.fail:
            msg = "count failed"
            raise OSError(msg)


async def _respond_with_skill_learning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request_update: Callable[[ResponseRequest], ResponseRequest] = lambda request: request,
    *,
    streaming: bool = False,
    fail: bool = False,
) -> tuple[_RecordedSkillReviews, ScriptedModel, AsyncMock]:
    bot = _bot(tmp_path)
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    assert bot.client is not None
    bot.client.room_send.return_value = nio.RoomSendResponse(event_id="$response", room_id="!room:localhost")
    coordinator.deps.runtime.config.agents["general"].skill_learning.enabled = True
    reviews = _RecordedSkillReviews(fail=fail)
    monkeypatch.setattr(bot.orchestrator, "_skill_reviews", reviews)
    model = ScriptedModel([answer("The scripted answer to the request.")])
    install_model_providers(monkeypatch, {"default": model})
    with patch_response_runner_module(
        typing_indicator=_noop_typing,
        should_use_streaming=AsyncMock(return_value=streaming),
    ):
        await coordinator.generate_response(request_update(_plain_request(_target())))
        assert await wait_for_background_tasks(SETTLE_TIMEOUT_SECONDS, owner=coordinator.deps.runtime)
    return reviews, model, bot.client.room_send


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_a_completed_response_hands_its_run_and_final_request_to_the_skill_reviews(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    streaming: bool,
) -> None:
    """Both response drivers stop the conversation's review, then count the completed run with its final request."""
    reviews, model, _send = await _respond_with_skill_learning(
        tmp_path,
        monkeypatch,
        lambda request: replace(request, correlation_id="$correlation"),
        streaming=streaming,
    )
    (counted,) = reviews.counted
    scope = {key: counted[key] for key in ("agent_name", "session_id", "identity")}
    assert reviews.cancelled == [scope]
    assert counted["agent_name"] == "general"
    assert counted["identity"].requester_id == "@user:localhost"
    assert counted["correlation_id"] == "$correlation"
    captured = counted["captured"]
    assert captured is not None
    assert captured.model is model
    assert captured.run_id == counted["run_id"]
    final = captured.request.messages[-1]
    assert (final.role, final.content, final.tool_calls) == ("assistant", "The scripted answer to the request.", None)
    assert "skill_manage" in {tool.name for tool in captured.request.tools}


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_a_completed_response_links_its_reply_event_onto_its_stored_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    streaming: bool,
) -> None:
    """Both agent response drivers record the Matrix reply event on the run they stored in the agent's session."""
    bot = _bot(tmp_path)
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    assert bot.client is not None
    bot.client.room_send.return_value = nio.RoomSendResponse(event_id="$response", room_id="!room:localhost")
    install_model_providers(monkeypatch, {"default": ScriptedModel([answer("The scripted answer to the request.")])})
    target = _target()
    with patch_response_runner_module(
        typing_indicator=_noop_typing,
        should_use_streaming=AsyncMock(return_value=streaming),
    ):
        await coordinator.generate_response(_plain_request(target))
        assert await wait_for_background_tasks(SETTLE_TIMEOUT_SECONDS, owner=coordinator.deps.runtime)

    [run] = stored_runs(coordinator.deps.runtime.config, coordinator.deps.runtime_paths, target.session_id)
    assert run.metadata is not None
    assert run.metadata[MATRIX_RESPONSE_EVENT_ID_METADATA_KEY] == "$response"


@pytest.mark.asyncio
@pytest.mark.parametrize("requested_by", ["a schedule", "a hook", "another agent", "a job follow-up"])
async def test_turns_no_person_asked_for_never_count_toward_skill_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    requested_by: str,
) -> None:
    """Like cron runs in Hermes, automation, agent requests, and job follow-ups have no person to learn from."""

    def update(request: ResponseRequest) -> ResponseRequest:
        envelope = request.response_envelope
        if requested_by == "a job follow-up":
            key = ConversationKey(
                "general",
                envelope.target.room_id,
                envelope.target.resolved_thread_id,
                request.user_id or "",
                silent=False,
            )
            follow_up = follow_up_envelope(
                key,
                follow_up_id="job-follow-up:1",
                sender_id="@mindroom_general:localhost",
                prompt="",
            )
            return replace(request, response_envelope=replace(envelope, origin=follow_up.origin))
        if requested_by in {"a schedule", "a hook"}:
            source_kind = SCHEDULED_SOURCE_KIND if requested_by == "a schedule" else HOOK_SOURCE_KIND
            return replace(
                request,
                response_envelope=request_envelope(
                    target=envelope.target,
                    prompt=request.prompt,
                    user_id=request.user_id,
                    source_kind=source_kind,
                ),
            )
        origin = message_origin(
            sender_id="@mindroom_helper:localhost",
            sender_entity_name="helper",
            requester_entity_name="helper",
        )
        return replace(request, response_envelope=replace(envelope, origin=origin))

    reviews, _model, send = await _respond_with_skill_learning(tmp_path, monkeypatch, update)
    send.assert_awaited()
    assert reviews.counted == []


@pytest.mark.asyncio
async def test_a_failing_skill_review_count_never_fails_the_reply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Skill learning is background bookkeeping, so a failed count must not cost the user their answer."""
    _reviews, _model, send = await _respond_with_skill_learning(tmp_path, monkeypatch, fail=True)
    send.assert_awaited()


@dataclass
class _RecordedAutomations:
    """The orchestrator's automation runner as a response sees it, recording each finished response."""

    finished: list[tuple[str, ...]] = field(default_factory=list)

    def response_finished(self, source_event_ids: Sequence[str]) -> None:
        self.finished.append(tuple(source_event_ids))


@pytest.mark.asyncio
async def test_a_finished_response_tells_automations_its_source_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An automation prompt's verify step waits for the response whose source is that prompt."""
    bot = _bot(tmp_path)
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    assert bot.client is not None
    bot.client.room_send.return_value = nio.RoomSendResponse(event_id="$response", room_id="!room:localhost")
    automations = _RecordedAutomations()
    monkeypatch.setattr(bot.orchestrator, "_automations", automations)
    model = ScriptedModel([answer("The scripted answer to the request.")])
    install_model_providers(monkeypatch, {"default": model})
    request = _plain_request(_target())
    with patch_response_runner_module(
        typing_indicator=_noop_typing,
        should_use_streaming=AsyncMock(return_value=False),
    ):
        await coordinator.generate_response(request)
        assert await wait_for_background_tasks(SETTLE_TIMEOUT_SECONDS, owner=coordinator.deps.runtime)

    assert automations.finished == [tuple(request.sources.logical_source_event_ids)]
    assert len(model.requests) == 1
    assert "The scripted answer to the request." in bot.client.room_send.await_args.kwargs["content"]["body"]


def test_a_finished_approval_continuation_tells_automations_its_source_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run that paused for approval is verified when its continuation ends, not after the hourly fallback."""
    bot = _bot(tmp_path)
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    automations = _RecordedAutomations()
    monkeypatch.setattr(bot.orchestrator, "_automations", automations)
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@user:localhost",
        room_id="!room:localhost",
        thread_id="$prompt",
        resolved_thread_id="$prompt",
        session_id="session-1",
    )
    continuation = approval_continuation(
        approval_id="approval-1",
        thread_id="$prompt",
        response_event_id="$response",
        sources=ResponseSources(("$prompt",), ("$prompt",)),
        state="running",
        execution_identity=identity,
    )

    deps = coordinator.approvals._approval_post_response_deps(continuation)
    assert deps.notify_response_finished is not None
    deps.notify_response_finished()

    assert automations.finished == [("$prompt",)]


@pytest.mark.asyncio
@pytest.mark.parametrize("entity_kind", ["agent", "team"])
async def test_a_finished_approval_continuation_links_its_reply_event_onto_the_run_it_resumed(
    tmp_path: Path,
    entity_kind: Literal["agent", "team"],
) -> None:
    """A resumed reply records its Matrix event on the run it continued, in the agent or team session holding it."""
    coordinator = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@user:localhost",
        room_id="!room:localhost",
        thread_id="$prompt",
        resolved_thread_id="$prompt",
        session_id="session-1",
    )
    is_team = entity_kind == "team"
    # Neither scope is the bot's own, so only the continuation's scope reaches the session that holds the run.
    scope = HistoryScope(kind=entity_kind, scope_id="crew" if is_team else "helper")
    if not is_team:
        coordinator.deps.runtime.config.agents["helper"] = AgentConfig(display_name="Helper")
    continuation = approval_continuation(
        entity_kind=entity_kind,
        history_scope=scope,
        execution_identity=identity,
    )
    storage = coordinator.deps.state_writer.create_storage(identity, scope=scope)
    session = new_scope_session(session_id="session-1", scope=scope)
    session.runs = [
        TeamRunRecord(run_id="run-1", team_id="crew", session_id="session-1", status=RunStatus.completed)
        if is_team
        else RunRecord(run_id="run-1", agent_id="helper", session_id="session-1", status=RunStatus.completed),
    ]
    seed_session(storage, session)

    deps = coordinator.approvals._approval_post_response_deps(continuation)
    assert deps.persist_response_event_id is not None
    await deps.persist_response_event_id("run-1", "$reply")

    stored = storage.load_session("session-1")
    assert stored is not None
    assert [run.metadata for run in stored.runs] == [{MATRIX_RESPONSE_EVENT_ID_METADATA_KEY: "$reply"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("succeeded", [True, False])
async def test_automations_hear_of_every_final_response(succeeded: bool) -> None:
    """A failed run must still reach verify, which asks for a re-check when a broken run left the files off."""
    calls: list[str] = []

    await apply_post_response_effects(
        FinalDeliveryOutcome(ending=Completed() if succeeded else Failed(FailureCause.ERROR)),
        ResponseOutcome(response_run_id="run-1", run_succeeded=succeeded),
        PostResponseEffectsDeps(logger=MagicMock(), notify_response_finished=lambda: calls.append("finished")),
    )
    assert calls == ["finished"]


@pytest.mark.asyncio
@pytest.mark.parametrize("succeeded", [True, False])
async def test_only_a_completed_response_counts_toward_its_skill_review(succeeded: bool) -> None:
    """Post-response effects count a completed response's run, and nothing for a failed one."""
    calls: list[str] = []

    async def count(run_id: str) -> None:
        calls.append(run_id)

    await apply_post_response_effects(
        FinalDeliveryOutcome(ending=Completed() if succeeded else Failed(FailureCause.ERROR)),
        ResponseOutcome(response_run_id="run-1", run_succeeded=succeeded),
        PostResponseEffectsDeps(logger=MagicMock(), queue_skill_review=count),
    )
    assert calls == (["run-1"] if succeeded else [])


@pytest.mark.parametrize(
    ("hook_source", "flushed"),
    [
        (None, True),
        ("automation/dreaming", False),
        ("automation/weekly_digest", False),
        ("digest:message:received", True),
    ],
)
def test_an_automation_turn_is_not_queued_for_memory(hook_source: str | None, flushed: bool) -> None:
    """A maintenance turn, such as an unreviewed memory proposal, never reaches Mem0; other turns do."""
    locked = LockedTurns(MagicMock())
    locked.deps.runtime.config = Config(
        agents={"mind": AgentConfig(display_name="Mind", memory_backend="mem0")},
    )

    queue = locked.memory_persistence(
        agent_name="mind",
        session_id="session",
        execution_identity=MagicMock(),
        prompt="Consolidate memory",
        thread_history=(),
        user_id=None,
        hook_source=hook_source,
        origin=message_origin(),
    )

    assert (queue is not None) is flushed


@pytest.mark.asyncio
@pytest.mark.parametrize("reply_exists", [False, True])
async def test_a_batch_whose_source_ended_answers_the_rest_only_while_no_reply_exists(
    tmp_path: Path,
    reply_exists: bool,
) -> None:
    """Before a reply exists only the ended source settles and the rest answer apart; once one exists, the batch ends."""
    bot = _bot(tmp_path)
    target = _target(thread_id="$thread", reply_to_event_id="$f")
    envelope = _envelope(target, source_event_id="$f")
    delivery_gateway = MagicMock(spec=DeliveryGateway)
    delivery_gateway.send_text = AsyncMock(return_value="$placeholder")
    # Whether an earlier attempt left a reply whose records now settle the batch.
    delivery_gateway.settle_unclaimed_reply = AsyncMock(return_value=reply_exists)
    request_preparer = MagicMock(spec=ResponsePayloadPreparer)
    request_preparer.prepare = AsyncMock()
    runner = runner_for(
        replace(
            unwrap_extracted_collaborator(bot._response_runner).deps,
            delivery_gateway=delivery_gateway,
            request_preparer=request_preparer,
        ),
    )
    preparation = SourcePreparation(invalid=("$s",), surviving=("$f",))
    request = ResponseRequest(
        sources=ResponseSources(pending_event_ids=("$f", "$s"), logical_source_event_ids=("$s", "$f")),
        thread_history=[],
        prompt="first and second",
        user_id="@user:localhost",
        response_envelope=envelope,
        payload_preparation=_preparation(target, envelope),
        prepare_source_turn=AsyncMock(return_value=preparation),
    )

    fate = await runner.generate_response(request)

    delivery_gateway.send_text.assert_not_awaited()
    request_preparer.prepare.assert_not_awaited()
    # Without a reply, the caller settles only the ended source and the rest answer as their own turn.
    assert fate == Unclaimed(UnclaimedReason.TERMINAL_SOURCES, None if reply_exists else preparation)
