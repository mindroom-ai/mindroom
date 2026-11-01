"""Prepare, generate, and deliver agent and team turns under their conversation's lock."""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from functools import partial
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from mindroom import reply_lifecycle as rl
from mindroom.agent_modes import resolve_agent_mode
from mindroom.agent_run_context import append_knowledge_availability_enrichment
from mindroom.ai import stream_agent_response
from mindroom.ai_run_metadata import ai_run_extra_content_from_metadata
from mindroom.approval.response import continuation_response_kind, continuation_target
from mindroom.authorization import ReplyMembershipPendingError
from mindroom.background_tasks import run_coroutine_until_complete
from mindroom.config.resolution import cheap_model as agent_cheap_model
from mindroom.config.resolution import entity_timezone, show_reasoning, show_tool_calls
from mindroom.constants import (
    HOOK_SOURCE_RUN_METADATA_KEY,
    STREAM_STATUS_KEY,
    STREAM_STATUS_PENDING,
    TURN_INTENT_RUN_METADATA_KEY,
)
from mindroom.contacts.turns import contact_turn_instructions, contact_turn_tools
from mindroom.conversation_state_writer import ReplySession
from mindroom.dispatch_source import is_automation_source_kind
from mindroom.error_handling import get_user_friendly_error_message
from mindroom.final_delivery import (
    Cancelled,
    FinalDeliveryOutcome,
    Suppressed,
    Suppression,
    TurnProgress,
)
from mindroom.history.storage import has_queued_force_compaction
from mindroom.history.turn_recorder import TurnRecorder
from mindroom.judgment.cold_cache import idle_seconds_before_turn, select_cold_cache_model
from mindroom.matrix.presence import should_use_streaming
from mindroom.mid_turn_judgment import create_mid_turn_gate
from mindroom.orchestration.runtime import (
    classify_cancel_source,
    current_task_is_process_shutdown,
    log_cancelled_response,
)
from mindroom.participation import ParticipationGate
from mindroom.participation_judgment import create_participation_decider
from mindroom.post_response_effects import ResponseOutcome
from mindroom.reply_presentation import (
    AGENT_PLACEHOLDER,
    TEAM_PLACEHOLDER,
    Presentation,
)
from mindroom.reply_scope import (
    ClaimRefused,
    SpanHandle,
    current_slot,
    current_span,
    initial_write,
    release_decision,
    suppress_decision,
    terminal_source_decision,
)
from mindroom.reply_turn.enrichment import (
    interrupted_attempt_model_prompt,
    matrix_message_target_item,
    prepare_memory_and_model_context,
    with_matrix_message_target,
    with_silent_schedule_delivery,
)
from mindroom.reply_turn.locked_turn import (
    DECLINED,
    LockedTurns,
    ReplyTurnDeps,
    existing_event,
    existing_event_id,
)
from mindroom.reply_turn.request import (
    PreparedResponseRuntime,
    ResponseRequest,
    correlation_id_for_request,
    merge_response_extra_content,
    response_typing_indicator,
)
from mindroom.response_sources import Owned, ResponseSources, SourceFate, Unclaimed, UnclaimedReason
from mindroom.response_turn import (
    PausedAttempt,
    ResponsePausedForApproval,
    ResponseTurnContext,
    TurnSinks,
    collect_streamed_response_content,
)
from mindroom.runtime_resolution import resolve_agent_storage
from mindroom.scheduling.scheduled_run_records import record_silent_schedule_started_if_needed
from mindroom.skill_learning.capture import SkillReviewCapture
from mindroom.streaming import (
    ReplacementStreamingResponse,
    StreamingResponse,
    UnfinishedStreamedReply,
)
from mindroom.teams.envelope import TeamTurnSpec, team_response_stream
from mindroom.teams.members import allow_direct_private_team_agents, requested_team_agent_names
from mindroom.teams.spec import TeamMode
from mindroom.thread_models import set_thread_model_override
from mindroom.timing import timed
from mindroom.tool_jobs.follow_ups import follow_up_envelope, follow_up_jobs, follow_up_key
from mindroom.tool_jobs.runtime import get_background_runtime
from mindroom.tool_system.agent_tools import room_turn_offers
from mindroom.tool_system.context_bound_streams import closing_async_stream
from mindroom.tool_system.events import CollectedStreamPresentation
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.turn_model import (
    ModelScope,
    TeamTurnModelSelection,
    TurnBudget,
    TurnModel,
    resolve_team_turn_models,
    resolve_turn_model,
)
from mindroom.turn_origin import SenderKind, TurnIntent
from mindroom.turn_record import RevisionSnapshotChangedError

from .delivery_gateway import (
    FinalDeliveryRequest,
    FinalizeStreamedResponseRequest,
    MatrixCompactionLifecycle,
    ResponseIdentity,
    SendTextRequest,
    StreamingDeliveryRequest,
)
from .media_inputs import MediaInputs
from .response_activity import ActiveResponseIdentity
from .response_admission import ResponseAdmissionRefusedError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Sequence

    from mindroom.agent_cli.cli_approval_waits import CliWaiters
    from mindroom.approval.continuation_runner import ApprovalContinuationRunner
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.event_journal import JournalEvent
    from mindroom.event_journal.approval_signals import ApprovalSignals
    from mindroom.history.types import HistoryScope
    from mindroom.hooks import EnrichmentItem, MessageEnvelope
    from mindroom.knowledge.search import AgentKnowledge
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.message_target import MessageTarget
    from mindroom.mid_turn import MidTurnGate
    from mindroom.post_response_effects import PostResponseEffectsDeps
    from mindroom.reply_turn.inbox import InboxResponses
    from mindroom.streaming import StreamInputChunk
    from mindroom.tool_jobs.conversation import ConversationKey
    from mindroom.tool_system.events import ToolTraceEntry
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity
    from mindroom.turn_origin import TurnOrigin

    from .response_lifecycle import (
        QueuedHumanNoticeReservation,
        ResponseLifecycleCoordinator,
        ResponseLifecycleReservation,
    )


def _paused_with_committed_presentation(
    error: ResponsePausedForApproval,
    *,
    show_tool_calls: bool,
) -> PausedAttempt:
    """Attach only the response state acknowledged before the stream suspended."""
    if error.presentation is None:
        return error.paused
    if show_tool_calls:
        tool_trace = error.presentation.tool_trace
        presentation_state = error.presentation.state or {}
    else:
        tool_trace = error.paused.tool_trace
        presentation_state = error.paused.response_presentation_state
    return replace(
        error.paused,
        response_text=error.presentation.response_text,
        acknowledged_response_text=error.presentation.rendered_response_text,
        tool_trace=tool_trace,
        response_presentation_state=presentation_state,
    )


def _resumed_reply() -> UnfinishedStreamedReply | None:
    """Return what the stopped attempt at the current span's reply showed, which this attempt streams below."""
    handle = current_span()
    return None if handle is None else handle.resumed


def _reply_event_id() -> str | None:
    """Return the event the current span's reply shows, if any."""
    handle = current_span()
    return None if handle is None else handle.reply.event_id


def _shows_placeholder(request: ResponseRequest) -> bool:
    """Return whether the event this response writes over is a placeholder rather than an answer."""
    return existing_event(request)[1]


def _replaceable_placeholder(request: ResponseRequest) -> bool:
    """Return whether the adopted event holds only a placeholder that terminal handling may replace or redact.

    A resumed reply shows its stopped attempt's work, which no failure may remove.
    """
    return _shows_placeholder(request) and _resumed_reply() is None


def _turn_run_metadata(request: ResponseRequest) -> dict[str, Any]:
    """Record what started this turn, so stored runs show which automation asked or that MindRoom wrote the prompt.

    The turn recorder gets it too, so a turn stopped before its run starts keeps it.
    """
    envelope = request.response_envelope
    recorded = {**(request.matrix_run_metadata or {}), TURN_INTENT_RUN_METADATA_KEY: envelope.origin.intent.value}
    if envelope.hook_source is not None:
        recorded[HOOK_SOURCE_RUN_METADATA_KEY] = envelope.hook_source
    return recorded


def _request_has_media(request: ResponseRequest) -> bool:
    """Count deferred attachment registration as media too."""
    preparation = request.payload_preparation
    return bool(
        request.attachment_ids
        or (request.media is not None and request.media.has_any())
        or (
            preparation is not None
            and (
                preparation.payload_inputs.media_events
                or preparation.payload_inputs.message_attachment_ids
                or preparation.payload_inputs.trusted_attachment_ids
                or preparation.payload_inputs.raw_audio_fallback
            )
        ),
    )


def _mid_turn_for_request(
    request: ResponseRequest,
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    on_defer: Callable[[str, str], Awaitable[None]] | None,
) -> MidTurnGate | None:
    """Treat deferred attachment registration as incomplete judgment context too."""
    has_media = _request_has_media(request)
    gate = create_mid_turn_gate(
        config,
        runtime_paths,
        request.response_envelope,
        prompt=request.prompt,
        has_media=has_media,
        on_defer=on_defer,
    )
    if gate is None:
        return None
    # The ingress history may be stale until this response acquires the lock.
    gate.bind_conversation_context(None)
    # A regeneration replaces an answer that may show, even one whose event Matrix has not acknowledged yet.
    if (request.existing_event_id or request.edit_regeneration) and not _shows_placeholder(request):
        gate.visible_response_text = None
    return gate


def _participation_for_request(
    request: ResponseRequest,
    config: Config,
    runtime_paths: RuntimePaths,
) -> ParticipationGate | None:
    """Own one participation decision from locked preparation through delivery."""
    if request.participation is None:
        return None
    gate = ParticipationGate(instructions=request.participation.instructions)
    if request.existing_event_id is not None:
        gate.approve_existing_response()
    else:
        gate.decider = create_participation_decider(
            request.participation,
            config,
            runtime_paths,
            agent_name=request.response_envelope.agent_name,
        )
    return gate


def _approve_continued_reply(participation: ParticipationGate | None, request: ResponseRequest) -> None:
    """A claim that continued a reply already showing this turn's answer needs no new participation decision."""
    if participation is not None and existing_event_id(request) is not None:
        participation.approve_existing_response()


class _PostLockRequestPreparationError(RuntimeError):
    """Raised when post-lock request preparation fails before generation starts.

    It only escapes a claimed reply, whose records then own the failure notice.
    """

    def __init__(self, message: str = "Post-lock request preparation failed") -> None:
        super().__init__(message)


def _runs_nothing(request: ResponseRequest) -> Suppression | None:
    """Return why a turn ends unanswered without running: an empty prompt, or a room whose contact was removed.

    A removed contact's room takes no more turns, even one admitted before the removal.
    """
    if not request.prompt.strip():
        return Suppression.EMPTY_PROMPT
    if request.contact is not None and request.contact[0].status != "active":
        return Suppression.CONTACT_TURN
    return None


def _raise_if_process_shutdown() -> None:
    """Restore process cancellation when generation code consumed it."""
    if current_task_is_process_shutdown():
        raise asyncio.CancelledError


@dataclass(frozen=True)
class _TeamResponseRequest:
    """The team-specific inputs of one team response request."""

    member_names: tuple[str, ...]
    team_mode: str
    # Why a configured team cannot answer: the turn delivers it as its final instead of running the team.
    rejection_message: str | None = None


@dataclass(frozen=True)
class _LockedTurn:
    """One admitted agent or team turn, prepared: everything its generation and settlement read.

    ``request`` is the turn's request as admitted, locked to the target its reply answers in; the stream reads the
    model-facing history and prompt from its own turn context.
    """

    request: ResponseRequest
    response_kind: Literal["ai", "team"]
    history_scope: HistoryScope
    execution_identity: ToolExecutionIdentity
    runtime: PreparedResponseRuntime
    run_id: str
    streaming_cls: type[StreamingResponse]
    # The entity's response stream for one generation, given the runtime the turn delivers with, the room's live
    # replies, its compaction notices, and what the turn writes back.
    stream: Callable[
        [PreparedResponseRuntime, set[str], MatrixCompactionLifecycle | None, TurnSinks],
        AsyncIterator[StreamInputChunk],
    ]
    post_response_deps: Callable[[], PostResponseEffectsDeps]
    team_member_names: tuple[str, ...] = ()
    team_mode: str | None = None


def _requested_by_a_person(origin: TurnOrigin) -> bool:
    """Whether a turn counts toward skill learning.

    Like Hermes skipping cron reviews, automated runs, job follow-ups, and replies to other agents never count toward a
    review; they have no human to learn from.
    """
    return (
        origin.requester_kind == SenderKind.USER
        and origin.intent is not TurnIntent.JOB_FOLLOW_UP
        and not is_automation_source_kind(origin.source_kind)
    )


@dataclass
class ResponseRunner:
    """Prepare, generate, and deliver an agent or team turn under its conversation's lock.

    Its collaborators are built beside it: the locked turns it settles through, the approval continuation runner a
    paused turn hands over to, the inbox that owns detached responses, and the conversation locks they all share.
    """

    locked: LockedTurns = field(repr=False)
    approvals: ApprovalContinuationRunner = field(repr=False)
    inbox: InboxResponses = field(repr=False)
    lifecycle_coordinator: ResponseLifecycleCoordinator = field(repr=False)
    # Which sources a response waits on in place for a minimal-mode approval.
    cli_waiters: CliWaiters = field(repr=False)
    # Own count, distinct from the shared gate's process-wide total: callers like
    # the todo-poke idle check ask whether *this* entity is busy.
    _in_flight_response_count: int = field(default=0, init=False)

    @property
    def deps(self) -> ReplyTurnDeps:
        """Return the collaborators every owner of a reply turn shares, which its locked turns hold."""
        return self.locked.deps

    def _turn_budget(self, request: ResponseRequest) -> TurnBudget:
        """Return the budget this request's requester spends."""
        return TurnBudget(
            monitor=self.deps.runtime.orchestrator.budgets,
            requester_id=request.response_envelope.requester_id,
        )

    @property
    def in_flight_response_count(self) -> int:
        """Return the number of active response lifecycles for this entity."""
        return self._in_flight_response_count

    def _show_tool_calls(self) -> bool:
        """Return tool-call visibility for this agent."""
        return show_tool_calls(self.deps.runtime.config, self.deps.agent_name)

    def _turn_show_tool_calls(self) -> bool:
        """Return the tool-call visibility of the current turn: what its reply was claimed with, read once per turn."""
        handle = current_span()
        assert handle is not None, "a turn reads its tool-call visibility once its reply is claimed"
        return handle.base.show_tool_calls

    def _response_skill_review(
        self,
        request: ResponseRequest,
        runtime: PreparedResponseRuntime,
        *,
        session_id: str,
        execution_identity: ToolExecutionIdentity | None,
    ) -> tuple[Callable[[str], Coroutine[Any, Any, None]] | None, PreparedResponseRuntime]:
        """Stop the conversation's running review, and return a person's response's skill-review handoff.

        Like Hermes, a response starting in a conversation stops its running review. The returned runtime records the
        response's final request for the review to fork.
        """
        config = self.deps.runtime.config
        agent = config.agents.get(self.deps.agent_name)
        if agent is None or not agent.skill_learning.enabled:
            return None, runtime
        reviews = self.deps.runtime.orchestrator.skill_reviews
        agent_name = self.deps.agent_name
        reviews.cancel(config, agent_name=agent_name, session_id=session_id, identity=execution_identity)
        if not _requested_by_a_person(request.response_envelope.origin):
            return None, runtime
        capture = SkillReviewCapture()

        async def count(run_id: str) -> None:
            await reviews.count(
                config,
                agent_name=agent_name,
                session_id=session_id,
                identity=execution_identity,
                run_id=run_id,
                captured=capture.latest,
                correlation_id=correlation_id_for_request(request),
            )

        return count, replace(runtime, skill_review_capture=capture)

    def _note_final_delivery_timing(self, request: ResponseRequest, delivery: FinalDeliveryOutcome) -> None:
        """Mark the terminal visible reply and response completion for one delivery."""
        if request.pipeline_timing is not None:
            request.pipeline_timing.mark_first_visible_reply(
                "final",
                substantive=delivery.delivered_substantive_content,
            )
            request.pipeline_timing.mark("response_complete")

    def has_active_response_for_target(self, target: MessageTarget) -> bool:
        """Return whether one canonical conversation target already has an active turn."""
        return self.lifecycle_coordinator.has_active_response_for_target(target)

    def active_thread_ids_for_room(self, room_id: str) -> frozenset[str | None]:
        """Return canonical thread IDs with active response lifecycles in one room."""
        return self.lifecycle_coordinator.active_thread_ids_for_room(room_id)

    def follow_approval_holds(self, signals: ApprovalSignals, principal_id: str) -> None:
        """Hold the conversation of each pending approval of this principal, from the ones pending now on."""
        self.lifecycle_coordinator.follow_approval_holds(signals, principal_id)

    def stop_following_approval_holds(self) -> None:
        """Stop holding conversations for approvals that pause or end from now on."""
        self.lifecycle_coordinator.stop_following_approval_holds()

    def is_held_for_approval(self, target: MessageTarget) -> bool:
        """Return whether a pending approval keeps this conversation busy."""
        return bool(self.lifecycle_coordinator.approval_holds(target.room_id, target.resolved_thread_id))

    async def wait_for_thread_response_idle(self, room_id: str, thread_id: str | None) -> None:
        """Wait until one canonical room/thread has no active response turn."""
        await self.lifecycle_coordinator.wait_for_thread_idle(room_id, thread_id)

    def reserve_waiting_human_message(
        self,
        *,
        target: MessageTarget,
        response_envelope: MessageEnvelope,
    ) -> QueuedHumanNoticeReservation | None:
        """Reserve a queued-human notice for an active response before dispatch owns ingress."""
        return self.lifecycle_coordinator.reserve_waiting_human_message(
            target=target,
            response_envelope=response_envelope,
        )

    async def reserve_response_lifecycle(
        self,
        response_envelope: MessageEnvelope,
    ) -> ResponseLifecycleReservation:
        """Reserve one canonical response lifecycle before detached preparation starts."""
        return await self.lifecycle_coordinator.reserve_response_lifecycle(response_envelope)

    async def _active_response_event_ids(self, room_id: str) -> set[str]:
        """Return the events of one room's replies whose span runs in this bot instance."""
        return set(await self.deps.replies.live_event_ids(room_id))

    async def _run_locked_response_lifecycle(
        self,
        request: ResponseRequest,
        *,
        response_kind: str,
        locked_operation: Callable[[MessageTarget], Awaitable[SourceFate]],
        signal_queued_message: bool = True,
    ) -> SourceFate:
        """Admit one response, then run it under its conversation's lock; return what it did with its sources."""
        gate = self.deps.runtime.response_admission_gate
        admission_deferred = False
        while not gate.admit():
            if not admission_deferred:
                admission_deferred = True
                self.deps.logger.info(
                    "response_deferred_during_replacement",
                    response_kind=response_kind,
                    **request.response_envelope.target.log_context,
                )
            if not await self.deps.runtime.wait_for_admission_or_shutdown():
                self.deps.logger.warning(
                    "response_refused_after_runtime_replacement",
                    response_kind=response_kind,
                    **request.response_envelope.target.log_context,
                )
                raise ResponseAdmissionRefusedError
        identity = ActiveResponseIdentity(
            responder=(
                f"team/{self.deps.agent_name}"
                if self.deps.agent_name in self.deps.runtime.config.teams
                else self.deps.agent_name
            ),
            requester_id=request.response_envelope.requester_id,
        )
        gate.response_identities.add(identity)
        self._in_flight_response_count += 1

        async def acknowledge_deferred(key: str, event_id: str) -> None:
            await self.deps.delivery_gateway.send_judgment_reaction(
                turn_id=request.response_envelope.source_event_id,
                room_id=request.room_id,
                event_id=event_id,
                key=key,
                kind="mid_turn_defer",
            )

        try:
            async with self.deps.replies.span_scope():
                fate = await self.lifecycle_coordinator.run_locked_response(
                    target=request.response_envelope.target,
                    response_envelope=request.response_envelope,
                    pipeline_timing=request.pipeline_timing,
                    mid_turn_gate=_mid_turn_for_request(
                        request,
                        self.deps.runtime.config,
                        self.deps.runtime_paths,
                        # A contact turn shows nothing, not even a reaction on the other side's message.
                        on_defer=None if request.contact is not None else acknowledge_deferred,
                    ),
                    locked_operation=lambda target: self._run_locked_ending_span(
                        request,
                        target=target,
                        locked_operation=locked_operation,
                    ),
                    signal_queued_message=signal_queued_message and not self.locked.is_quiet(request),
                )
                handle = current_span()
                if handle is not None and not handle.exited:
                    # Every exit path is meant to end its span; one that did not is a bug,
                    # and releasing keeps the reply's sources for a retry.
                    self.deps.logger.error("reply_span_left_unended", span_id=handle.span_id, reply_id=handle.reply_id)
                    await self.deps.delivery_gateway.end_reply_span(handle, release_decision(handle))
                return fate
        finally:
            gate.response_identities.remove(identity)
            self._in_flight_response_count -= 1
            gate.release()

    async def _run_locked_ending_span(
        self,
        request: ResponseRequest,
        *,
        target: MessageTarget,
        locked_operation: Callable[[MessageTarget], Awaitable[SourceFate]],
    ) -> SourceFate:
        """Run the locked response, ending a span it left running before the conversation lock is released.

        A claim waiting for the lock must not find that span still current. A preparation failure the span's
        records now show answers the turn; any other error leaves the span released for a retry and propagates.
        """
        try:
            return await self._run_owned_or_locked_response(request, target=target, locked_operation=locked_operation)
        except BaseException as error:
            handle = current_span()
            await self._exit_unended_span(handle, error, target=target, contact_turn=request.contact is not None)
            if isinstance(error, _PostLockRequestPreparationError) and handle is not None and handle.exited:
                return Owned()
            raise

    async def _run_owned_or_locked_response(
        self,
        request: ResponseRequest,
        *,
        target: MessageTarget,
        locked_operation: Callable[[MessageTarget], Awaitable[SourceFate]],
    ) -> SourceFate:
        """Keep executable approval generations inside their response's lifecycle."""
        owned = await self.deps.approval_store.approval_continuation_for_source(
            request.response_envelope.source_event_id,
        )
        if owned is None:
            fate = await locked_operation(target)
            owned = await self.deps.approval_store.approval_continuation_for_source(
                request.response_envelope.source_event_id,
            )
            if owned is None or owned.state != "ready":
                return fate
        await self.approvals.run_owned_approvals(owned, request=request, target=target)
        return Owned()

    async def _exit_unended_span(
        self,
        handle: SpanHandle | None,
        error: BaseException,
        *,
        target: MessageTarget,
        contact_turn: bool,
    ) -> None:
        """End a claimed span the error left running.

        A process stop leaves the span exactly as a crash would; the next bot
        instance ends it as lost and replays its sources.
        """
        if handle is not None and not handle.exited and not current_task_is_process_shutdown():
            await run_coroutine_until_complete(
                self._exit_span_on_error(handle, error, target=target, contact_turn=contact_turn),
            )

    async def _exit_span_on_error(
        self,
        handle: SpanHandle,
        error: BaseException,
        *,
        target: MessageTarget,
        contact_turn: bool,
    ) -> None:
        """End a span whose locked response raised before ending it; a contact turn's failure stays unshown."""
        gateway = self.deps.delivery_gateway
        if isinstance(error, asyncio.CancelledError):
            await gateway.end_interrupted_span(
                handle,
                target,
                ending=Cancelled(classify_cancel_source(error)),
                delivery_started=False,
                contact_turn=contact_turn,
            )
            return
        if isinstance(error, (ReplyMembershipPendingError, RevisionSnapshotChangedError)):
            await gateway.end_reply_span(handle, release_decision(handle, outcome=rl.SpanOutcome.SUPERSEDED))
            return
        if isinstance(error, _PostLockRequestPreparationError):
            cause = error.__cause__ if isinstance(error.__cause__, Exception) else error
            if contact_turn:
                # A contact turn shows nothing in its contact room, not even its failure.
                self.deps.logger.warning("contact_turn_preparation_failed", error=str(cause), **target.log_context)
                await gateway.end_reply_span(handle, suppress_decision(handle))
                return
            confirms = handle.unconfirmed_progress
            now_ns = time.time_ns()
            error_text = get_user_friendly_error_message(
                cause,
                self.deps.agent_name,
                runtime_paths=self.deps.runtime_paths,
            )
            await gateway.end_reply_span(
                handle,
                lambda reply, span: rl.dispatch_failed(
                    rl.confirm_progress(reply, confirms),
                    span,
                    error_text=error_text,
                    now_ns=now_ns,
                ),
            )
            return
        await gateway.end_reply_span(handle, release_decision(handle))

    async def _claim_reply_span(
        self,
        request: ResponseRequest,
        *,
        history_scope: HistoryScope,
    ) -> ResponseRequest | SourceFate:
        """Claim the reply this request answers, after the first source gate passed.

        A fate means no span opened: the claim was deferred until what blocks it resolves, or another instance took
        the replies over (both keep the sources for their retry), or nothing runs for them, as when the reply already
        answered the edit, a Stop ended it, or a follow-up has nothing left to report.
        """
        slot = current_slot()
        assert slot is not None, "a reply is claimed inside its span scope"
        if request.follow_up_key is not None and not await self._follow_up_due(request):
            return Unclaimed(UnclaimedReason.NOT_DUE)
        handle = await self.deps.replies.claim(
            delivery_id=request.response_envelope.source_event_id,
            sources=request.sources,
            room_id=request.room_id,
            thread_id=request.thread_id,
            placeholder=TEAM_PLACEHOLDER if history_scope.kind == "team" else AGENT_PLACEHOLDER,
            show_tool_calls=self._show_tool_calls(),
            driving_edit_id=request.response_envelope.source_event_id if request.edit_regeneration else None,
            existing_event_id=request.existing_event_id,
            interactive_span_id=request.interactive_span_id,
        )
        if handle is ClaimRefused.NOTHING_TO_RUN:
            self.deps.logger.info(
                "reply_claim_nothing_to_run",
                source_event_id=request.response_envelope.source_event_id,
            )
            return Unclaimed(UnclaimedReason.NOTHING_TO_RUN)
        if isinstance(handle, ClaimRefused):
            # The sources stay pending: the instance that took the replies over
            # replays them, or the resolution of the reply's earlier writes retries them.
            return Owned()
        slot.handle = handle
        return request

    async def handoff_approval_source(self, source_event_id: str) -> bool | None:
        """Take a source an approval owns out of the journal lane and resume it through this runner's lifecycle.

        ``None`` means no approval owns the source; ``False`` means a response waiting in place for its decision, or the
        resume task tracked here, owns it now.
        """
        continuation = await self.deps.approval_store.approval_continuation_for_source(source_event_id)
        if continuation is None:
            return None
        if not self.cli_waiters.wake((source_event_id,)):
            return False
        resume = self._resume_approval_source(source_event_id)
        try:
            self.inbox.track_inbox_response(
                resume,
                name=f"approval_resume:{continuation.approval_id}:{continuation.generation}",
                recovery_proof_ready=lambda: True,
                room_id=continuation.room_id,
                source_event_ids=continuation.source_event_ids,
            )
        except BaseException:
            resume.close()
            raise
        return False

    async def _resume_approval_source(self, source_event_id: str) -> None:
        """Run the approval that owns a source inside the response lifecycle, before ingress can reinterpret it."""
        continuation = await self.deps.approval_store.approval_continuation_for_source(source_event_id)
        if continuation is None:
            return
        request = self.approvals.approval_response_request(
            continuation,
            target=continuation_target(continuation, reply_to_event_id=source_event_id),
        )

        async def ownership_disappeared(_target: MessageTarget) -> SourceFate:
            # Reached only when no approval owns the source any more: whoever ended it settled its sources.
            return Owned()

        await self._run_locked_response_lifecycle(
            request,
            response_kind=continuation_response_kind(continuation),
            locked_operation=ownership_disappeared,
            signal_queued_message=False,
        )

    async def handoff_job_follow_up(self, event: JournalEvent) -> None:
        """Hand a follow-up to a detached response owner; its reply, or that owner, settles it.

        The dispatcher hands over only a follow-up no live owner holds.
        """
        self.inbox.track_inbox_response(
            self._run_job_follow_up(event),
            name=f"job_follow_up:{event.event_id}",
            room_id=event.room_id,
            source_event_ids=(event.event_id,),
            recovery_proof_ready=lambda: True,
        )

    async def _run_job_follow_up(self, event: JournalEvent) -> None:
        """Run the turn a follow-up asks for, then settle a follow-up no reply took."""
        fate = await self._generate_job_follow_up(
            self._job_follow_up_request(follow_up_key(event, self.deps.agent_name), event.event_id),
        )
        if isinstance(fate, Unclaimed):
            await self.deps.settle_ignored_sources((event.event_id,))

    async def _follow_up_due(self, request: ResponseRequest) -> bool:
        """Decide under the conversation lock whether a follow-up still has jobs to report.

        A reply may have collected them, and the reply to a message of their requester that already waits here reports
        them in its start note, so the follow-up gives way to that message.
        """
        key = request.follow_up_key
        assert key is not None
        waiting = self.lifecycle_coordinator.has_waiting_messages_from(
            request.response_envelope.target,
            key.requester_id,
        )
        runtime = get_background_runtime(self.deps.runtime_paths)
        if not waiting and runtime is not None and follow_up_jobs(await runtime.ended_jobs(key)):
            return True
        # A follow-up whose own reply a restart cut short answers again, though that reply collected the jobs.
        reply = await self.deps.replies.store.replies.for_sources((request.response_envelope.source_event_id,))
        return reply is not None and not reply.terminal

    def _job_follow_up_request(self, key: ConversationKey, follow_up_id: str) -> ResponseRequest:
        """Build the turn that reports a conversation's ended jobs in a new reply; its start note lists the jobs."""
        prompt = self.deps.runtime.config.get_prompt("BACKGROUND_JOBS_FOLLOW_UP_PROMPT")
        envelope = follow_up_envelope(key, follow_up_id=follow_up_id, sender_id=self.deps.matrix_full_id, prompt=prompt)
        return ResponseRequest(
            thread_history=(),
            prompt=envelope.body,
            response_envelope=envelope,
            sources=ResponseSources((follow_up_id,), (follow_up_id,)),
            follow_up_key=key,
            user_id=key.requester_id,
        )

    async def _generate_job_follow_up(self, request: ResponseRequest) -> SourceFate:
        """Answer as this agent, or as this team, which owns the jobs its members started."""
        config = self.deps.runtime.config
        team = config.teams.get(self.deps.agent_name)
        if team is None:
            return await self.generate_response(request)
        return await self.generate_team_response_helper(request, member_names=team.agents, team_mode=team.mode)

    def _build_compaction_lifecycle(
        self,
        *,
        target: MessageTarget,
        request: ResponseRequest,
    ) -> MatrixCompactionLifecycle | None:
        """Build the ordered foreground compaction notice adapter for one response."""
        if self.locked.is_quiet(request) or request.participation is not None:
            return None
        event_id, shows_placeholder = existing_event(request)
        reply_to_event_id = event_id if shows_placeholder else request.reply_to_event_id
        return MatrixCompactionLifecycle(
            delivery_gateway=self.deps.delivery_gateway,
            target=target,
            reply_to_event_id=reply_to_event_id,
        )

    async def _has_queued_forced_compaction(
        self,
        *,
        session_id: str,
        scope: HistoryScope,
        execution_identity: ToolExecutionIdentity | None,
    ) -> bool:
        """Return whether this scope should compact before creating a reply placeholder.

        Every turn pays for this before its placeholder is sent, so it reads
        one session row, without its runs, on a thread.
        """
        return await asyncio.to_thread(
            self._read_queued_forced_compaction,
            session_id=session_id,
            scope=scope,
            execution_identity=execution_identity,
        )

    def _read_queued_forced_compaction(
        self,
        *,
        session_id: str,
        scope: HistoryScope,
        execution_identity: ToolExecutionIdentity | None,
    ) -> bool:
        try:
            storage = self.deps.state_writer.create_storage(execution_identity, scope=scope)
            return has_queued_force_compaction(storage, session_id)
        except Exception as error:
            self.deps.logger.warning(
                "forced_compaction_placeholder_check_failed",
                session_id=session_id,
                scope=scope.key,
                exception_type=error.__class__.__name__,
            )
            return False

    async def _prepare_request_after_lock(
        self,
        request: ResponseRequest,
        *,
        exclude_history_event_id: str | None = None,
    ) -> ResponseRequest:
        """Refresh thread history and rebuild any history-derived payload once locked."""
        try:
            if request.pipeline_timing is not None:
                request.pipeline_timing.mark("thread_refresh_start")
            request = await self.locked.refresh_model_history(
                request,
                exclude_event_id=exclude_history_event_id,
            )
            self.locked.bind_mid_turn_context(request)
            if request.pipeline_timing is not None:
                request.pipeline_timing.mark("thread_refresh_ready")
            system_enrichment_items = with_silent_schedule_delivery(
                request.system_enrichment_items,
                request.response_envelope,
            )
            if request.contact is not None:
                instructions = contact_turn_instructions(
                    self.deps.runtime.config,
                    self.deps.runtime_paths,
                    *request.contact,
                )
                # The runtime owns these instructions; a hook's item under the same key never stands in for them.
                system_enrichment_items = (
                    *(item for item in system_enrichment_items if item.key != instructions.key),
                    instructions,
                )
            request = replace(request, system_enrichment_items=system_enrichment_items)
            if request.payload_preparation is None:
                return request
            return await self.deps.request_preparer.prepare(request)
        except (ReplyMembershipPendingError, RevisionSnapshotChangedError):
            raise
        except Exception as exc:
            raise _PostLockRequestPreparationError from exc

    def _turn_context(
        self,
        request: ResponseRequest,
        runtime: PreparedResponseRuntime,
        *,
        run_id: str | None,
        requester_id: str | None,
        prompt: str,
        thread_history: Sequence[ResolvedVisibleMessage],
        media: MediaInputs | None,
        transient_enrichment_items: Sequence[EnrichmentItem],
        matrix_message_available: bool,
    ) -> ResponseTurnContext:
        """Build the inputs an agent turn and a team turn share; each adds its own."""
        target = runtime.tool_context.target
        return ResponseTurnContext(
            entity_label=self.deps.agent_name,
            session_id=runtime.tool_context.target.session_id,
            run_id=run_id,
            correlation_id=correlation_id_for_request(request),
            reply_to_event_id=request.reply_to_event_id,
            room_id=request.room_id,
            thread_id=target.resolved_thread_id,
            requester_id=requester_id,
            matrix_run_metadata=_turn_run_metadata(request),
            current_sender_id=request.response_envelope.origin.speaker_id,
            history_boundary_event_id=request.history_boundary_event_id,
            member_display_names=request.member_display_names,
            transient_enrichment_items=with_matrix_message_target(
                transient_enrichment_items,
                matrix_message_target_item(
                    target,
                    matrix_message_available=matrix_message_available,
                    runtime=self.deps.runtime,
                ),
            ),
            system_enrichment_items=tuple(request.system_enrichment_items),
            # A quiet turn may end without an answer: an empty run is neither retried nor answered with a notice.
            allow_no_report_response=self.locked.is_quiet(request),
            scheduled_history_budget=request.scheduled_history_budget,
            redacted_history_events=partial(self.deps.redacted_history_events, target),
            prompt=prompt,
            current_timestamp_ms=request.current_timestamp_ms,
            current_prompt_is_structured=request.current_prompt_is_structured,
            thread_history=thread_history,
            media=media,
            show_tool_calls=runtime.show_tool_calls,
            show_reasoning=show_reasoning(self.deps.runtime.config, self.deps.agent_name),
            execution_identity=runtime.tool_context.execution_identity,
            pipeline_timing=request.pipeline_timing,
        )

    def _agent_turn_context(
        self,
        request: ResponseRequest,
        *,
        runtime: PreparedResponseRuntime,
        run_id: str | None,
        active_event_ids: set[str],
        knowledge: AgentKnowledge | None,
        transient_enrichment_items: Sequence[EnrichmentItem],
        compaction_lifecycle: MatrixCompactionLifecycle | None,
    ) -> ResponseTurnContext:
        """Build every input of one agent response turn.

        A contact turn offers only its contact's tools, none that would show an approval card to the other side, and
        no interactive questions, since its reply never shows.
        """
        tool_names = None
        if request.contact is not None:
            record, side = request.contact
            tool_names = contact_turn_tools(self.deps.runtime.config, self.deps.agent_name, record.side(side))
        state_root = resolve_agent_storage(
            self.deps.agent_name,
            self.deps.runtime.config,
            self.deps.runtime_paths,
            runtime.tool_context.execution_identity,
        ).state_root
        shared = self._turn_context(
            request,
            runtime,
            run_id=run_id,
            requester_id=request.user_id,
            prompt=request.prompt,
            thread_history=request.thread_history,
            media=runtime.media_inputs,
            transient_enrichment_items=transient_enrichment_items,
            matrix_message_available=room_turn_offers(self.deps.runtime.config, self.deps.agent_name, "matrix_message"),
        )
        return replace(
            shared,
            agent_mode=resolve_agent_mode(
                self.deps.runtime_paths,
                state_root,
                self.deps.agent_name,
                runtime.tool_context.target.session_id,
            ),
            active_model_name=runtime.active_model_name,
            active_event_ids=frozenset(active_event_ids),
            participation=runtime.participation,
            skill_review_capture=runtime.skill_review_capture,
            model_prompt=runtime.model_prompt,
            compaction_lifecycle=compaction_lifecycle,
            refresh_scheduler=self.locked.knowledge_refresh_scheduler(),
            knowledge=knowledge,
            include_interactive_questions=tool_names is None,
            supports_native_tool_approval=tool_names is None,
            tool_names=tool_names,
        )

    async def _admit_locked_turn(
        self,
        request: ResponseRequest,
        *,
        resolved_target: MessageTarget,
        history_scope: HistoryScope,
        reply_entity_names: tuple[str, ...] = (),
    ) -> ResponseRequest | SourceFate:
        """Pass locked authorization and terminal-source gates, then claim the reply, before setup."""
        if not await self.locked.request_remains_authorized(request, reply_entity_names=reply_entity_names):
            return Unclaimed(UnclaimedReason.REVOKED)
        if request.on_lifecycle_lock_acquired is not None:
            request.on_lifecycle_lock_acquired()
        prepared = await self._prepare_locked_source(request, resolved_target=resolved_target)
        if not isinstance(prepared, ResponseRequest):
            return prepared
        claimed = await self._claim_reply_span(prepared, history_scope=history_scope)
        if not isinstance(claimed, ResponseRequest):
            return claimed
        if claimed.on_reply_claimed is not None:
            await claimed.on_reply_claimed()
        await record_silent_schedule_started_if_needed(
            entity_name=self.deps.agent_name,
            agent_names=claimed.participating_agent_names or (self.deps.agent_name,),
            envelope=claimed.response_envelope,
            config=self.deps.runtime.config,
            runtime_paths=self.deps.runtime_paths,
        )
        return claimed

    async def _with_interrupted_attempt(self, request: ResponseRequest) -> ResponseRequest:
        """Tell a replayed turn, or a retried regeneration, what its stopped attempts showed and ran.

        A restart, whether a crash, an orderly shutdown, an entity replacement
        or an approved run cut short, leaves the reply streaming and its sources
        pending, so replay claims that reply. Its records say what the stopped
        attempt may have shown, which stays in the message with the new attempt
        streaming below it, and every tool call it made, shown or not. The same
        account goes into the new attempt's prompt, where later turns keep it.
        """
        handle = current_span()
        if handle is None or handle.span.kind not in {rl.SpanKind.REPLAY, rl.SpanKind.REGENERATION}:
            return request
        recorded = await handle.runtime.interrupted_tool_calls(handle)
        return self._with_recorded_interrupted_attempt(request, handle, recorded)

    def _with_recorded_interrupted_attempt(
        self,
        request: ResponseRequest,
        handle: SpanHandle,
        recorded: tuple[ToolTraceEntry, ...],
    ) -> ResponseRequest:
        """Tell a new attempt what its stopped attempts showed and which tool calls they made, from the reply's records."""
        unfinished = handle.resumed if handle.span.kind is rl.SpanKind.REPLAY else None
        model_prompt = interrupted_attempt_model_prompt(
            request.model_prompt if request.model_prompt is not None else request.prompt,
            span_kind=handle.span.kind,
            unfinished=unfinished,
            recorded=recorded,
        )
        if model_prompt is None:
            return request
        self.deps.logger.info(
            "interrupted_attempt_resumed",
            response_event_id=existing_event_id(request),
            attempt_shown=unfinished is not None,
            recorded_tool_calls=len(recorded),
        )
        return replace(request, model_prompt=model_prompt)

    async def _prepare_locked_source(
        self,
        request: ResponseRequest,
        *,
        resolved_target: MessageTarget,
    ) -> ResponseRequest | SourceFate:
        """Apply the owner gate to this exact request history, before and after refresh.

        A source deleted or answered elsewhere while the request waited ends it.
        A claimed span ends through its records, which settle its sources. Before
        the claim, a reply an earlier attempt left ends through its records too,
        though they do not settle this request's own pending sources (an edit's
        revision), so its caller still settles those. When others survive and no
        reply exists yet, only the terminal ones settle and the rest answer as
        their own turn.
        """
        # The request answers in the target that owns the lock from here on.
        if request.response_envelope.target != resolved_target:
            request = replace(request, response_envelope=replace(request.response_envelope, target=resolved_target))
        preparation = (
            await run_coroutine_until_complete(request.prepare_source_turn(request.thread_history))
            if request.prepare_source_turn is not None
            else None
        )
        if preparation is None or not preparation.invalid:
            return request
        handle = current_span()
        self.deps.logger.info(
            "response_suppressed_for_terminal_source",
            source_event_id=request.response_envelope.source_event_id,
            surviving_source_count=len(preparation.surviving),
        )
        source_deleted = await self._sources_deleted(request, resolved_target)
        if handle is not None and not handle.exited:
            await self.deps.delivery_gateway.end_reply_span(
                handle,
                terminal_source_decision(source_deleted=source_deleted),
            )
            return Owned()
        if await self.deps.delivery_gateway.settle_unclaimed_reply(
            (*request.sources.pending_event_ids, *request.sources.logical_source_event_ids),
            source_deleted=source_deleted,
        ):
            # A reply exists: its sources stay together and the whole request ends.
            return Unclaimed(UnclaimedReason.TERMINAL_SOURCES)
        return Unclaimed(UnclaimedReason.TERMINAL_SOURCES, preparation)

    async def _sources_deleted(self, request: ResponseRequest, target: MessageTarget) -> bool:
        """Return whether deletion made the request's sources terminal: all of them, or the edit driving it.

        A source answering for a relayed message is deleted with that message.
        """
        driving = request.response_envelope.source_event_id
        logical = request.sources.logical_source_event_ids
        deleted = await self.deps.replies.store.deleted_event_ids(target.room_id, (driving, *logical))
        return deleted.issuperset(logical) or (driving not in logical and driving in deleted)

    async def _send_placeholder(
        self,
        request: ResponseRequest,
        *,
        resolved_target: MessageTarget,
        history_scope: HistoryScope,
        execution_identity: ToolExecutionIdentity,
        placeholder_message: str | None,
    ) -> str | None:
        """Send a claimed reply's placeholder as its create, unless it shows something or compacts first; return it."""
        if (
            placeholder_message is None
            or existing_event_id(request) is not None
            or await self._has_queued_forced_compaction(
                session_id=resolved_target.session_id,
                scope=history_scope,
                execution_identity=execution_identity,
            )
        ):
            return None
        handle = current_span()
        assert handle is not None, "a placeholder is its reply's create"
        placeholder_event_id = await self.deps.delivery_gateway.send_text(
            SendTextRequest(
                target=resolved_target,
                response_text=placeholder_message,
                extra_content={STREAM_STATUS_KEY: STREAM_STATUS_PENDING},
                # A streamed answer creates its visible message here and
                # only edits it afterwards, so this send is the one a crash
                # could duplicate into two answers in the room.
                reply_write=initial_write(
                    handle,
                    Presentation(placeholder=placeholder_message, show_tool_calls=handle.base.show_tool_calls),
                    placeholder_only=True,
                ),
            ),
        )
        if placeholder_event_id is not None and request.pipeline_timing is not None:
            request.pipeline_timing.mark("placeholder_sent")
            request.pipeline_timing.mark_first_visible_reply("placeholder")
        return placeholder_event_id

    async def _prepare_claimed_request(
        self,
        request: ResponseRequest,
        *,
        resolved_target: MessageTarget,
        placeholder_event_id: str | None,
    ) -> ResponseRequest | SourceFate:
        """Refresh a claimed turn's history and payload, gate its sources again, and tell it what an earlier attempt did."""
        request = await self._prepare_request_after_lock(request, exclude_history_event_id=placeholder_event_id)
        prepared = await self._prepare_locked_source(request, resolved_target=resolved_target)
        if not isinstance(prepared, ResponseRequest):
            return prepared
        return await self._with_interrupted_attempt(prepared)

    async def _begin_locked_turn(
        self,
        request: ResponseRequest,
        *,
        resolved_target: MessageTarget,
        history_scope: HistoryScope,
        reply_entity_names: tuple[str, ...] = (),
    ) -> ResponseRequest | SourceFate:
        """Admit and prepare one turn that shows no placeholder, after its lifecycle lock is acquired."""
        admitted = await self._admit_locked_turn(
            request,
            resolved_target=resolved_target,
            history_scope=history_scope,
            reply_entity_names=reply_entity_names,
        )
        if not isinstance(admitted, ResponseRequest):
            return admitted
        return await self._prepare_claimed_request(admitted, resolved_target=resolved_target, placeholder_event_id=None)

    async def _finalize_empty_prompt_locked(
        self,
        request: ResponseRequest,
        *,
        resolved_target: MessageTarget,
        response_kind: str,
        history_scope: HistoryScope | None = None,
        reply_entity_names: tuple[str, ...] = (),
        suppression: Suppression = Suppression.EMPTY_PROMPT,
    ) -> SourceFate:
        """Finalize a turn that runs nothing through the canonical response lifecycle: its claimed reply shows nothing.

        That is an empty prompt, or a turn in a removed contact's room.
        """
        prepared = await self._begin_locked_turn(
            request,
            resolved_target=resolved_target,
            history_scope=history_scope or self.deps.state_writer.history_scope(),
            reply_entity_names=reply_entity_names,
        )
        if not isinstance(prepared, ResponseRequest):
            return prepared
        await self._end_unanswered(
            prepared,
            FinalDeliveryOutcome(ending=Suppressed(suppression)),
            response_kind=response_kind,
            post_response_outcome=ResponseOutcome(),
        )
        return Owned()

    async def _end_unanswered(
        self,
        request: ResponseRequest,
        outcome: FinalDeliveryOutcome,
        *,
        response_kind: str,
        post_response_outcome: ResponseOutcome,
    ) -> bool:
        """End a locked turn that answers nothing; return whether it had claimed a reply.

        A claimed reply ends showing nothing, its placeholder gone as a suppressed answer's is.
        """
        handle = current_span()
        if handle is None or handle.exited:
            claimed = False
        else:
            claimed = True
            await self.deps.delivery_gateway.end_reply_span(handle, suppress_decision(handle))
        lifecycle = self.locked.build_lifecycle(request, response_kind=response_kind)
        await lifecycle.finalize(
            outcome,
            build_post_response_outcome=lambda _final_outcome: post_response_outcome,
            post_response_deps=lambda: self.locked.post_response_deps(request),
        )
        return claimed

    async def generate_team_response_helper(
        self,
        request: ResponseRequest,
        *,
        member_names: Sequence[str],
        team_mode: str,
    ) -> SourceFate:
        """Generate a team response of ``member_names`` with lifecycle locking and queued-message state."""
        return await self._run_team_turn(
            request,
            _TeamResponseRequest(member_names=tuple(member_names), team_mode=team_mode),
        )

    async def deliver_team_rejection(
        self,
        request: ResponseRequest,
        *,
        member_names: Sequence[str],
        team_mode: str,
        rejection_message: str,
    ) -> SourceFate:
        """Deliver why a configured team cannot answer as the final of the reply this request claims.

        A regeneration's request names its existing reply, so the rejection
        edits that reply in place and its records settle the turn.
        """
        return await self._run_team_turn(
            request,
            _TeamResponseRequest(
                member_names=tuple(member_names),
                team_mode=team_mode,
                rejection_message=rejection_message,
            ),
        )

    async def _run_team_turn(self, request: ResponseRequest, team_request: _TeamResponseRequest) -> SourceFate:
        return await self._run_locked_response_lifecycle(
            request,
            response_kind="team",
            locked_operation=lambda resolved_target: self._run_locked_turn(
                request,
                team=team_request,
                resolved_target=resolved_target,
                participation=None,
            ),
        )

    async def generate_response_for_empty_prompt(
        self,
        request: ResponseRequest,
        *,
        response_kind: str,
    ) -> SourceFate:
        """Finalize an empty prompt through the locked lifecycle before setup side effects."""
        request = self.locked.with_contact(request)
        return await self._run_locked_response_lifecycle(
            request,
            response_kind=response_kind,
            locked_operation=lambda resolved_target: self._finalize_empty_prompt_locked(
                request,
                resolved_target=resolved_target,
                response_kind=response_kind,
            ),
        )

    async def _run_locked_turn(
        self,
        request: ResponseRequest,
        *,
        team: _TeamResponseRequest | None,
        resolved_target: MessageTarget,
        participation: ParticipationGate | None,
    ) -> SourceFate:
        """Admit, prepare, generate, and settle one agent or team turn while the conversation's lock is held.

        ``team`` names the members a team turn runs; an agent turn has none, and only it may wait on a participation
        decision. A preparation failure before that decision declines the turn quietly. What differs between the two
        kinds is decided in ``_prepare_agent_turn`` and ``_prepare_team_turn``.
        """
        try:
            prepared = await self._prepare_locked_turn(
                request,
                team=team,
                resolved_target=resolved_target,
                participation=participation,
            )
        except (ReplyMembershipPendingError, RevisionSnapshotChangedError, ResponseAdmissionRefusedError):
            raise
        except Exception as error:
            if participation is None or participation.decision is not None:
                raise
            participation.decline("preparation_failed")
            return await self._decline_unprepared_turn(request, error)
        if not isinstance(prepared, _LockedTurn):
            return prepared
        await self._generate_and_settle(prepared)
        return Owned()

    async def _prepare_locked_turn(
        self,
        request: ResponseRequest,
        *,
        team: _TeamResponseRequest | None,
        resolved_target: MessageTarget,
        participation: ParticipationGate | None,
    ) -> _LockedTurn | SourceFate:
        """Admit and prepare one turn: what its generation and settlement read, or what became of its sources.

        Once its placeholder is in the room, a preparation failure is the reply's dispatch failure, which its records
        show and settle; before that, the failure releases the reply for a retry.
        """
        execution_identity = self.deps.tool_runtime.build_execution_identity(
            target=resolved_target,
            user_id=request.user_id,
        )
        member_names = () if team is None else team.member_names
        request = replace(request, participating_agent_names=member_names or (self.deps.agent_name,))
        history_scope = (
            self.deps.state_writer.history_scope()
            if team is None
            else self.deps.state_writer.team_history_scope(
                member_names,
                requester_user_id=execution_identity.requester_id,
            )
        )
        if (suppression := _runs_nothing(request)) is not None:
            return await self._finalize_empty_prompt_locked(
                request,
                resolved_target=resolved_target,
                response_kind="ai" if team is None else "team",
                history_scope=history_scope,
                reply_entity_names=member_names,
                suppression=suppression,
            )
        admitted = await self._admit_locked_turn(
            request,
            resolved_target=resolved_target,
            history_scope=history_scope,
            reply_entity_names=member_names,
        )
        if not isinstance(admitted, ResponseRequest):
            return admitted
        request = admitted
        if team is None:
            _approve_continued_reply(participation, request)
            # The cold-cache judge sees the model before the budget, which the response runtime applies after it.
            runtime_model = resolve_turn_model(
                self.deps.runtime.config,
                self.deps.runtime_paths,
                ModelScope(
                    entity=self.deps.agent_name,
                    room_id=resolved_target.room_id,
                    thread_id=resolved_target.resolved_thread_id,
                    explicit=request.scheduled_model,
                ),
                None,
            )
        else:
            team_models = (
                None
                if team.rejection_message is not None
                else resolve_team_turn_models(
                    self.deps.runtime.config,
                    self.deps.runtime_paths,
                    ModelScope(
                        entity=self.deps.agent_name,
                        room_id=request.room_id,
                        thread_id=resolved_target.resolved_thread_id,
                        explicit=request.scheduled_model,
                    ),
                    requested_team_agent_names(team.member_names),
                    self._turn_budget(request),
                )
            )
        placeholder_event_id = await self._send_placeholder(
            request,
            resolved_target=resolved_target,
            history_scope=history_scope,
            execution_identity=execution_identity,
            placeholder_message=(
                None
                if self.locked.is_quiet(request) or request.participation is not None
                else AGENT_PLACEHOLDER
                if team is None
                else TEAM_PLACEHOLDER
            ),
        )
        try:
            prepared = await self._prepare_claimed_request(
                request,
                resolved_target=resolved_target,
                placeholder_event_id=placeholder_event_id,
            )
            if not isinstance(prepared, ResponseRequest):
                return prepared
            if team is None:
                return await self._prepare_agent_turn(
                    prepared,
                    resolved_target=resolved_target,
                    runtime_model=runtime_model,
                    history_scope=history_scope,
                    execution_identity=execution_identity,
                    participation=participation,
                )
            if team.rejection_message is None:
                assert team_models is not None
                return await self._prepare_team_turn(
                    prepared,
                    team=team,
                    team_models=team_models,
                    resolved_target=resolved_target,
                    history_scope=history_scope,
                )
        except (ReplyMembershipPendingError, RevisionSnapshotChangedError, _PostLockRequestPreparationError):
            raise
        except Exception as error:
            if placeholder_event_id is None:
                raise
            raise _PostLockRequestPreparationError from error
        # A rejection is the turn's settlement, not its preparation: its failure is no dispatch failure.
        await self._settle_team_rejection(prepared, resolved_target, team.rejection_message)
        return Owned()

    async def _settle_team_rejection(
        self,
        request: ResponseRequest,
        resolved_target: MessageTarget,
        reason: str,
    ) -> None:
        """Answer a team turn with why it cannot run, as one final delivery."""
        lifecycle = self.locked.build_lifecycle(request, response_kind="team")
        progress = TurnProgress()

        async def deliver_rejection(message_id: str | None) -> FinalDeliveryOutcome:
            return await self.deps.delivery_gateway.deliver_final(
                FinalDeliveryRequest(
                    target=resolved_target,
                    existing_event_id=message_id,
                    response_text=reason,
                    identity=lifecycle.identity,
                    tool_trace=None,
                    extra_content=None,
                ),
            )

        await self.locked.run_and_settle_locked_response(
            request,
            target=resolved_target,
            lifecycle=lifecycle,
            progress=progress,
            response_function=deliver_rejection,
            build_post_response_outcome=lambda _final_outcome: ResponseOutcome(),
            post_response_deps=lambda: self.locked.post_response_deps(request),
        )

    async def _prepare_team_turn(
        self,
        request: ResponseRequest,
        *,
        team: _TeamResponseRequest,
        team_models: TeamTurnModelSelection,
        resolved_target: MessageTarget,
        history_scope: HistoryScope,
    ) -> _LockedTurn:
        """Prepare an admitted team turn: its members' context, its leader's dispatch, and the stream that runs it."""
        agent_names = list(team.member_names)
        memory_prompt, memory_thread_history, prepared_prompt, model_thread_history = prepare_memory_and_model_context(
            request.prompt,
            request.thread_history,
            config=self.deps.runtime.config,
            runtime_paths=self.deps.runtime_paths,
            # A team turn is dated in the zone of the entity whose bot runs it, as its leader and prompt are.
            timezone=entity_timezone(self.deps.runtime.config, self.deps.agent_name),
            model_prompt=request.model_prompt,
        )
        tool_context = self.deps.tool_runtime.build_turn_context(
            resolved_target,
            user_id=request.user_id or "",
            active_model_name=team_models.team_model_name,
            attachment_ids=request.attachment_ids,
            correlation_id=correlation_id_for_request(request),
            source_envelope=request.response_envelope,
        )
        execution_identity = tool_context.execution_identity
        configured_team_name = self.deps.runtime.config.configured_team_name(self.deps.agent_name)
        self.deps.runtime.config.assert_team_agents_supported(
            requested_team_agent_names(agent_names),
            allow_direct_private_agents=allow_direct_private_team_agents(
                execution_identity,
                configured_team_name=configured_team_name,
            ),
        )
        orchestrator = self.deps.runtime.orchestrator
        show_tool_calls = self._turn_show_tool_calls()
        runtime = PreparedResponseRuntime(
            media_inputs=request.media or MediaInputs(),
            model_prompt=prepared_prompt,
            active_model_name=team_models.team_model_name,
            show_tool_calls=show_tool_calls,
            tool_context=tool_context,
        )
        run_id = str(uuid4())
        mode = TeamMode(team.team_mode)
        team_spec = TeamTurnSpec(
            agent_names=agent_names,
            mode=mode,
            orchestrator=orchestrator,
            model_name=team_models.team_model_name,
            member_model_names=team_models.member_model_names,
            configured_team_name=configured_team_name,
            response_sender_id=self.deps.matrix_full_id,
        )
        # Team entries refine entity_label to the materialized team label and
        # append the knowledge-availability enrichment before the turn runs.
        team_turn_ctx = self._turn_context(
            request,
            runtime,
            run_id=run_id,
            # A team turn without a requester answers for the identity it runs as.
            requester_id=request.user_id or execution_identity.requester_id,
            prompt=prepared_prompt,
            thread_history=model_thread_history,
            media=request.media,
            transient_enrichment_items=request.transient_enrichment_items,
            matrix_message_available=any(
                room_turn_offers(self.deps.runtime.config, name, "matrix_message") for name in agent_names
            ),
        )

        def stream(
            _runtime: PreparedResponseRuntime,
            active_event_ids: set[str],
            compaction_lifecycle: MatrixCompactionLifecycle | None,
            sinks: TurnSinks,
        ) -> AsyncIterator[StreamInputChunk]:
            return team_response_stream(
                team_spec,
                replace(
                    team_turn_ctx,
                    active_event_ids=frozenset(active_event_ids),
                    compaction_lifecycle=compaction_lifecycle,
                ),
                sinks=sinks,
            )

        # A configured team keeps the conversations it answers in its members' shared memory.
        queue_memory_persistence = (
            self.locked.memory_persistence(
                agent_name=agent_names,
                session_id=resolved_target.session_id,
                execution_identity=execution_identity,
                prompt=memory_prompt,
                thread_history=memory_thread_history,
                user_id=request.user_id,
                hook_source=request.response_envelope.hook_source,
                origin=request.response_envelope.origin,
            )
            if configured_team_name is not None
            else None
        )
        persist_response_event_id = self.locked.build_persist_response_event_id_effect(
            session_id=resolved_target.session_id,
            scope=history_scope,
            execution_identity=execution_identity,
        )
        return _LockedTurn(
            request=request,
            response_kind="team",
            history_scope=history_scope,
            execution_identity=execution_identity,
            runtime=runtime,
            run_id=run_id,
            streaming_cls=ReplacementStreamingResponse,
            stream=stream,
            post_response_deps=lambda: self.locked.post_response_deps(
                request,
                queue_memory_persistence=queue_memory_persistence,
                persist_response_event_id=persist_response_event_id,
            ),
            team_member_names=team.member_names,
            team_mode=mode.value,
        )

    async def _generate_and_settle(self, turn: _LockedTurn) -> None:
        """Generate a prepared turn's reply, streamed or delivered whole, and settle its lifecycle once."""
        request = turn.request
        # A resumed reply is a stream, and a whole answer would replace what it already showed.
        use_streaming = _resumed_reply() is not None or (
            not self.locked.is_quiet(request)
            and await should_use_streaming(
                self.locked.client(),
                request.room_id,
                requester_user_id=request.user_id,
                enable_streaming=self.deps.runtime.config.defaults.enable_streaming,
            )
        )
        if request.pipeline_timing is not None:
            request.pipeline_timing.note(
                response_kind="agent" if turn.response_kind == "ai" else "team",
                used_streaming=use_streaming,
            )
        # The turn driver writes the turn's record; this runner reads how it ended.
        recorder = TurnRecorder()
        progress = TurnProgress()
        lifecycle = self.locked.build_lifecycle(request, response_kind=turn.response_kind)

        # Set once the turn's generation returned a delivery; a turn cut short later, such as by a Stop during
        # message:before_response, has a completed record but did not deliver it.
        generated = False

        def build_post_response_outcome(_final_delivery_outcome: FinalDeliveryOutcome) -> ResponseOutcome:
            return ResponseOutcome(
                response_run_id=recorder.run_id or turn.run_id,
                # A turn that never returned a delivery was cut short, even when its record completed first.
                run_succeeded=generated and recorder.outcome == "completed",
                response_target=turn.runtime.tool_context.target,
                thread_summary_message_count_hint=self.locked.thread_summary_message_count_hint(request),
                thread_summary_entity_name=self.deps.agent_name,
            )

        async with self._turn_dispatch_scope(turn, progress=progress) as dispatched:

            async def generate(_message_id: str | None) -> FinalDeliveryOutcome:
                nonlocal generated
                delivery = await self._deliver_turn(
                    dispatched,
                    use_streaming=use_streaming,
                    recorder=recorder,
                    response_identity=lifecycle.identity,
                    progress=progress,
                )
                participation = dispatched.runtime.participation
                if (
                    participation is not None
                    and participation.is_declined
                    and request.participation is not None
                    and request.participation.decline_reaction is not None
                    and delivery.ending == DECLINED.ending
                ):
                    await self.deps.delivery_gateway.send_judgment_reaction(
                        turn_id=request.response_envelope.source_event_id,
                        room_id=request.room_id,
                        event_id=request.sources.logical_source_event_ids[-1],
                        key=request.participation.decline_reaction,
                        kind="participation_decline",
                    )
                generated = True
                return delivery

            await self.locked.run_and_settle_locked_response(
                request,
                target=dispatched.runtime.tool_context.target,
                lifecycle=lifecycle,
                progress=progress,
                response_function=generate,
                build_post_response_outcome=build_post_response_outcome,
                post_response_deps=dispatched.post_response_deps,
                approval_suspension_handler=lambda pause: self.approvals.suspend_for_approval(
                    _paused_with_committed_presentation(pause, show_tool_calls=dispatched.runtime.show_tool_calls),
                    request=request,
                    target=dispatched.runtime.tool_context.target,
                    execution_identity=dispatched.execution_identity,
                    entity_kind="agent" if dispatched.response_kind == "ai" else "team",
                    history_scope=dispatched.history_scope,
                    show_tool_calls=dispatched.runtime.show_tool_calls,
                    team_member_names=dispatched.team_member_names,
                    team_mode=dispatched.team_mode,
                ),
                participation=dispatched.runtime.participation,
                reply_session=ReplySession(dispatched.history_scope, dispatched.runtime.tool_context),
            )

    @asynccontextmanager
    async def _turn_dispatch_scope(
        self,
        turn: _LockedTurn,
        *,
        progress: TurnProgress,
    ) -> AsyncIterator[_LockedTurn]:
        """Bind an agent turn's CLI approval publication to its reply; a team turn's members ask through its leader."""
        if turn.response_kind != "ai":
            yield turn
            return
        async with self.approvals.cli_approval_scope(
            turn.runtime,
            request=turn.request,
            progress=progress,
            history_scope=turn.history_scope,
        ) as runtime:
            yield replace(turn, runtime=runtime)

    async def _deliver_turn(
        self,
        turn: _LockedTurn,
        *,
        use_streaming: bool,
        recorder: TurnRecorder,
        response_identity: ResponseIdentity,
        progress: TurnProgress,
    ) -> FinalDeliveryOutcome:
        """Run the turn's stream into its reply: streamed as it grows, or collected and delivered whole."""
        request = turn.request
        active_event_ids = await self._active_response_event_ids(request.room_id)
        compaction_lifecycle = self._build_compaction_lifecycle(
            target=turn.runtime.tool_context.target,
            request=request,
        )
        # The caller-owned collectors stay live: the gateway snapshots the run metadata as the stream ends.
        run_metadata_content: dict[str, Any] = {}
        tool_trace: list[ToolTraceEntry] = []
        sinks = TurnSinks(
            turn_recorder=recorder,
            run_metadata_collector=run_metadata_content,
            response_event_id=_reply_event_id,
        )

        def response_stream() -> AsyncIterator[StreamInputChunk]:
            return turn.stream(turn.runtime, active_event_ids, compaction_lifecycle, sinks)

        def final_extra_content() -> dict[str, Any] | None:
            # A turn that published no run metadata still names the run the record holds.
            fallback = ai_run_extra_content_from_metadata(recorder.run_metadata) or {}
            return merge_response_extra_content({**fallback, **run_metadata_content} or None, request.attachment_ids)

        if use_streaming:
            return await self._deliver_streamed(
                turn,
                response_stream,
                response_identity=response_identity,
                run_metadata_content=run_metadata_content,
                tool_trace=tool_trace,
                final_extra_content=final_extra_content,
                progress=progress,
            )
        return await self._deliver_collected(
            turn,
            response_stream,
            response_identity=response_identity,
            tool_trace=tool_trace,
            final_extra_content=final_extra_content,
            progress=progress,
        )

    async def _deliver_streamed(
        self,
        turn: _LockedTurn,
        response_stream: Callable[[], AsyncIterator[StreamInputChunk]],
        *,
        response_identity: ResponseIdentity,
        run_metadata_content: dict[str, Any],
        tool_trace: list[ToolTraceEntry],
        final_extra_content: Callable[[], dict[str, Any] | None],
        progress: TurnProgress,
    ) -> FinalDeliveryOutcome:
        """Stream the turn into its reply and finalize what the stream left."""
        request = turn.request
        runtime = turn.runtime
        label = "Bot" if turn.response_kind == "ai" else "Team"
        # What the reply showed before this stream, which its terminal handling continues from.
        shown_event_id = existing_event_id(request)
        participation = runtime.participation
        try:
            async with response_typing_indicator(
                self.locked.client(),
                request,
                response_run_id=turn.run_id,
                quiet=self.locked.is_quiet(request),
                participation=participation,
            ):
                transport_outcome = await self.deps.delivery_gateway.deliver_stream(
                    StreamingDeliveryRequest(
                        target=runtime.tool_context.target,
                        identity=response_identity,
                        response_stream=self.deps.tool_runtime.stream_in_context(
                            tool_context=runtime.tool_context,
                            stream_factory=response_stream,
                        ),
                        existing_event_id=shown_event_id,
                        adopt_existing_placeholder=_shows_placeholder(request),
                        show_tool_calls=runtime.show_tool_calls,
                        # The live collector dict: the turn driver fills it at terminal settle, before the stream's
                        # final edit snapshots extra_content, so the ai_run metadata lands on the wire.
                        extra_content=merge_response_extra_content(run_metadata_content, request.attachment_ids),
                        tool_trace_collector=tool_trace,
                        streaming_cls=turn.streaming_cls,
                        pipeline_timing=request.pipeline_timing,
                        visible_event_id_callback=progress.note_delivery_started,
                        visible_progress_callback=self.lifecycle_coordinator.visible_progress_callback(
                            runtime.tool_context.target,
                        ),
                        allow_new_terminal_message=lambda: participation is None or participation.approved,
                        placeholder_ready=None if participation is None else participation.wait_for_approval,
                    ),
                )
            if request.pipeline_timing is not None:
                request.pipeline_timing.mark("streaming_complete")
            _raise_if_process_shutdown()
        except asyncio.CancelledError as exc:
            log_cancelled_response(
                self.deps.logger,
                exc=exc,
                message_id=shown_event_id,
                restart_message=f"{label} streaming response interrupted by sync restart",
                user_stop_message=f"{label} streaming response cancelled by user",
                interrupted_message=f"{label} streaming response interrupted — traceback for diagnosis",
            )
            raise

        if participation is not None and participation.is_silent:
            return DECLINED
        progress.note_delivery_started()
        delivery = await self.deps.delivery_gateway.finalize_streamed_response(
            FinalizeStreamedResponseRequest(
                target=runtime.tool_context.target,
                stream_transport_outcome=transport_outcome,
                initial_delivery_kind="edited" if shown_event_id else "sent",
                identity=response_identity,
                tool_trace=tool_trace if runtime.show_tool_calls else None,
                extra_content=final_extra_content(),
                # Read again after the stream, which may have sent the reply's first event.
                existing_event_id=existing_event_id(request),
                existing_event_is_placeholder=_replaceable_placeholder(request),
            ),
        )
        self._note_final_delivery_timing(request, delivery)
        return delivery

    async def _deliver_collected(
        self,
        turn: _LockedTurn,
        response_stream: Callable[[], AsyncIterator[StreamInputChunk]],
        *,
        response_identity: ResponseIdentity,
        tool_trace: list[ToolTraceEntry],
        final_extra_content: Callable[[], dict[str, Any] | None],
        progress: TurnProgress,
    ) -> FinalDeliveryOutcome:
        """Collect the turn's stream into one reply, then deliver it whole."""
        request = turn.request
        runtime = turn.runtime
        label = "Non-streaming" if turn.response_kind == "ai" else "Team non-streaming"

        async def collect() -> str:
            body, collected_trace = await collect_streamed_response_content(
                response_stream(),
                # Hidden tool calls still separate the text around them, as a streamed reply shows it.
                presentation=CollectedStreamPresentation(
                    show_tool_calls=runtime.show_tool_calls,
                    track_hidden_tools=True,
                    replaces_text=turn.streaming_cls.replaces_text,
                ),
            )
            tool_trace.extend(collected_trace)
            return body

        try:
            async with response_typing_indicator(
                self.locked.client(),
                request,
                response_run_id=turn.run_id,
                quiet=self.locked.is_quiet(request),
                participation=runtime.participation,
            ):
                with tool_runtime_context(runtime.tool_context):
                    response_text = await collect()
            _raise_if_process_shutdown()
        except asyncio.CancelledError as exc:
            log_cancelled_response(
                self.deps.logger,
                exc=exc,
                message_id=existing_event_id(request),
                restart_message=f"{label} response interrupted by sync restart",
                user_stop_message=f"{label} response cancelled by user",
                interrupted_message=f"{label} response interrupted — traceback for diagnosis",
            )
            raise

        if runtime.participation is not None and runtime.participation.is_silent:
            return DECLINED
        progress.note_delivery_started()
        delivery = await self.deps.delivery_gateway.deliver_final(
            FinalDeliveryRequest(
                target=runtime.tool_context.target,
                existing_event_id=existing_event_id(request),
                response_text=response_text,
                identity=response_identity,
                tool_trace=tool_trace if runtime.show_tool_calls else None,
                extra_content=final_extra_content(),
            ),
        )
        self._note_final_delivery_timing(request, delivery)
        return delivery

    @timed("prepare_response_runtime")
    async def prepare_response_runtime(
        self,
        request: ResponseRequest,
        *,
        show_tool_calls: bool,
        active_model_name: str | None = None,
        participation: ParticipationGate | None = None,
    ) -> PreparedResponseRuntime:
        """Resolve shared runtime context for one streaming or non-streaming response."""
        resolved_target = request.response_envelope.target
        media_inputs = request.media or MediaInputs()
        resolved_model_prompt = request.model_prompt or request.prompt
        active_model_name = resolve_turn_model(
            self.deps.runtime.config,
            self.deps.runtime_paths,
            ModelScope(
                entity=self.deps.agent_name,
                room_id=resolved_target.room_id,
                thread_id=resolved_target.resolved_thread_id,
                explicit=active_model_name if active_model_name is not None else request.scheduled_model,
            ),
            self._turn_budget(request),
        ).model_name
        tool_context = self.deps.tool_runtime.build_turn_context(
            resolved_target,
            user_id=request.user_id,
            active_model_name=active_model_name,
            attachment_ids=request.attachment_ids,
            correlation_id=correlation_id_for_request(request),
            source_envelope=request.response_envelope,
        )
        return PreparedResponseRuntime(
            media_inputs=media_inputs,
            model_prompt=resolved_model_prompt,
            active_model_name=active_model_name,
            show_tool_calls=show_tool_calls,
            tool_context=tool_context,
            participation=participation
            if participation is not None
            else _participation_for_request(request, self.deps.runtime.config, self.deps.runtime_paths),
        )

    async def generate_response(self, request: ResponseRequest) -> SourceFate:
        """Generate and send/edit an agent response with lifecycle locking; return what it did with its sources.

        Whether it is a contact turn is read here once, and the whole turn follows it.
        """
        request = self.locked.with_contact(request)
        return await self._run_locked_response_lifecycle(
            request,
            response_kind="ai",
            locked_operation=lambda resolved_target: self._run_locked_turn(
                request,
                team=None,
                resolved_target=resolved_target,
                participation=_participation_for_request(request, self.deps.runtime.config, self.deps.runtime_paths),
            ),
        )

    async def _decline_unprepared_turn(self, request: ResponseRequest, error: Exception) -> SourceFate:
        """Quietly decline a turn whose preparation failed before its participation decision."""
        self.deps.logger.error("Response preparation skipped before participation", error=str(error), exc_info=error)
        claimed = await self._end_unanswered(
            request,
            DECLINED,
            response_kind="ai",
            post_response_outcome=ResponseOutcome(run_succeeded=False),
        )
        # Without a claimed reply, nothing settles the sources but their caller.
        return Owned() if claimed else Unclaimed(UnclaimedReason.DECLINED)

    async def _cold_cache_model(
        self,
        request: ResponseRequest,
        *,
        runtime_model: TurnModel,
        thread_id: str | None,
    ) -> str:
        """Let a judge move a thread whose prompt cache went cold to the agent's cheap model."""
        resolved_model = runtime_model.model_name
        config = self.deps.runtime.config
        agent = config.agents.get(self.deps.agent_name)
        cheap_model = agent_cheap_model(config, self.deps.agent_name) if agent is not None else None
        if (
            agent is None
            or agent.cold_cache is None
            or cheap_model is None
            # Scheduled, thread, and room choices are explicit, and an earlier switch is a thread choice.
            or runtime_model.overridden
            or resolved_model != agent.model
            # Only a thread can be switched for good.
            or thread_id is None
            # A regenerated or resumed answer follows one the agent model just gave.
            or request.edit_regeneration
            or _resumed_reply() is not None
            # The judge cannot see media, and the cheap model may not read it.
            or _request_has_media(request)
        ):
            return resolved_model
        selected = await select_cold_cache_model(
            agent.cold_cache,
            config=config,
            runtime_paths=self.deps.runtime_paths,
            agent_name=self.deps.agent_name,
            agent_model=resolved_model,
            cheap_model=cheap_model,
            idle_seconds=idle_seconds_before_turn(
                request.thread_history,
                agent_user_id=self.deps.matrix_full_id,
                source_event_ids=request.sources.logical_source_event_ids,
                now=time.time(),
            ),
            context=self.locked.judgment_context(request),
            prompt=request.prompt,
        )
        if selected == resolved_model:
            return selected
        try:
            # The thread stays on the cheap model, like a `!model` choice, until someone changes it.
            set_thread_model_override(
                self.deps.runtime_paths,
                thread_id=thread_id,
                model_name=selected,
                entity_names=(self.deps.agent_name,),
                config=config,
            )
        except OSError:
            # The switch is an optimization, so a failed save must not fail the reply.
            self.deps.logger.warning("cold_cache_switch_save_failed", thread_id=thread_id, exc_info=True)
            return resolved_model
        return selected

    async def _prepare_agent_turn(
        self,
        request: ResponseRequest,
        *,
        resolved_target: MessageTarget,
        runtime_model: TurnModel,
        history_scope: HistoryScope,
        execution_identity: ToolExecutionIdentity,
        participation: ParticipationGate | None,
    ) -> _LockedTurn:
        """Prepare an admitted agent turn: its model, runtime, effects, and the stream that runs it."""
        memory_prompt, memory_thread_history, model_prompt_text, model_thread_history = (
            prepare_memory_and_model_context(
                request.prompt,
                request.thread_history,
                config=self.deps.runtime.config,
                runtime_paths=self.deps.runtime_paths,
                timezone=entity_timezone(self.deps.runtime.config, self.deps.agent_name),
                model_prompt=request.model_prompt,
            )
        )
        normalized_request = replace(
            request,
            prompt=memory_prompt,
            model_prompt=model_prompt_text,
            thread_history=model_thread_history,
            media=request.media or MediaInputs(),
        )
        session_id = resolved_target.session_id
        active_model_name = await self._cold_cache_model(
            request,
            runtime_model=runtime_model,
            thread_id=resolved_target.resolved_thread_id,
        )
        if request.pipeline_timing is not None:
            request.pipeline_timing.mark("response_runtime_start")
        runtime = await self.prepare_response_runtime(
            normalized_request,
            show_tool_calls=self._turn_show_tool_calls(),
            active_model_name=active_model_name,
            participation=participation,
        )
        if request.pipeline_timing is not None:
            request.pipeline_timing.mark("response_runtime_ready")
        if request.contact is not None:
            # The other side drives a contact turn, so it writes no long-term memory and teaches the agent no skill.
            queue_memory_persistence = None
            queue_skill_review = None
        else:
            queue_memory_persistence = self.locked.memory_persistence(
                agent_name=self.deps.agent_name,
                session_id=session_id,
                execution_identity=execution_identity,
                prompt=memory_prompt,
                thread_history=memory_thread_history,
                user_id=request.user_id,
                hook_source=request.response_envelope.hook_source,
                origin=request.response_envelope.origin,
            )
            queue_skill_review, runtime = self._response_skill_review(
                request,
                runtime,
                session_id=session_id,
                execution_identity=execution_identity,
            )
        persist_response_event_id = self.locked.build_persist_response_event_id_effect(
            session_id=session_id,
            scope=history_scope,
            execution_identity=execution_identity,
        )
        run_id = str(uuid4())

        async def stream(
            runtime: PreparedResponseRuntime,
            active_event_ids: set[str],
            compaction_lifecycle: MatrixCompactionLifecycle | None,
            sinks: TurnSinks,
        ) -> AsyncIterator[StreamInputChunk]:
            pipeline_timing = normalized_request.pipeline_timing
            if pipeline_timing is not None:
                pipeline_timing.mark("knowledge_access_start")
            knowledge_resolution = await self.deps.knowledge_access.resolve_for_agent_async(
                self.deps.agent_name,
                execution_identity=runtime.tool_context.execution_identity,
            )
            if pipeline_timing is not None:
                pipeline_timing.mark("knowledge_access_ready")
            ctx = self._agent_turn_context(
                normalized_request,
                runtime=runtime,
                run_id=run_id,
                active_event_ids=active_event_ids,
                knowledge=knowledge_resolution.knowledge,
                transient_enrichment_items=append_knowledge_availability_enrichment(
                    normalized_request.transient_enrichment_items,
                    knowledge_resolution.unavailable,
                ),
                compaction_lifecycle=compaction_lifecycle,
            )
            response_stream = stream_agent_response(ctx, self.deps.runtime_paths, self.deps.runtime.config, sinks=sinks)
            async with closing_async_stream(response_stream):
                async for chunk in response_stream:
                    yield chunk

        return _LockedTurn(
            request=request,
            response_kind="ai",
            history_scope=history_scope,
            execution_identity=execution_identity,
            runtime=runtime,
            run_id=run_id,
            streaming_cls=StreamingResponse,
            stream=stream,
            post_response_deps=lambda: self.locked.post_response_deps(
                request,
                queue_memory_persistence=queue_memory_persistence,
                queue_skill_review=queue_skill_review,
                persist_response_event_id=persist_response_event_id,
            ),
        )
