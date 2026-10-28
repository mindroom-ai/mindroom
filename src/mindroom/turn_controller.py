"""Control one inbound turn from ingress to recorded outcome."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, replace
from enum import Enum
from functools import partial
from typing import TYPE_CHECKING, Any, Protocol, cast

from mindroom import interactive
from mindroom.authorization import ensure_room_membership_synced, is_requester_joined_to_room
from mindroom.background_tasks import create_background_task, run_coroutine_until_complete
from mindroom.coalescing.batch import (
    CoalescingKey,
    PendingEvent,
    PreparedTurn,
    build_prepared_turn,
    is_active_follow_up_coalescing_key,
    requester_coalescing_key,
)
from mindroom.coalescing.gate import CoalescingGate, ReadyPendingEvent
from mindroom.coalescing.ingress_lanes import IngressRetryError, ReceiptLaneKey
from mindroom.coalescing.ingress_ticket import IngressTicket
from mindroom.constants import (
    ROUTER_AGENT_NAME,
    SCHEDULED_MODEL_KEY,
    STREAM_STATUS_ERROR,
    STREAM_STATUS_KEY,
    RuntimePaths,
)
from mindroom.delivery_gateway import SendTextRequest
from mindroom.dispatch_callback_outcome import TurnDispatchOutcome
from mindroom.dispatch_handoff import (
    DispatchEvent,
    DispatchIngressMetadata,
    DispatchPayloadMetadata,
    MediaDispatchEvent,
    PreparedIngress,
    dispatch_prompt_for_event,
    prepare_media_ingress,
)
from mindroom.dispatch_recovery_context import turn_dispatch_recovery_active
from mindroom.dispatch_replay_guard import (
    has_newer_unresponded_in_thread,
    has_newer_unresponded_journal_thread_event,
)
from mindroom.dispatch_source import (
    EDIT_SOURCE_KIND,
    IMAGE_SOURCE_KIND,
    INTERACTIVE_SELECTION_SOURCE_KIND,
    MEDIA_SOURCE_KIND,
    MESSAGE_SOURCE_KIND,
    TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
    VOICE_SOURCE_KIND,
    ScheduledHistoryBudget,
    scheduled_history_limit_from_content,
)
from mindroom.error_handling import get_user_friendly_error_message
from mindroom.hooks import MessageEnvelope, hook_ingress_policy
from mindroom.inbound_turn_normalizer import (
    DispatchPayloadWithAttachmentsRequest,
    InboundTurnNormalizer,
    TextNormalizationRequest,
)
from mindroom.logging_config import bound_log_context
from mindroom.matrix.client_visible_messages import extract_visible_edit_body
from mindroom.matrix.conversation_reads import ThreadReadMode
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.media import (
    AudioMessageEvent,
    FileMessageEvent,
    MatrixMediaEvent,
    is_audio_message_event,
    is_file_message_event,
    is_image_message_event,
    is_matrix_media_dispatch_event,
)
from mindroom.matrix.member_display_names import room_member_display_names
from mindroom.matrix.message_content import is_v2_sidecar_text_preview
from mindroom.matrix.thread_history_result import ThreadHistoryResult
from mindroom.matrix.thread_membership import RelatedEventUnavailableError, ThreadMembershipLookupError
from mindroom.matrix.transport_progress import is_unfinished_reply
from mindroom.response_admission import admitted_response_decision
from mindroom.response_lifecycle import response_lifecycle_reservation_context
from mindroom.response_payload_preparation import (
    DispatchPayloadInputs,
    ResponsePayloadPreparation,
)
from mindroom.response_runner import PostLockRequestPreparationError, ResponseRequest
from mindroom.response_sources import ResponseSources
from mindroom.router_relay import execute_router_relay
from mindroom.teams.formation import TeamIntent, select_ad_hoc_team_mode
from mindroom.text_ingress_dispatch import dispatch_text_message
from mindroom.thread_utils import (
    check_agent_mentioned,
    is_router_only_agent_mention,
    thread_requires_explicit_agent_targeting,
)
from mindroom.timestamp_formatting import normalize_timestamp_ms
from mindroom.timing import (
    DispatchPipelineTiming,
    attach_dispatch_pipeline_timing,
    create_dispatch_pipeline_timing,
    emit_elapsed_timing,
    event_timing_scope,
    get_dispatch_pipeline_timing,
)
from mindroom.turn_origin import TurnIntent
from mindroom.turn_policy import PreparedDispatch, ResponseAction, TurnPolicy
from mindroom.turn_record import TurnRecord, canonicalize_turn_record
from mindroom.turns.facts import command_for, is_router_voice_echo
from mindroom.voice.readiness import VoiceReadiness

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    import nio
    import structlog

    from mindroom.bot_runtime_view import BotRuntimeView
    from mindroom.command_turn_executor import CommandTurnExecutor
    from mindroom.conversation_resolver import ConversationResolver, MessageContext
    from mindroom.delivery_gateway import DeliveryGateway
    from mindroom.event_journal import PendingTurnView, PrincipalStore
    from mindroom.hooks.ingress import IngressHookRunner
    from mindroom.ingress_validation import IngressValidator
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.matrix.identity import MatrixID
    from mindroom.message_target import MessageTarget
    from mindroom.response_lifecycle import QueuedHumanNoticeReservation
    from mindroom.response_runner import ResponseRunner
    from mindroom.teams.spec import TeamMode
    from mindroom.turn_store import SourcePreparation, TurnStore
    from mindroom.turns.facts import IngressFacts
    from mindroom.visible_response_reconciliation import VisibleResponseReconciler
    from mindroom.voice.visible_voice_echo import VisibleVoiceEchoLifecycle


@dataclass(frozen=True)
class InteractiveSelectionDispatch:
    """Deferred selection work carried through receipt-ordered coalescing."""

    response_factory: Callable[[], Awaitable[bool]]
    response_target: MessageTarget
    source_event_id: str
    facts: IngressFacts
    selected_value: str


def _room_level_context_event(event: PreparedIngress) -> PreparedIngress:
    """Return an event view that cannot pull dispatch context through Matrix relations."""
    if not isinstance(event.source, dict):
        return event
    content = event.source.get("content")
    if not isinstance(content, dict) or "m.relates_to" not in content:
        return event
    stripped_content = dict(content)
    stripped_content.pop("m.relates_to", None)
    return replace(event, source={**event.source, "content": stripped_content})


# Ingress treats only a human's router relay as a handoff, so the router's relay of a
# task a bot account scheduled arrives as a notice and must keep its per-run settings too.
_SCHEDULED_RUN_INTENTS = frozenset({TurnIntent.SCHEDULED_FIRE, TurnIntent.ROUTER_HANDOFF, TurnIntent.ROUTER_NOTICE})
# Hook dispatches come only from managed senders, such as automations choosing a model for their prompt.
_PER_RUN_MODEL_INTENTS = _SCHEDULED_RUN_INTENTS | {TurnIntent.HOOK_DISPATCH}


def _scheduled_model_for_dispatch(event: DispatchEvent, origin_intent: TurnIntent) -> str | None:
    """Accept a per-run model only from trusted scheduled fires, hook dispatches, or the router's relays of them."""
    if origin_intent not in _PER_RUN_MODEL_INTENTS:
        return None
    content = event.source.get("content") if isinstance(event.source, dict) else None
    if not isinstance(content, dict):
        return None
    model = content.get(SCHEDULED_MODEL_KEY)
    return model if isinstance(model, str) and model else None


def _scheduled_history_budget_for_dispatch(
    event: DispatchEvent,
    origin_intent: TurnIntent,
) -> ScheduledHistoryBudget | None:
    """Return the trusted history budget and prompt source for one scheduled dispatch."""
    if origin_intent not in _SCHEDULED_RUN_INTENTS:
        return None
    content = event.source.get("content") if isinstance(event.source, dict) else None
    if not isinstance(content, dict):
        return None
    history_limit = scheduled_history_limit_from_content(content)
    if history_limit is None:
        return None
    source_event_id = (
        event.event_id
        if origin_intent is TurnIntent.SCHEDULED_FIRE
        else EventInfo.from_event(event.source).reply_to_event_id
    )
    if source_event_id is None:
        return None
    return ScheduledHistoryBudget(limit=history_limit, source_event_id=source_event_id)


class _EditRegenerator(Protocol):
    """Minimal edit-regeneration surface needed by turn sequencing."""

    async def handle_message_edit(
        self,
        room: nio.MatrixRoom,
        event: nio.RoomMessageFormatted,
        event_info: EventInfo,
        facts: IngressFacts,
    ) -> bool | None:
        """Regenerate an edit and report any durable source handoff."""


@dataclass(frozen=True)
class _PrecheckedEvent[T]:
    """A raw or prepared event that already passed ingress prechecks, with the provenance precheck read."""

    event: T
    facts: IngressFacts


type _PrecheckedInboundMediaEvent = _PrecheckedEvent[MatrixMediaEvent]


# A selection queues as an ``interactive_selection`` barrier behind earlier ingress, but its
# answer replies to a message, so mid-turn judging treats it as one.
_SELECTION_RESPONSE_SOURCE_KIND = MESSAGE_SOURCE_KIND


def _media_source_kind(event: MatrixMediaEvent) -> str | None:
    """Return the source kind a media event's transport fixes; a long text sent as a file is text."""
    if is_audio_message_event(event):
        return VOICE_SOURCE_KIND
    if is_v2_sidecar_text_preview(event.source):
        return None
    return IMAGE_SOURCE_KIND if is_image_message_event(event) else MEDIA_SOURCE_KIND


class _IngressAdmissionOutcome(Enum):
    DEFERRED = "deferred"
    CONSUMED = "consumed"
    IGNORED = "ignored"


@dataclass(frozen=True)
class _ReplayGuardContext:
    """Dispatch-local evidence for deciding whether an older turn should still run."""

    history: Sequence[ResolvedVisibleMessage]
    degraded: bool
    thread_id: str | None


@dataclass(frozen=True)
class _DispatchPreparation:
    """Prepared dispatch plus evidence that must stay out of policy-visible context."""

    dispatch: PreparedDispatch
    replay_guard: _ReplayGuardContext


@dataclass(frozen=True)
class TurnControllerDeps:
    """Collaborators needed for turn control, policy, and execution."""

    runtime: BotRuntimeView
    logger: structlog.stdlib.BoundLogger
    runtime_paths: RuntimePaths
    agent_name: str
    matrix_id: MatrixID
    pending_turns: PendingTurnView
    interactive_questions: PrincipalStore
    resolver: ConversationResolver
    normalizer: InboundTurnNormalizer
    command_executor: CommandTurnExecutor
    turn_policy: TurnPolicy
    ingress_hook_runner: IngressHookRunner
    response_runner: ResponseRunner
    delivery_gateway: DeliveryGateway
    turn_store: TurnStore
    coalescing_gate: CoalescingGate
    edit_regenerator: _EditRegenerator
    ingress: IngressValidator
    visible_voice_echo: VisibleVoiceEchoLifecycle
    visible_responses: VisibleResponseReconciler
    retry_dispatch_sources: Callable[[str, tuple[str, ...]], None]
    settle_dispatch_sources: Callable[[tuple[str, ...]], Awaitable[None]]
    dispatch_source_is_terminal: Callable[[str], Awaitable[bool]]


@dataclass
class TurnController:
    """Own sequencing for one inbound text or media turn."""

    deps: TurnControllerDeps

    def _client(self) -> nio.AsyncClient:
        client = self.deps.runtime.client
        if client is None:
            msg = "Matrix client is not ready for turn execution"
            raise RuntimeError(msg)
        return client

    def reserve_prompt_ingress_order(
        self,
        room: nio.MatrixRoom,
        requester_user_id: str,
        *,
        receipt_time: float | None = None,
    ) -> IngressTicket:
        """Reserve receipt order for one prompt-like Matrix ingress item."""
        return IngressTicket(
            gate=self.deps.coalescing_gate,
            slot=self.deps.coalescing_gate.enter_lane(
                ReceiptLaneKey(room_id=room.room_id, sender_id=requester_user_id),
                receipt_time=receipt_time,
            ),
            turn_store=self.deps.turn_store,
        )

    async def _precheck_dispatch_event[T: DispatchEvent | MatrixMediaEvent](
        self,
        room: nio.MatrixRoom,
        event: T,
        *,
        source_kind: str | None = None,
        is_edit: bool = False,
    ) -> _PrecheckedEvent[T] | None:
        """Classify one event once, then run the shared prechecks on that classification."""
        facts = self.deps.ingress.classify(event, source_kind=source_kind)
        async with admitted_response_decision(
            self.deps.runtime.response_admission_gate,
            self.deps.response_runner.wait_for_admission_or_shutdown,
        ):
            if not await self.deps.ingress.precheck_event(room, event, facts, is_edit=is_edit):
                return None
        return _PrecheckedEvent(event=event, facts=facts)

    async def _has_newer_unresponded_in_thread(
        self,
        room_id: str,
        event: PreparedIngress,
        requester_user_id: str,
        thread_history: Sequence[ResolvedVisibleMessage],
        *,
        may_be_superseded_by_newer_requester_turn: bool,
    ) -> bool:
        """Return True when a newer unresponded message from the same requester exists."""
        return await has_newer_unresponded_in_thread(
            event,
            requester_user_id,
            thread_history,
            may_be_superseded_by_newer_requester_turn=may_be_superseded_by_newer_requester_turn,
            classify=self.deps.ingress.classify_source,
            is_handled=partial(self.deps.turn_store.handled_among, room_id),
            logger=self.deps.logger,
        )

    async def _has_newer_unresponded_journal_thread_event(
        self,
        *,
        room_id: str,
        event: PreparedIngress,
        requester_user_id: str,
        thread_id: str | None,
        may_be_superseded_by_newer_requester_turn: bool,
    ) -> bool:
        """Return positive replay proof from pending journal events when thread history degraded."""
        return await has_newer_unresponded_journal_thread_event(
            room_id=room_id,
            event=event,
            requester_user_id=requester_user_id,
            thread_id=thread_id,
            may_be_superseded_by_newer_requester_turn=may_be_superseded_by_newer_requester_turn,
            pending_turns=self.deps.pending_turns,
            classify=self.deps.ingress.classify_source,
            is_handled=partial(self.deps.turn_store.handled_among, room_id),
            logger=self.deps.logger,
        )

    def _should_skip_deep_synthetic_full_dispatch(
        self,
        *,
        event_id: str,
        envelope: MessageEnvelope,
    ) -> bool:
        """Return True when a deep synthetic hook relay must stop before dispatch."""
        resolved_policy = hook_ingress_policy(envelope)
        if resolved_policy.allow_full_dispatch:
            return False
        self.deps.logger.debug(
            "Ignoring deep synthetic hook relay before command/response dispatch",
            event_id=event_id,
            source_kind=envelope.source_kind,
            hook_source=envelope.hook_source,
            message_received_depth=envelope.message_received_depth,
        )
        return True

    @staticmethod
    def _same_response_lifecycle_target(left: MessageTarget, right: MessageTarget) -> bool:
        """Return whether two targets share the same response lifecycle lock."""
        return left.lifecycle_key == right.lifecycle_key

    def _queued_notice_reservation_if_busy(
        self,
        *,
        target: MessageTarget,
        envelope: MessageEnvelope,
    ) -> QueuedHumanNoticeReservation | None:
        """Reserve the mid-turn queued notice when this conversation has a running response."""
        if not envelope.origin.may_answer_interactive_prompt:
            return None
        if not self.deps.response_runner.has_active_response_for_target(target):
            return None
        return self.deps.response_runner.reserve_waiting_human_message(
            target=target,
            response_envelope=envelope,
        )

    def _event_allows_queued_notice(
        self,
        room: nio.MatrixRoom,
        event: PreparedIngress,
        envelope: MessageEnvelope,
    ) -> bool:
        """Return whether this event may notify the conversation's active response.

        Router handoffs and agent relays (for example a `matrix_message` handoff) that
        are addressed to another participant must not pause this agent's running turn.
        """
        if envelope.origin.intent not in {TurnIntent.ROUTER_HANDOFF, TurnIntent.TRUSTED_INTERNAL_RELAY}:
            return True
        return self._mentions_another_participant(room, event.source) is not True

    def _mentions_another_participant(self, room: nio.MatrixRoom, source: dict[str, Any]) -> bool | None:
        """Return whom a message mentions: ``None`` nobody, ``False`` this agent, ``True`` only other agents or people."""
        mentioned_agents, am_i_mentioned, has_non_agent_mentions = check_agent_mentioned(
            source,
            self.deps.matrix_id,
            self.deps.runtime.config,
            self.deps.runtime_paths,
            room=room,
        )
        if am_i_mentioned:
            return False
        return True if mentioned_agents or has_non_agent_mentions else None

    def _voice_queued_notice_reservation(
        self,
        *,
        preliminary_target: MessageTarget | None,
        target: MessageTarget,
        envelope: MessageEnvelope,
        queued_notice_reservation: QueuedHumanNoticeReservation | None,
    ) -> QueuedHumanNoticeReservation | None:
        """Keep, replace, or cancel the queued notice once the voice target is final."""
        if queued_notice_reservation is not None and (
            preliminary_target is None
            or not self._same_response_lifecycle_target(
                preliminary_target,
                target,
            )
        ):
            queued_notice_reservation.release()
            queued_notice_reservation = None
        busy = self.deps.response_runner.has_active_response_for_target(target)
        if queued_notice_reservation is not None:
            if busy:
                return queued_notice_reservation
            queued_notice_reservation.release()
            return None
        if not busy:
            return None
        return self._queued_notice_reservation_if_busy(target=target, envelope=envelope)

    async def _enqueue_prepared_text_for_dispatch(
        self,
        *,
        room: nio.MatrixRoom,
        prepared_event: PreparedIngress,
        facts: IngressFacts,
        envelope: MessageEnvelope,
        coalescing_thread_id: str | None,
        ticket: IngressTicket,
    ) -> _IngressAdmissionOutcome:
        """Queue one normalized text event; the gate decides busy-conversation routing."""
        target = self.deps.resolver.build_message_target(
            room_id=room.room_id,
            thread_id=coalescing_thread_id,
            reply_to_event_id=prepared_event.event_id,
            event_source=prepared_event.source,
        )
        if self._event_allows_queued_notice(room, prepared_event, envelope):
            ticket.notice = self._queued_notice_reservation_if_busy(target=target, envelope=envelope)
        await self._enqueue_for_dispatch(
            prepared_event,
            room,
            facts=facts,
            ticket=ticket,
            coalescing_key=requester_coalescing_key(room.room_id, coalescing_thread_id, facts.lane_requester_id),
        )
        self._mark_waiting_for_approval(room, prepared_event.source, target, envelope)
        return _IngressAdmissionOutcome.DEFERRED

    def _mark_waiting_for_approval(
        self,
        room: nio.MatrixRoom,
        source: dict[str, Any],
        target: MessageTarget,
        envelope: MessageEnvelope,
    ) -> None:
        """React ⏳ to a human message that waits because a pending approval holds its conversation."""
        if not envelope.origin.may_answer_interactive_prompt or not self.deps.response_runner.is_held_for_approval(
            target,
        ):
            return
        if self._mentions_another_participant(room, source):
            # A message for another agent or person does not wait for this one.
            return
        create_background_task(
            self.deps.delivery_gateway.send_judgment_reaction(
                turn_id=envelope.source_event_id,
                room_id=target.room_id,
                event_id=envelope.source_event_id,
                key="⏳",
                kind="approval_wait",
            ),
            name=f"approval_wait_reaction_{envelope.source_event_id}",
            owner=self.deps.runtime,
        )

    async def _enqueue_media_for_dispatch(
        self,
        *,
        room: nio.MatrixRoom,
        event: MediaDispatchEvent,
        facts: IngressFacts,
        coalescing_thread_id: str | None,
        ticket: IngressTicket,
    ) -> _IngressAdmissionOutcome:
        """Queue one media event; the gate decides busy-conversation routing."""
        target = self.deps.resolver.build_message_target(
            room_id=room.room_id,
            thread_id=coalescing_thread_id,
            reply_to_event_id=event.event_id,
            event_source=event.source,
        )
        envelope = self.deps.resolver.build_envelope(event=event, facts=facts, target=target)
        ticket.notice = self._queued_notice_reservation_if_busy(target=target, envelope=envelope)
        await self._enqueue_for_dispatch(
            event,
            room,
            facts=facts,
            ticket=ticket,
            coalescing_key=requester_coalescing_key(room.room_id, coalescing_thread_id, facts.lane_requester_id),
        )
        self._mark_waiting_for_approval(room, event.source, target, envelope)
        return _IngressAdmissionOutcome.DEFERRED

    async def _should_skip_router_before_shared_ingress_work(
        self,
        room: nio.MatrixRoom,
        event: nio.RoomMessageFormatted,
        *,
        facts: IngressFacts,
        thread_id: str | None,
    ) -> bool:
        """Return whether the router can safely skip shared ingress work for one text event."""
        requester_user_id = facts.lane_requester_id
        if (
            self.deps.agent_name != ROUTER_AGENT_NAME
            or command_for(facts, dispatch_prompt_for_event(event)) is not None
            # A long text's preview is not its text yet, so it may hide a command.
            or is_v2_sidecar_text_preview(event.source)
            # A caption for an upload still waiting in this sender's burst must join that upload's
            # turn, or the router routes the upload alone while the named agent answers the caption.
            or any(
                self.deps.coalescing_gate.has_pending_work(
                    requester_coalescing_key(room.room_id, scope_thread_id, requester_user_id),
                )
                for scope_thread_id in {None, thread_id}
            )
        ):
            return False

        mentioned_agents, _am_i_mentioned, has_non_agent_mentions = check_agent_mentioned(
            event.source,
            self.deps.matrix_id,
            self.deps.runtime.config,
            self.deps.runtime_paths,
            room=room,
        )
        if mentioned_agents or has_non_agent_mentions:
            return not is_router_only_agent_mention(
                mentioned_agents,
                has_non_agent_mentions=has_non_agent_mentions,
                config=self.deps.runtime.config,
                runtime_paths=self.deps.runtime_paths,
            )
        if thread_id is None:
            return False

        try:
            thread_history = await self.deps.resolver.dispatch_thread_snapshot(
                room.room_id,
                thread_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.deps.logger.warning(
                "Router pre-ingress skip ignored thread snapshot failure",
                room_id=room.room_id,
                thread_id=thread_id,
                error=str(exc),
            )
            return False
        if thread_history is None:
            return False
        available_responders = await self.deps.turn_policy.responder_candidates_for_room(
            room,
            requester_user_id,
        )
        return thread_requires_explicit_agent_targeting(
            thread_history,
            sender_id=requester_user_id,
            config=self.deps.runtime.config,
            runtime_paths=self.deps.runtime_paths,
            membership_index=self.deps.turn_policy.deps.agent_reply_memberships,
            available_responders_in_room=available_responders,
        )

    async def _resolve_text_event_with_ingress_timing(
        self,
        event: nio.RoomMessageFormatted,
        *,
        dispatch_timing: DispatchPipelineTiming | None,
    ) -> PreparedIngress:
        """Normalize one inbound text event while recording ingress timing boundaries."""
        if dispatch_timing is not None:
            dispatch_timing.mark("ingress_normalize_start")
        prepared_event = await self.deps.normalizer.resolve_text_event(
            TextNormalizationRequest(event=event),
        )
        if dispatch_timing is not None:
            dispatch_timing.mark("ingress_normalize_ready")
        attach_dispatch_pipeline_timing(prepared_event.source, dispatch_timing)
        return prepared_event

    async def _dispatch_prepared_text_like_ingress(
        self,
        *,
        room: nio.MatrixRoom,
        prepared_event: PreparedIngress,
        facts: IngressFacts,
        ticket: IngressTicket,
        coalescing_thread_id: str | None,
    ) -> _IngressAdmissionOutcome:
        """Run shared ingress dispatch for text events and sidecar text previews."""
        target = self.deps.resolver.build_message_target(
            room_id=room.room_id,
            thread_id=coalescing_thread_id,
            reply_to_event_id=prepared_event.event_id,
            event_source=prepared_event.source,
        )
        content = prepared_event.source.get("content") if isinstance(prepared_event.source, dict) else None
        if facts.source_kind == TRUSTED_INTERNAL_RELAY_SOURCE_KIND and is_router_voice_echo(facts, content):
            return _IngressAdmissionOutcome.CONSUMED
        envelope = self.deps.resolver.build_envelope(event=prepared_event, facts=facts, target=target)
        if self._should_skip_deep_synthetic_full_dispatch(
            event_id=prepared_event.event_id,
            envelope=envelope,
        ):
            return _IngressAdmissionOutcome.CONSUMED
        message_text = prepared_event.body.strip()
        if envelope.origin.may_answer_interactive_prompt and message_text.isdigit() and len(message_text) == 1:
            selection = await self.deps.interactive_questions.claim_interactive_text(
                source_event_id=prepared_event.event_id,
            )
            if selection is not None:
                # As a reaction's answer does, the response runs on a runner-owned task behind this conversation's
                # earlier messages, so neither its run nor an approval it waits for holds the room's event lane. The
                # queue and that task own the source from here, so a retry of it must not meet this turn's claim.
                ticket.take_claim().release()
                await self.enqueue_interactive_selection(
                    ticket,
                    room,
                    selection=selection,
                    facts=facts,
                    source_event_id=prepared_event.event_id,
                )
                return _IngressAdmissionOutcome.DEFERRED
        if command_for(facts, dispatch_prompt_for_event(prepared_event)) is not None:
            await self._dispatch_command_control_input(
                room=room,
                dispatch_event=prepared_event,
                facts=facts,
                coalescing_thread_id=coalescing_thread_id,
                ticket=ticket,
            )
            return _IngressAdmissionOutcome.CONSUMED
        return await self._enqueue_prepared_text_for_dispatch(
            room=room,
            prepared_event=prepared_event,
            facts=facts,
            envelope=envelope,
            coalescing_thread_id=coalescing_thread_id,
            ticket=ticket,
        )

    def _reply_target_event_id(self, event: PreparedIngress) -> str:
        """Return the message a response to this event answers: a finished reply's original, else the event."""
        return self.deps.ingress.entity_final_reply_original_event_id(event) or event.event_id

    def _mentioned_entity_final_reply(
        self,
        room: nio.MatrixRoom,
        event: nio.RoomMessageFormatted,
    ) -> nio.RoomMessageFormatted | None:
        """Return another entity's completed edit as its reply when that reply mentions this entity."""
        final_reply = self.deps.ingress.entity_final_reply(event)
        if final_reply is None:
            return None
        _mentioned_agents, am_i_mentioned, _has_non_agent_mentions = check_agent_mentioned(
            final_reply.source,
            self.deps.matrix_id,
            self.deps.runtime.config,
            self.deps.runtime_paths,
            room=room,
        )
        return final_reply if am_i_mentioned else None

    async def _agent_mention_may_wake(
        self,
        room: nio.MatrixRoom,
        envelope: MessageEnvelope,
        context: MessageContext,
    ) -> bool:
        """Return whether an agent-written message may wake this entity.

        The person it acts for must still be joined to the room, and agents stop waking each
        other once a conversation has the configured number of consecutive agent messages.
        """
        config = self.deps.runtime.config
        if envelope.origin.acting_sender_id is not None and not await is_requester_joined_to_room(
            self._client(),
            room,
            envelope.requester_id,
            config,
            self.deps.runtime_paths,
        ):
            self.deps.logger.info("agent_mention_ignored_requester_not_joined", requester_id=envelope.requester_id)
            return False
        limit = config.defaults.max_consecutive_agent_replies
        if await self.deps.resolver.consecutive_agent_messages(room.room_id, context.thread_id, limit=limit) >= limit:
            self.deps.logger.info("agent_mention_ignored_consecutive_agent_limit", limit=limit)
            return False
        return True

    async def _handle_edit_event(
        self,
        room: nio.MatrixRoom,
        prechecked_event: _PrecheckedEvent[nio.RoomMessageFormatted],
        event_info: EventInfo,
    ) -> bool | None:
        """Apply a person's edit to their message still waiting in a queue, or hand it to the edit regenerator.

        An entity's edit changes neither, so a queued message from an entity answers the text it was queued with.
        """
        if prechecked_event.facts.sender_is_managed:
            return None
        async with admitted_response_decision(
            self.deps.runtime.response_admission_gate,
            self.deps.response_runner.wait_for_admission_or_shutdown,
        ):
            if not self.deps.turn_policy.can_reply_to_sender_in_room(
                prechecked_event.facts.lane_requester_id,
                room.room_id,
            ):
                return None
            if await self._edit_queued_message(room, prechecked_event.event, event_info):
                return None
            return await self.deps.edit_regenerator.handle_message_edit(
                room,
                prechecked_event.event,
                event_info,
                prechecked_event.facts,
            )

    async def _edit_queued_message(
        self,
        room: nio.MatrixRoom,
        event: nio.RoomMessageFormatted,
        event_info: EventInfo,
    ) -> bool:
        """Give a message still waiting for its turn the text its sender edited it to; return whether one did."""
        original_event_id = event_info.original_event_id
        if original_event_id is None or not self.deps.coalescing_gate.has_pending_source_event(original_event_id):
            return False
        body, content = await extract_visible_edit_body(
            event.source,
            self._client(),
            config=self.deps.runtime.config,
            runtime_paths=self.deps.runtime_paths,
        )
        if body is None or content is None:
            return False
        return self.deps.coalescing_gate.apply_pending_edit(
            room_id=room.room_id,
            source_event_id=original_event_id,
            sender=event.sender,
            body=body,
            new_content=content,
            for_another_participant=self._mentions_another_participant(room, {"content": content}),
        )

    def _log_unplaceable_event(self, room: nio.MatrixRoom, event: DispatchEvent | MatrixMediaEvent) -> None:
        """Record an event left unanswered because its relation target cannot be read.

        Without that target there is no conversation to place the event in,
        and asking the homeserver again gets the same refusal, so retrying the
        turn would only hold up every later event in the room.
        """
        self.deps.logger.warning(
            "relation_target_unavailable",
            event_id=event.event_id,
            room_id=room.room_id,
            sender=event.sender,
        )

    async def _notify_command_target_not_ready(
        self,
        room: nio.MatrixRoom,
        event: nio.RoomMessageFormatted,
        *,
        facts: IngressFacts,
    ) -> bool:
        """Fail one command visibly when its conversation cannot be resolved yet."""
        if command_for(facts, dispatch_prompt_for_event(event)) is None:
            return False
        requester_user_id = facts.lane_requester_id
        self.deps.logger.warning(
            "command_target_not_ready",
            event_id=event.event_id,
            room_id=room.room_id,
            sender=event.sender,
        )
        if self.deps.agent_name == ROUTER_AGENT_NAME:
            async with admitted_response_decision(
                self.deps.runtime.response_admission_gate,
                self.deps.response_runner.wait_for_admission_or_shutdown,
            ):
                if not self.deps.turn_policy.can_reply_to_sender_in_room(requester_user_id, room.room_id):
                    return True
                target = self.deps.resolver.build_message_target(
                    room_id=room.room_id,
                    thread_id=None,
                    reply_to_event_id=event.event_id,
                    event_source=event.source,
                )
                pending_turn, response_event_id = await self.deps.visible_responses.prepare_visible_delivery_turn(
                    TurnRecord.create([event.event_id]),
                    requester_id=requester_user_id,
                    target=target,
                )
                if pending_turn is None:
                    return True
                response_event_id = await self.deps.visible_responses.deliver_recoverable_text(
                    pending_turn,
                    target=target,
                    response_text=(
                        "I could not run that command yet: the conversation it targets "
                        "is still being resolved. Please resend it in a moment."
                    ),
                    # One notice, then the turn is terminal, so this send satisfies
                    # the outbox's once-per-(turn, stage) rule.
                    recovered_response_event_id=response_event_id,
                )
                await self.deps.turn_store.record_responded_turn(
                    canonicalize_turn_record(pending_turn, response_event_id=response_event_id),
                )
                return True
        await self.deps.visible_responses.settle_source_events_ignored(TurnRecord.create([event.event_id]))
        return True

    async def _dispatch_command_control_input(
        self,
        *,
        room: nio.MatrixRoom,
        dispatch_event: PreparedIngress,
        facts: IngressFacts,
        coalescing_thread_id: str | None,
        ticket: IngressTicket,
    ) -> None:
        """Dispatch one command as a control input without entering the coalescing gate, under its ingress claim."""
        # A command an entity wrote for a human runs with that human's authority, as its conversation would;
        # handle_command refuses the commands only the human may send.
        pending_event = PendingEvent(
            event=replace(dispatch_event, payload_trusted=dispatch_event.payload_trusted or facts.sender_is_managed),
            room=room,
            facts=facts,
        )
        turn = build_prepared_turn(
            requester_coalescing_key(room.room_id, coalescing_thread_id, facts.lane_requester_id),
            [pending_event],
        )
        await dispatch_text_message(self, replace(turn, claims=(ticket.take_claim(),)))

    async def _adaptive_text_debounce_seconds(
        self,
        event: PreparedIngress,
        room: nio.MatrixRoom,
        *,
        facts: IngressFacts,
        key: CoalescingKey,
    ) -> float:
        """Resolve adaptive eligibility before queue admission without delaying ordinary text."""
        agent = self.deps.runtime.config.agents.get(self.deps.agent_name)
        participation = agent.participation if agent is not None else None
        if (
            participation is None
            or participation.debounce_seconds <= 0
            or key.thread_id is None
            or facts.source_kind != MESSAGE_SOURCE_KIND
            or not facts.origin.may_answer_interactive_prompt
        ):
            return 0.0
        result = await self.deps.resolver.extract_dispatch_context(room, event)
        selected = self.deps.turn_policy.adaptive_participation(
            context=result.context,
            room=room,
            requester_user_id=facts.lane_requester_id,
        )
        return selected.debounce_seconds if selected is not None else 0.0

    async def _enqueue_for_dispatch(
        self,
        event: DispatchEvent,
        room: nio.MatrixRoom,
        *,
        facts: IngressFacts,
        ticket: IngressTicket,
        coalescing_key: CoalescingKey,
    ) -> _IngressAdmissionOutcome:
        """Route one inbound event through the live coalescing gate."""
        dispatch_timing = get_dispatch_pipeline_timing(event.source)
        if dispatch_timing is not None:
            dispatch_timing.mark("gate_enter")
        enqueue_start = time.monotonic()
        timing_scope = event_timing_scope(event.event_id)
        source_kind = facts.source_kind
        gate_enqueue_start = time.monotonic()
        if isinstance(event, PreparedIngress):
            prepared_event = event
        elif is_matrix_media_dispatch_event(event):
            prepared_event = prepare_media_ingress(event)
        else:
            msg = f"Unsupported dispatch event: {type(event).__name__}"
            raise TypeError(msg)
        text_debounce_seconds = await self._adaptive_text_debounce_seconds(
            prepared_event,
            room,
            facts=facts,
            key=coalescing_key,
        )
        # Coalescing keys and lanes stay on the sender; the batch runs, and splits, by the effective requester.
        pending_event = PendingEvent(
            text_debounce_seconds=text_debounce_seconds,
            event=replace(
                prepared_event,
                payload_trusted=prepared_event.payload_trusted or facts.sender_is_managed,
                turn_dispatch_recovery=turn_dispatch_recovery_active(),
                for_another_participant=self._mentions_another_participant(room, prepared_event.source),
            ),
            room=room,
            facts=facts,
        )
        ticket.admit(coalescing_key, pending_event)
        emit_elapsed_timing(
            "ingress_handoff.enqueue_for_dispatch.coalescing_gate",
            gate_enqueue_start,
            source_kind=source_kind,
            timing_scope=timing_scope,
        )
        emit_elapsed_timing(
            "ingress_handoff.enqueue_for_dispatch",
            enqueue_start,
            source_kind=source_kind,
            timing_scope=timing_scope,
        )
        return _IngressAdmissionOutcome.DEFERRED

    async def _settle_unmentioned_managed_sender_before_hydration(
        self,
        *,
        room: nio.MatrixRoom,
        event: PreparedIngress,
        facts: IngressFacts,
        context_event: PreparedIngress,
        event_label: str,
        handled_turn: TurnRecord,
        ingress_metadata: DispatchIngressMetadata | None,
        payload_metadata: DispatchPayloadMetadata | None,
    ) -> bool:
        """Settle managed chatter before thread hydration can make it retryable."""
        requester_user_id = facts.run_requester_id
        origin = facts.origin
        if not origin.blocks_unmentioned_managed_sender:
            return False
        mentioned_agents, am_i_mentioned = self.deps.resolver.pre_hydration_mentions(
            room=room,
            event=event,
            payload_metadata=payload_metadata,
        )
        if am_i_mentioned or not handled_turn.sources_all_from_requester(requester_user_id):
            return False
        coalescing_key = ingress_metadata.coalescing_key if ingress_metadata is not None else None
        target = self.deps.resolver.build_message_target(
            room_id=room.room_id,
            thread_id=(
                coalescing_key.thread_id
                if coalescing_key is not None
                else EventInfo.from_event(context_event.source).thread_id
            ),
            reply_to_event_id=event.event_id,
            event_source=context_event.source,
        )
        envelope = self.deps.resolver.build_envelope(
            event=event,
            facts=facts,
            target=target,
            mentioned_agents=mentioned_agents,
            attachment_ids=payload_metadata.attachment_ids if payload_metadata is not None else None,
            active_follow_up=ingress_metadata is not None and ingress_metadata.active_follow_up,
        )
        if await self._message_received_hook_settles_turn(
            room=room,
            envelope=envelope,
            correlation_id=event.event_id,
            handled_turn=handled_turn,
        ):
            return True
        self.deps.logger.debug(
            "ignore_unmentioned_agent_event",
            agent=origin.requester_entity_name,
            event_label=event_label,
            user_id=requester_user_id,
        )
        await self.deps.visible_responses.settle_source_events_ignored(handled_turn)
        return True

    async def _message_received_hook_settles_turn(
        self,
        *,
        room: nio.MatrixRoom,
        envelope: MessageEnvelope,
        correlation_id: str,
        handled_turn: TurnRecord,
    ) -> bool:
        """Run the authorized ingress hook and settle when it blocks dispatch."""
        hooks_start = time.monotonic()
        async with admitted_response_decision(
            self.deps.runtime.response_admission_gate,
            self.deps.response_runner.wait_for_admission_or_shutdown,
        ):
            if not self.deps.turn_policy.can_reply_to_sender_in_room(envelope.requester_id, room.room_id):
                await self.deps.visible_responses.settle_source_events_ignored(handled_turn)
                return True
            suppressed = await self.deps.ingress_hook_runner.emit_message_received_hooks(
                envelope=envelope,
                correlation_id=correlation_id,
                policy=hook_ingress_policy(envelope),
            )
        emit_elapsed_timing(
            "dispatch_handoff.prepare_dispatch.emit_message_received_hooks",
            hooks_start,
            suppressed=suppressed,
        )
        if not suppressed:
            return False
        await self.deps.visible_responses.settle_source_events_ignored(handled_turn)
        return True

    async def _prepare_dispatch(
        self,
        room: nio.MatrixRoom,
        event: PreparedIngress,
        facts: IngressFacts,
        *,
        event_label: str,
        handled_turn: TurnRecord,
        ingress_metadata: DispatchIngressMetadata | None = None,
        payload_metadata: DispatchPayloadMetadata | None = None,
        use_command_context: bool = False,
        current_prompt_is_structured: bool = False,
    ) -> _DispatchPreparation | None:
        """Build the shared dispatch context for one prepared inbound turn."""
        extract_context_start = time.monotonic()
        requester_user_id = facts.run_requester_id
        coalescing_key = ingress_metadata.coalescing_key if ingress_metadata is not None else None
        context_event = (
            _room_level_context_event(event)
            if coalescing_key is not None and coalescing_key.thread_id is None
            else event
        )
        # A router relay of a human's message reads only the context the relay names.
        use_trusted_router_relay_context = (
            not use_command_context and facts.router_relay and facts.original_sender_is_human
        )
        if await self._settle_unmentioned_managed_sender_before_hydration(
            room=room,
            event=event,
            facts=facts,
            context_event=context_event,
            event_label=event_label,
            handled_turn=handled_turn,
            ingress_metadata=ingress_metadata,
            payload_metadata=payload_metadata,
        ):
            return None
        if use_command_context:
            dispatch_context_result = await self.deps.resolver.extract_dispatch_context(
                room,
                context_event,
                mode=ThreadReadMode.NONBLOCKING,
                payload_metadata=payload_metadata,
            )
            emit_elapsed_timing(
                "dispatch_handoff.prepare_dispatch.extract_context",
                extract_context_start,
                path="command",
            )
        elif use_trusted_router_relay_context:
            dispatch_context_result = await self.deps.resolver.extract_trusted_router_relay_context(
                room,
                context_event,
                payload_metadata=payload_metadata,
            )
            emit_elapsed_timing(
                "dispatch_handoff.prepare_dispatch.extract_context",
                extract_context_start,
                path="trusted_router_relay",
            )
        else:
            dispatch_context_result = await self.deps.resolver.extract_dispatch_context(
                room,
                context_event,
                payload_metadata=payload_metadata,
            )
            emit_elapsed_timing(
                "dispatch_handoff.prepare_dispatch.extract_context",
                extract_context_start,
                path="normal",
            )
        context = dispatch_context_result.context
        thread_context = dispatch_context_result.thread_context
        target_start = time.monotonic()
        if coalescing_key is not None:
            coalesced_thread_id = coalescing_key.thread_id
            if context.thread_id != coalesced_thread_id:
                context = replace(
                    context,
                    is_thread=coalesced_thread_id is not None,
                    thread_id=coalesced_thread_id,
                    thread_history=[],
                    replay_guard_history=[],
                    requires_model_history_refresh=False,
                )
            target = self.deps.resolver.build_message_target(
                room_id=room.room_id,
                thread_id=coalesced_thread_id,
                reply_to_event_id=self._reply_target_event_id(event),
                event_source=context_event.source,
            )
        else:
            target = (
                thread_context.stable_target
                if thread_context is not None
                else self.deps.resolver.build_message_target(
                    room_id=room.room_id,
                    thread_id=context.thread_id,
                    reply_to_event_id=self._reply_target_event_id(event),
                    event_source=event.source,
                )
            )
        emit_elapsed_timing(
            "dispatch_handoff.prepare_dispatch.build_message_target",
            target_start,
            resolved_thread_id=target.resolved_thread_id,
        )
        correlation_id = event.event_id
        envelope_start = time.monotonic()
        envelope = self.deps.resolver.build_envelope(
            event=event,
            facts=facts,
            target=target,
            mentioned_agents=context.mentioned_agents,
            attachment_ids=payload_metadata.attachment_ids if payload_metadata is not None else None,
            active_follow_up=ingress_metadata is not None and ingress_metadata.active_follow_up,
        )
        emit_elapsed_timing(
            "dispatch_handoff.prepare_dispatch.build_message_envelope",
            envelope_start,
            source_kind=envelope.source_kind,
        )
        if await self._message_received_hook_settles_turn(
            room=room,
            envelope=envelope,
            correlation_id=correlation_id,
            handled_turn=handled_turn,
        ):
            return None

        origin = envelope.origin
        sender_agent_name = origin.requester_entity_name
        blocks_unmentioned_managed_sender = origin.blocks_unmentioned_managed_sender
        if (
            blocks_unmentioned_managed_sender
            and not context.am_i_mentioned
            and handled_turn.sources_all_from_requester(requester_user_id)
        ):
            self.deps.logger.debug(
                "ignore_unmentioned_agent_event",
                agent=sender_agent_name,
                event_label=event_label,
                user_id=requester_user_id,
            )
            await self.deps.visible_responses.settle_source_events_ignored(handled_turn)
            return None
        if blocks_unmentioned_managed_sender and not await self._agent_mention_may_wake(room, envelope, context):
            await self.deps.visible_responses.settle_source_events_ignored(handled_turn)
            return None

        replay_guard = (
            _ReplayGuardContext(
                history=thread_context.replay_guard_history,
                degraded=thread_context.replay_guard_degraded,
                thread_id=target.resolved_thread_id or thread_context.candidate_thread_root_id,
            )
            if thread_context is not None
            else _ReplayGuardContext(
                history=context.replay_guard_history,
                degraded=False,
                thread_id=target.resolved_thread_id,
            )
        )

        return _DispatchPreparation(
            dispatch=PreparedDispatch(
                requester_user_id=requester_user_id,
                context=context,
                target=target,
                correlation_id=correlation_id,
                envelope=envelope,
                current_prompt_is_structured=current_prompt_is_structured,
                scheduled_history_budget=_scheduled_history_budget_for_dispatch(event, origin.intent),
                scheduled_model=_scheduled_model_for_dispatch(event, origin.intent),
            ),
            replay_guard=replay_guard,
        )

    def _interactive_selection_response_envelope(
        self,
        *,
        target: MessageTarget,
        facts: IngressFacts,
        source_event_id: str,
        selected_value: str,
        attachment_ids: tuple[str, ...] = (),
    ) -> MessageEnvelope:
        """Build the canonical response envelope for one interactive selection."""
        return MessageEnvelope(
            source_event_id=source_event_id,
            target=target,
            body=f"The user selected: {selected_value}",
            attachment_ids=attachment_ids,
            mentioned_agents=(),
            agent_name=self.deps.agent_name,
            origin=replace(facts, source_kind=_SELECTION_RESPONSE_SOURCE_KIND).origin,
        )

    async def _start_interactive_selection(
        self,
        response_factory: Callable[[], Awaitable[bool]],
        *,
        response_target: MessageTarget,
        source_event_id: str,
        facts: IngressFacts,
        selected_value: str,
    ) -> None:
        """Transfer one selection response from journal dispatch to runner ownership."""
        response_envelope = self._interactive_selection_response_envelope(
            target=response_target,
            facts=facts,
            source_event_id=source_event_id,
            selected_value=selected_value,
        )
        try:
            reservation = await self.deps.response_runner.reserve_response_lifecycle(response_envelope)
        except BaseException:
            self.deps.retry_dispatch_sources(response_target.room_id, (source_event_id,))
            raise

        async def run_owned_response() -> None:
            response_claim_transferred = False
            try:
                with response_lifecycle_reservation_context(reservation):
                    await reservation.wait_until_acquired()
                    response = response_factory()
                    response_claim_transferred = True
                    source_handed_off = await response
                # Terminal delivery normally settles the discovery alias in its
                # outbox transaction. This idempotent fallback also handles an
                # already-terminal replay that has no new delivery to enqueue.
                if not source_handed_off:
                    await self.deps.settle_dispatch_sources((source_event_id,))
            except BaseException:
                if not response_claim_transferred:
                    self.deps.retry_dispatch_sources(response_target.room_id, (source_event_id,))
                raise
            finally:
                await reservation.release()

        owned_response = run_owned_response()
        try:
            self.deps.response_runner.track_inbox_response(
                owned_response,
                name=f"interactive_selection_response:{source_event_id}",
                room_id=response_target.room_id,
                # No turn record proves a selection reply recoverable, so orderly
                # shutdown waits out its budget for one still running.
                recovery_proof_ready=lambda: False,
                on_failure=lambda: self.deps.retry_dispatch_sources(response_target.room_id, (source_event_id,)),
                source_event_ids=(source_event_id,),
            )
        except BaseException:
            owned_response.close()
            await reservation.release()
            self.deps.retry_dispatch_sources(response_target.room_id, (source_event_id,))
            raise
        # Ownership registration is synchronous, and the task cannot execute
        # until this callback yields after reporting its deferred handoff.

    def _interactive_selection_target(
        self,
        room_id: str,
        selection: interactive.InteractiveSelection,
    ) -> MessageTarget:
        """Return the canonical response target for one interactive selection."""
        return self.deps.resolver.build_message_target(
            room_id=room_id,
            thread_id=selection.thread_id,
            reply_to_event_id=selection.question_event_id,
        )

    async def enqueue_interactive_selection(
        self,
        ticket: IngressTicket,
        room: nio.MatrixRoom,
        *,
        selection: interactive.InteractiveSelection,
        facts: IngressFacts,
        source_event_id: str,
    ) -> None:
        """Queue a selection handoff as a FIFO barrier behind earlier ingress.

        ``facts`` classify the answering event: a digit message or a reaction.
        """
        response_target = self._interactive_selection_target(room.room_id, selection)
        facts = replace(facts, source_kind=INTERACTIVE_SELECTION_SOURCE_KIND)

        selection_dispatch = InteractiveSelectionDispatch(
            response_factory=lambda: self._handle_interactive_selection(
                room,
                selection=selection,
                facts=facts,
                source_event_id=source_event_id,
                response_target=response_target,
            ),
            response_target=response_target,
            source_event_id=source_event_id,
            facts=facts,
            selected_value=selection.selected_value,
        )
        pending_event = PendingEvent(
            event=PreparedIngress(
                sender=facts.physical_sender,
                event_id=source_event_id,
                body="",
                source={},
            ),
            room=room,
            facts=facts,
            selection=selection_dispatch,
        )
        ticket.admit(
            requester_coalescing_key(
                room.room_id,
                response_target.lifecycle_key.thread_id,
                facts.run_requester_id,
            ),
            pending_event,
        )

    async def _dispatch_interactive_selection_turn(self, turn: PreparedTurn) -> bool:
        """Start one receipt-ordered selection barrier and transfer its source to the runner."""
        dispatch = turn.selection
        if dispatch is None:
            return False
        if len(turn.handled_turn.source_event_ids) != 1:
            msg = "Interactive selection dispatch must be a single FIFO barrier"
            raise ValueError(msg)
        await self._start_interactive_selection(
            dispatch.response_factory,
            response_target=dispatch.response_target,
            source_event_id=dispatch.source_event_id,
            facts=dispatch.facts,
            selected_value=dispatch.selected_value,
        )
        return True

    async def _handle_interactive_selection(
        self,
        room: nio.MatrixRoom,
        *,
        selection: interactive.InteractiveSelection,
        facts: IngressFacts,
        source_event_id: str,
        response_target: MessageTarget | None = None,
    ) -> bool:
        """Own claim settlement around one validated interactive selection."""
        target = response_target or self._interactive_selection_target(room.room_id, selection)
        try:
            return await self._execute_interactive_selection(
                room,
                selection=selection,
                facts=facts,
                source_event_id=source_event_id,
                response_target=target,
            )
        except BaseException as error:

            async def source_is_terminal_after_cancellation() -> bool:
                return await self.deps.dispatch_source_is_terminal(source_event_id)

            source_is_terminal = await run_coroutine_until_complete(
                source_is_terminal_after_cancellation(),
            )
            if source_is_terminal:
                if isinstance(error, asyncio.CancelledError):
                    raise
                return False
            raise

    async def _execute_interactive_selection(
        self,
        room: nio.MatrixRoom,
        *,
        selection: interactive.InteractiveSelection,
        facts: IngressFacts,
        source_event_id: str,
        response_target: MessageTarget,
    ) -> bool:
        """Authorize and execute one selection after its caller transfers claim ownership."""
        async with admitted_response_decision(
            self.deps.runtime.response_admission_gate,
            self.deps.response_runner.wait_for_admission_or_shutdown,
        ):
            if not self.deps.turn_policy.can_reply_to_sender_in_room(facts.run_requester_id, room.room_id):
                await self.deps.settle_dispatch_sources((source_event_id,))
                return False
            return await self._execute_admitted_interactive_selection(
                room,
                selection=selection,
                facts=facts,
                source_event_id=source_event_id,
                response_target=response_target,
            )

    async def _execute_admitted_interactive_selection(
        self,
        room: nio.MatrixRoom,
        *,
        selection: interactive.InteractiveSelection,
        facts: IngressFacts,
        source_event_id: str,
        response_target: MessageTarget,
    ) -> bool:
        """Execute one authorized selection while replacement admission remains reserved."""
        if await self._interactive_selection_is_durably_terminal(room.room_id, source_event_id):
            return False
        thread_history = (
            await self.deps.resolver.fetch_thread_history(
                room.room_id,
                selection.thread_id,
            )
            if selection.thread_id
            else []
        )
        selection_handled_turn = self.deps.turn_store.attach_response_context(
            TurnRecord.create(
                [source_event_id],
                discovery_event_ids=(
                    (selection.question_event_id,) if source_event_id != selection.question_event_id else ()
                ),
                requester_id=facts.run_requester_id,
            ),
            history_scope=self.deps.turn_store.response_history_scope(ResponseAction(kind="individual")),
            conversation_target=response_target,
        )
        pending_turn = await self.deps.turn_store.record_pending_turn(selection_handled_turn)
        if pending_turn is None or await self.deps.turn_store.deleted_sources(pending_turn):
            await self._require_durable_interactive_selection(room.room_id, source_event_id)
            return False
        selection_handled_turn = pending_turn
        # This acknowledgement is the placeholder the selection's answer then
        # edits, which is what its `interactive_span_id` below says, so it
        # is the turn's initial delivery and not its answer. Staging it that way
        # also keeps it from settling the journal source: a placeholder
        # discharges nothing, and a crash before the model finished would
        # otherwise leave "Processing your response..." in the room with
        # nothing pending to replay.
        ack_event_id, interactive_span_id = await self.deps.visible_responses.deliver_selection_acknowledgement(
            selection_handled_turn,
            target=response_target,
            response_text=(
                f"You selected: {selection.selection_key} {selection.selected_label}\n\nProcessing your response..."
            ),
            delivery_turn_id=source_event_id,
        )
        if not ack_event_id:
            self.deps.logger.error(
                "Failed to send acknowledgment for interactive selection",
                source_event_id=source_event_id,
            )
            raise self._interactive_selection_retry_error(source_event_id)
        # A reaction or numeric answer identifies the selection but does not
        # carry the question's attachment context, so rebuild it from the
        # conversation that asked the question.
        try:
            selection_payload = await self.deps.normalizer.build_dispatch_payload_with_attachments(
                DispatchPayloadWithAttachmentsRequest(
                    room_id=room.room_id,
                    prompt=interactive.build_selection_prompt(selection),
                    current_attachment_ids=[],
                    thread_id=selection.thread_id,
                    media_thread_id=response_target.resolved_thread_id,
                    thread_history=thread_history,
                ),
            )
        except Exception as error:
            response_event_id = await self._finalize_dispatch_failure(
                target=response_target,
                error=error,
                handled_turn=selection_handled_turn,
                existing_event_id=ack_event_id,
            )
            if response_event_id is not None:
                await self._require_durable_interactive_selection(room.room_id, source_event_id)
                return False
            raise self._interactive_selection_retry_error(source_event_id) from error
        selection_attachment_ids = tuple(selection_payload.attachment_ids or ())
        selection_matrix_run_metadata = self.deps.turn_store.build_run_metadata(selection_handled_turn)
        response_envelope = self._interactive_selection_response_envelope(
            target=response_target,
            facts=facts,
            source_event_id=source_event_id,
            selected_value=selection.selected_value,
            attachment_ids=selection_attachment_ids,
        )

        source_handoff = asyncio.Event()
        await self.deps.response_runner.generate_response(
            ResponseRequest(
                prompt=selection_payload.prompt,
                model_prompt=selection_payload.model_prompt,
                thread_history=thread_history,
                sources=ResponseSources(
                    pending_event_ids=tuple(
                        dict.fromkeys((source_event_id, *selection_handled_turn.source_event_ids)),
                    ),
                    logical_source_event_ids=selection_handled_turn.source_event_ids,
                    discovery_event_ids=selection_handled_turn.discovery_event_ids,
                ),
                history_boundary_event_id=source_event_id,
                member_display_names=room_member_display_names(room),
                existing_event_id=ack_event_id,
                interactive_span_id=interactive_span_id,
                user_id=facts.run_requester_id,
                attachment_ids=selection_attachment_ids or None,
                response_envelope=response_envelope,
                matrix_run_metadata=selection_matrix_run_metadata,
                prepare_source_turn=lambda history: self.deps.turn_store.prepare_pending_response_source(
                    target=response_target,
                    source_event_ids=selection_handled_turn.indexed_event_ids,
                    terminal_source_event_ids=selection_handled_turn.source_event_ids,
                    thread_history=history,
                ),
                source_handoff=source_handoff,
            ),
        )
        if source_handoff.is_set():
            return True
        await self._require_durable_interactive_selection(room.room_id, source_event_id)
        return False

    async def _interactive_selection_is_durably_terminal(self, room_id: str, source_event_id: str) -> bool:
        """Return whether the exact selection source is durably terminal.

        Two facts on purpose: the turn's rows are the agent's, the journal row
        this login's, and either can say yes without the other.
        """
        return await self.deps.turn_store.is_handled(
            room_id,
            source_event_id,
        ) or await self.deps.dispatch_source_is_terminal(source_event_id)

    async def _require_durable_interactive_selection(self, room_id: str, source_event_id: str) -> None:
        """Fail retryably until one selection source reaches durable terminal truth."""
        if await self._interactive_selection_is_durably_terminal(room_id, source_event_id):
            return
        raise self._interactive_selection_retry_error(source_event_id)

    @staticmethod
    def _interactive_selection_retry_error(source_event_id: str) -> RuntimeError:
        """Return the shared retry signal for a selection without terminal truth."""
        return RuntimeError(f"Interactive selection {source_event_id} has no durable terminal outcome")

    async def _execute_router_relay(
        self,
        room: nio.MatrixRoom,
        event: DispatchEvent,
        thread_history: Sequence[ResolvedVisibleMessage],
        thread_id: str | None = None,
        message: str | None = None,
        *,
        requester_user_id: str,
        extra_content: dict[str, Any] | None = None,
        media_events: list[MediaDispatchEvent] | None = None,
        handled_turn: TurnRecord | None = None,
        scheduled_prompt: str | None = None,
    ) -> None:
        """Run one explicit router relay through the router-relay executor."""
        await execute_router_relay(
            self.deps,
            room,
            event,
            thread_history,
            thread_id,
            message,
            requester_user_id=requester_user_id,
            extra_content=extra_content,
            media_events=media_events,
            handled_turn=handled_turn,
            scheduled_prompt=scheduled_prompt,
        )

    async def _router_handled_turn_outcome(
        self,
        room_id: str,
        handled_turn: TurnRecord,
    ) -> TurnRecord | None:
        """Return the turn an ignored router turn records answered: by the visible echo it published, if any.

        The router takes its echo as its answer here deliberately; publishing an
        echo never records one by itself.
        """
        echoes = await self.deps.turn_store.finalized_visible_echoes(handled_turn.source_event_ids)
        if not echoes:
            return None
        handled = await self.deps.turn_store.handled_among(room_id, handled_turn.source_event_ids)
        if handled.issuperset(handled_turn.source_event_ids):
            return None
        return canonicalize_turn_record(handled_turn, response_event_id=echoes[0])

    async def _finalize_dispatch_failure(
        self,
        *,
        target: MessageTarget,
        error: Exception,
        handled_turn: TurnRecord,
        existing_event_id: str | None = None,
    ) -> str | None:
        """Convert dispatch setup failures into a visible terminal message.

        A reply that owns ``existing_event_id`` shows the failure through its
        records, which settle the journal sources, record the turn answered,
        and own the notice until it is delivered. Otherwise nothing durable owns
        the notice, so it is sent directly and the turn records it as its answer.
        """
        error_text = get_user_friendly_error_message(
            error,
            self.deps.agent_name,
            runtime_paths=self.deps.runtime_paths,
        )
        if existing_event_id is not None and await self.deps.delivery_gateway.fail_reply_dispatch(
            existing_event_id,
            error_text,
        ):
            return existing_event_id
        response_event_id = await self.deps.delivery_gateway.send_text(
            SendTextRequest(
                target=target,
                response_text=error_text,
                extra_content={STREAM_STATUS_KEY: STREAM_STATUS_ERROR},
            ),
        )
        if response_event_id is not None:
            await self.deps.turn_store.record_responded_turn(
                canonicalize_turn_record(handled_turn, response_event_id=response_event_id),
                room_id=target.room_id,
            )
        return response_event_id

    async def _execute_response_action(  # noqa: C901, PLR0915
        self,
        room: nio.MatrixRoom,
        event: DispatchEvent,
        dispatch: PreparedDispatch,
        action: ResponseAction,
        payload_inputs: DispatchPayloadInputs,
        *,
        processing_log: str,
        dispatch_started_at: float,
        handled_turn: TurnRecord,
        matrix_run_metadata: dict[str, Any] | None = None,
        on_lifecycle_lock_acquired: Callable[[], None] | None = None,
        reconcile_visible_response: bool = False,
    ) -> None:
        """Execute one final response path for a prepared dispatch action."""
        if room.room_id != dispatch.target.room_id:
            msg = "Prepared dispatch target room does not match the Matrix room"
            raise ValueError(msg)
        action = self.deps.turn_policy.effective_response_action(action)
        dispatch_timing = get_dispatch_pipeline_timing(event.source)
        if dispatch_timing is not None:
            dispatch_timing.note(response_action_kind=action.kind)

        with bound_log_context(
            agent_id=self.deps.agent_name,
            requester_id=dispatch.requester_user_id,
            room_id=dispatch.target.room_id,
            thread_id=dispatch.target.resolved_thread_id,
            session_id=dispatch.target.session_id,
            reply_to_event_id=event.event_id,
            correlation_id=dispatch.correlation_id,
        ):
            if action.kind == "reject":
                assert action.rejection_message is not None
                response_event_id = (
                    await self.deps.visible_responses.recovered_response_event_id(
                        handled_turn,
                        room_id=dispatch.target.room_id,
                    )
                    if reconcile_visible_response
                    else None
                )
                response_event_id = await self.deps.visible_responses.deliver_recoverable_text(
                    handled_turn,
                    target=dispatch.target,
                    response_text=action.rejection_message,
                    # One rejection, then the turn is terminal, so this send
                    # satisfies the outbox's once-per-(turn, stage) rule.
                    recovered_response_event_id=response_event_id,
                )
                await self.deps.turn_store.record_responded_turn(
                    canonicalize_turn_record(handled_turn, response_event_id=response_event_id),
                )
                if dispatch_timing is not None and response_event_id is not None:
                    dispatch_timing.mark_first_visible_reply("final", substantive=True)
                    dispatch_timing.mark("response_complete")
                    dispatch_timing.emit_summary(self.deps.logger, outcome="reject")
                return

            if not dispatch.context.am_i_mentioned:
                self.deps.logger.info("Will respond: only agent in thread")

            target_member_names: tuple[str, ...] | None = None
            if action.kind == "team":
                assert action.form_team is not None
                assert action.form_team.mode is not None
                target_member_names = action.form_team.eligible_member_names

            context_ready_monotonic = time.monotonic()

            if dispatch_timing is not None and isinstance(dispatch.context.thread_history, ThreadHistoryResult):
                dispatch_timing.note(**dispatch.context.thread_history.diagnostics)

            self.deps.logger.info(processing_log, event_id=event.event_id)
            current_timestamp_ms = normalize_timestamp_ms(event.server_timestamp)
            payload_preparation = ResponsePayloadPreparation(
                dispatch=dispatch,
                prompt=event.body,
                action_kind=action.kind,
                payload_inputs=payload_inputs,
                target_member_names=target_member_names,
                dispatch_started_at=dispatch_started_at,
                context_ready_monotonic=context_ready_monotonic,
            )

            team_mode: TeamMode | None = None
            if action.kind == "team":
                assert action.form_team is not None
                assert action.form_team.mode is not None
                team_mode = action.form_team.mode
                if action.form_team.intent is not TeamIntent.CONFIGURED_TEAM and event.body:
                    team_mode = await select_ad_hoc_team_mode(
                        event.body,
                        action.form_team.eligible_member_names,
                        self.deps.runtime.config,
                        self.deps.runtime_paths,
                    )

            async def settle_redacted_sources() -> None:
                await self.deps.visible_responses.settle_source_events_ignored(handled_turn)

            async def answer_surviving_sources(preparation: SourcePreparation) -> None:
                # The rest answer as their own turn once the response task lets their claim go.
                await self.deps.settle_dispatch_sources(preparation.invalid)
                self.deps.turn_store.retry_when_claim_releases(dispatch.target.room_id, preparation.surviving)

            async def record_no_response() -> None:
                await self.deps.turn_store.record_turn(handled_turn)
                await self.deps.visible_responses.settle_source_events_ignored(handled_turn)

            try:
                response_request = ResponseRequest(
                    thread_history=dispatch.context.thread_history,
                    member_display_names=room_member_display_names(room),
                    prompt=event.body,
                    sources=ResponseSources(
                        pending_event_ids=tuple(
                            dict.fromkeys((event.event_id, *handled_turn.source_event_ids)),
                        ),
                        logical_source_event_ids=handled_turn.source_event_ids,
                        discovery_event_ids=handled_turn.discovery_event_ids,
                    ),
                    user_id=dispatch.requester_user_id,
                    # A replay finds the reply its earlier attempt left through the reply's records.
                    response_envelope=dispatch.envelope,
                    correlation_id=dispatch.correlation_id,
                    matrix_run_metadata=matrix_run_metadata,
                    requires_model_history_refresh=dispatch.context.requires_model_history_refresh,
                    scheduled_history_budget=dispatch.scheduled_history_budget,
                    scheduled_model=dispatch.scheduled_model,
                    participation=action.participation,
                    payload_preparation=payload_preparation,
                    current_timestamp_ms=current_timestamp_ms,
                    current_prompt_is_structured=dispatch.current_prompt_is_structured,
                    pipeline_timing=dispatch_timing,
                    on_lifecycle_lock_acquired=on_lifecycle_lock_acquired,
                    prepare_source_turn=lambda history: self.deps.turn_store.prepare_pending_response_source(
                        target=dispatch.target,
                        source_event_ids=handled_turn.indexed_event_ids,
                        terminal_source_event_ids=handled_turn.source_event_ids,
                        thread_history=history,
                    ),
                    on_source_turn_suppressed=settle_redacted_sources,
                    on_sources_invalidated=answer_surviving_sources,
                    on_no_response_handled=record_no_response,
                )
                # The reply's records settle the sources and record the turn answered.
                if action.kind == "team":
                    assert action.form_team is not None
                    assert team_mode is not None
                    await self.deps.response_runner.generate_team_response_helper(
                        response_request,
                        member_names=action.form_team.eligible_member_names,
                        team_mode=team_mode.value,
                    )
                else:
                    await self.deps.response_runner.generate_response(response_request)
            except PostLockRequestPreparationError:
                # The reply's records settled the sources, recorded the turn answered, and owe the notice.
                return

    async def handle_prepared_turn(self, turn: PreparedTurn) -> None:
        """Dispatch one logical turn emitted directly by the coalescing gate."""
        if await self._dispatch_interactive_selection_turn(turn):
            return
        coalescing_key = turn.ingress.coalescing_key
        assert coalescing_key is not None
        for notice in turn.notices:
            notice.release()
        if is_active_follow_up_coalescing_key(coalescing_key):
            # Follow-ups held back by a requester split waited on the response
            # that just finished; left pending, they would cut this turn short
            # and hand its unfinished work to the next requester's turn.
            for pending_event in self.deps.coalescing_gate.queued_pending_events(coalescing_key):
                if pending_event.notice is not None:
                    pending_event.notice.release()
        timing_scope = event_timing_scope(turn.event.event_id)
        dispatch_timing = get_dispatch_pipeline_timing(turn.event.source)
        if dispatch_timing is not None:
            dispatch_timing.mark("gate_exit")
        async with self.deps.resolver.turn_lookup_scope():
            dispatch_start = time.monotonic()
            await dispatch_text_message(self, turn)
            emit_elapsed_timing(
                "coalescing.handle_turn.dispatch_text_message",
                dispatch_start,
                source_event_count=len(turn.handled_turn.source_event_ids),
                timing_scope=timing_scope,
            )

    async def handle_text_event(
        self,
        room: nio.MatrixRoom,
        event: nio.RoomMessageFormatted,
        *,
        receipt_time: float | None = None,
        ticket: IngressTicket | None = None,
    ) -> TurnDispatchOutcome:
        """Handle one inbound text event, through a ticket the caller reserved early or one of its own."""
        async with self.deps.resolver.turn_lookup_scope():
            return await self._handle_message_inner(
                room,
                event,
                receipt_time=receipt_time,
                ticket=ticket,
            )

    async def _handle_message_inner(
        self,
        room: nio.MatrixRoom,
        event: nio.RoomMessageFormatted,
        *,
        receipt_time: float | None = None,
        ticket: IngressTicket | None = None,
    ) -> TurnDispatchOutcome:
        """Handle one text message inside the per-turn conversation lookup scope."""
        event_info = EventInfo.from_event(event.source)
        event_content = event.source.get("content") if isinstance(event.source, dict) else None
        is_nonterminal_stream = isinstance(event_content, dict) and is_unfinished_reply(event_content)
        # A placeholder still streaming names no requester, so refusing it here would mark the turn its finished
        # reply needs as handled.
        if not isinstance(event.body, str) or is_nonterminal_stream:
            return TurnDispatchOutcome.INTENTIONALLY_IGNORED
        # Another entity's finished reply arrives as an edit of its placeholder; its mentions dispatch like a message.
        final_reply = self._mentioned_entity_final_reply(room, event) if event_info.is_edit else None
        if final_reply is not None:
            event = final_reply
        is_edit = event_info.is_edit and final_reply is None
        prechecked_event = await self._precheck_dispatch_event(
            room,
            event,
            source_kind=EDIT_SOURCE_KIND if is_edit else None,
            is_edit=is_edit,
        )
        if prechecked_event is None:
            return TurnDispatchOutcome.INTENTIONALLY_IGNORED
        await ensure_room_membership_synced(
            self._client(),
            room,
            sender_id=prechecked_event.facts.lane_requester_id,
        )

        dispatch_timing = create_dispatch_pipeline_timing(
            event_id=event.event_id,
            room_id=room.room_id,
        )
        attach_dispatch_pipeline_timing(event.source, dispatch_timing)
        if ticket is None:
            ticket = self.reserve_prompt_ingress_order(
                room,
                prechecked_event.facts.lane_requester_id,
                receipt_time=receipt_time,
            )
        try:
            if is_edit:
                ticket.close()
                handed_off = await self._handle_edit_event(room, prechecked_event, event_info)
                return TurnDispatchOutcome.DEFERRED if handed_off is True else TurnDispatchOutcome.INTENTIONALLY_IGNORED
            discovery_alias = prechecked_event.facts.discovery_event_id
            claimed = await ticket.claim_turn(
                TurnRecord.create(
                    [event.event_id],
                    discovery_event_ids=(discovery_alias,) if discovery_alias else (),
                    completed=False,
                ),
            )
            if claimed is not None:
                return claimed
            outcome = await self._ingest_live_text_event(
                room,
                prechecked_event,
                dispatch_timing=dispatch_timing,
                ticket=ticket,
            )
            return (
                TurnDispatchOutcome.DEFERRED
                if outcome is _IngressAdmissionOutcome.DEFERRED
                else TurnDispatchOutcome.INTENTIONALLY_IGNORED
            )
        finally:
            ticket.close()

    async def _ingest_live_text_event(
        self,
        room: nio.MatrixRoom,
        prechecked_event: _PrecheckedEvent[nio.RoomMessageFormatted],
        *,
        dispatch_timing: DispatchPipelineTiming | None,
        ticket: IngressTicket,
    ) -> _IngressAdmissionOutcome:
        """Resolve, normalize, and admit one live (non-edit) text event."""
        event = prechecked_event.event
        facts = prechecked_event.facts
        try:
            ingress_thread_id = await self.deps.resolver.coalescing_thread_id(room, event)
        except ThreadMembershipLookupError as exc:
            if await self._notify_command_target_not_ready(room, event, facts=facts):
                return _IngressAdmissionOutcome.CONSUMED
            if isinstance(exc, RelatedEventUnavailableError):
                self._log_unplaceable_event(room, event)
                return _IngressAdmissionOutcome.IGNORED
            raise
        if await self._should_skip_router_before_shared_ingress_work(
            room,
            event,
            facts=facts,
            thread_id=ingress_thread_id,
        ):
            self.deps.logger.debug(
                "skip_router_shared_ingress_work",
                event_id=event.event_id,
                room_id=room.room_id,
                thread_id=ingress_thread_id,
            )
            return _IngressAdmissionOutcome.CONSUMED

        self.deps.logger.info(
            "Received message",
            event_id=event.event_id,
            room_id=room.room_id,
            sender=event.sender,
            thread_id=ingress_thread_id,
        )
        prepared_event = await self._resolve_text_event_with_ingress_timing(
            event,
            dispatch_timing=dispatch_timing,
        )
        return await self._dispatch_prepared_text_like_ingress(
            room=room,
            prepared_event=prepared_event,
            facts=facts,
            ticket=ticket,
            coalescing_thread_id=ingress_thread_id,
        )

    async def handle_media_event(
        self,
        room: nio.MatrixRoom,
        event: MatrixMediaEvent,
        *,
        receipt_time: float | None = None,
    ) -> TurnDispatchOutcome:
        """Handle one inbound media event."""
        async with self.deps.resolver.turn_lookup_scope():
            return await self._handle_media_message_inner(room, event, receipt_time=receipt_time)

    async def _handle_media_message_inner(
        self,
        room: nio.MatrixRoom,
        event: MatrixMediaEvent,
        *,
        receipt_time: float | None = None,
    ) -> TurnDispatchOutcome:
        """Handle one media event inside the per-turn conversation lookup scope."""
        prechecked_event = await self._precheck_dispatch_event(room, event, source_kind=_media_source_kind(event))
        if prechecked_event is None:
            return TurnDispatchOutcome.INTENTIONALLY_IGNORED
        dispatch_timing = create_dispatch_pipeline_timing(
            event_id=prechecked_event.event.event_id,
            room_id=room.room_id,
        )
        attach_dispatch_pipeline_timing(prechecked_event.event.source, dispatch_timing)
        if is_audio_message_event(prechecked_event.event) and prechecked_event.facts.sender_is_managed:
            self.deps.logger.debug(
                "Ignoring agent audio event for voice transcription",
                event_id=prechecked_event.event.event_id,
                sender=prechecked_event.event.sender,
            )
            return TurnDispatchOutcome.INTENTIONALLY_IGNORED
        await ensure_room_membership_synced(
            self._client(),
            room,
            sender_id=prechecked_event.facts.lane_requester_id,
        )
        ticket = self.reserve_prompt_ingress_order(
            room,
            prechecked_event.facts.lane_requester_id,
            receipt_time=receipt_time,
        )
        try:
            claimed = await ticket.claim_turn(TurnRecord.create([prechecked_event.event.event_id], completed=False))
            if claimed is not None:
                return claimed
            if is_audio_message_event(prechecked_event.event):
                dispatch_outcome = await self._on_audio_media_message(
                    room,
                    _PrecheckedEvent(event=prechecked_event.event, facts=prechecked_event.facts),
                    dispatch_timing=dispatch_timing,
                    ticket=ticket,
                )
            else:
                try:
                    coalescing_thread_id = await self.deps.resolver.coalescing_thread_id(room, prechecked_event.event)
                except RelatedEventUnavailableError:
                    self._log_unplaceable_event(room, prechecked_event.event)
                    return TurnDispatchOutcome.INTENTIONALLY_IGNORED
                admission_outcome = await self._dispatch_special_media_as_text(
                    room,
                    prechecked_event,
                    ticket=ticket,
                    coalescing_thread_id=coalescing_thread_id,
                )
                if admission_outcome is _IngressAdmissionOutcome.DEFERRED:
                    dispatch_outcome = TurnDispatchOutcome.DEFERRED
                elif admission_outcome is _IngressAdmissionOutcome.CONSUMED or not is_matrix_media_dispatch_event(
                    prechecked_event.event,
                ):
                    dispatch_outcome = TurnDispatchOutcome.INTENTIONALLY_IGNORED
                else:
                    await self._enqueue_media_for_dispatch(
                        room=room,
                        event=prechecked_event.event,
                        facts=prechecked_event.facts,
                        coalescing_thread_id=coalescing_thread_id,
                        ticket=ticket,
                    )
                    dispatch_outcome = TurnDispatchOutcome.DEFERRED
        finally:
            ticket.close()
        return dispatch_outcome

    async def _dispatch_special_media_as_text(
        self,
        room: nio.MatrixRoom,
        prechecked_event: _PrecheckedInboundMediaEvent,
        *,
        ticket: IngressTicket,
        coalescing_thread_id: str | None,
    ) -> _IngressAdmissionOutcome:
        """Handle media events that normalize into the text dispatch pipeline."""
        event = prechecked_event.event
        if is_file_message_event(event):
            return await self._dispatch_file_sidecar_text_preview(
                room,
                _PrecheckedEvent(event=event, facts=prechecked_event.facts),
                ticket=ticket,
                coalescing_thread_id=coalescing_thread_id,
            )
        return _IngressAdmissionOutcome.IGNORED

    async def _on_audio_media_message(
        self,
        room: nio.MatrixRoom,
        prechecked_event: _PrecheckedEvent[AudioMessageEvent],
        *,
        dispatch_timing: DispatchPipelineTiming | None,
        ticket: IngressTicket,
    ) -> TurnDispatchOutcome:
        """Resolve the audio conversation key once, then defer voice normalization."""
        event = prechecked_event.event

        readiness = VoiceReadiness(
            normalizer=self.deps.normalizer,
            turn_store=self.deps.turn_store,
            visible_echo=self.deps.visible_voice_echo,
            logger=self.deps.logger,
        )
        prepared = await readiness.prepared_source(event.event_id)
        try:
            coalescing_thread_id = (
                prepared.coalescing_thread_id
                if prepared is not None
                else await self.deps.resolver.coalescing_thread_id(room, event)
            )
        except RelatedEventUnavailableError:
            self._log_unplaceable_event(room, event)
            return TurnDispatchOutcome.INTENTIONALLY_IGNORED
        voice_target = self.deps.resolver.build_message_target(
            room_id=room.room_id,
            thread_id=coalescing_thread_id,
            reply_to_event_id=event.event_id,
            event_source=event.source,
        )
        if prepared is not None:
            voice_target = voice_target.with_thread_root(prepared.thread_id)
        admission_key = requester_coalescing_key(
            room.room_id,
            coalescing_thread_id,
            prechecked_event.facts.lane_requester_id,
        )

        ready_task = asyncio.create_task(
            self._ready_voice_event(
                room=room,
                prechecked_event=prechecked_event,
                voice_target=voice_target,
                readiness=readiness,
                coalescing_thread_id=coalescing_thread_id,
                dispatch_timing=dispatch_timing,
            ),
            name=f"voice_ready:{room.room_id}:{event.event_id}",
        )
        ticket.admit_ready(admission_key, ready_task, source_event_id=event.event_id, source_kind=VOICE_SOURCE_KIND)
        return TurnDispatchOutcome.DEFERRED

    async def _ready_voice_event(
        self,
        *,
        room: nio.MatrixRoom,
        prechecked_event: _PrecheckedEvent[AudioMessageEvent],
        voice_target: MessageTarget,
        readiness: VoiceReadiness,
        coalescing_thread_id: str | None,
        dispatch_timing: DispatchPipelineTiming | None,
    ) -> ReadyPendingEvent:
        """Normalize a raw voice event after its conversation key is fixed; its lane holds the turn claim."""
        event = prechecked_event.event
        facts = prechecked_event.facts
        queued_notice_reservation = None
        reservation_released_or_handed_off = False
        try:
            envelope = self.deps.resolver.build_envelope(
                event=cast("DispatchEvent", event),
                facts=facts,
                target=voice_target,
            )
            queued_notice_reservation = self._queued_notice_reservation_if_busy(
                target=voice_target,
                envelope=envelope,
            )
            normalized_event = await readiness.prepare(
                room=room,
                event=event,
                target=voice_target,
                coalescing_thread_id=coalescing_thread_id,
                requester_user_id=facts.lane_requester_id,
                dispatch_timing=dispatch_timing,
            )
            normalized_target = voice_target
            envelope = self.deps.resolver.build_envelope(event=normalized_event, facts=facts, target=normalized_target)
            queued_notice_reservation = self._voice_queued_notice_reservation(
                preliminary_target=voice_target,
                target=normalized_target,
                envelope=envelope,
                queued_notice_reservation=queued_notice_reservation,
            )
            ready = ReadyPendingEvent(
                pending_event=PendingEvent(
                    event=replace(
                        normalized_event,
                        turn_dispatch_recovery=turn_dispatch_recovery_active(),
                        for_another_participant=self._mentions_another_participant(room, normalized_event.source),
                    ),
                    room=room,
                    facts=facts,
                    notice=queued_notice_reservation,
                ),
            )
        except IngressRetryError:
            raise
        except Exception as exc:
            self.deps.logger.exception(
                "Voice dispatch metadata failed; deferring this voice turn",
                event_id=event.event_id,
                room_id=room.room_id,
            )
            raise IngressRetryError from exc
        else:
            reservation_released_or_handed_off = True
            self._mark_waiting_for_approval(room, event.source, normalized_target, envelope)
            return ready
        finally:
            if not reservation_released_or_handed_off and queued_notice_reservation is not None:
                queued_notice_reservation.release()

    async def _dispatch_file_sidecar_text_preview(
        self,
        room: nio.MatrixRoom,
        prechecked_event: _PrecheckedEvent[FileMessageEvent],
        *,
        ticket: IngressTicket,
        coalescing_thread_id: str | None,
    ) -> _IngressAdmissionOutcome:
        """Dispatch one sidecar-backed file preview through the normal text pipeline."""
        event = prechecked_event.event
        if not is_v2_sidecar_text_preview(event.source):
            return _IngressAdmissionOutcome.IGNORED

        dispatch_timing = get_dispatch_pipeline_timing(event.source)
        if dispatch_timing is not None:
            dispatch_timing.mark("ingress_normalize_start")
        prepared_text_event = await self.deps.normalizer.prepare_file_sidecar_text_event(event)
        if dispatch_timing is not None:
            dispatch_timing.mark("ingress_normalize_ready")
        assert prepared_text_event is not None
        attach_dispatch_pipeline_timing(prepared_text_event.source, dispatch_timing)
        return await self._dispatch_prepared_text_like_ingress(
            room=room,
            prepared_event=prepared_text_event,
            facts=prechecked_event.facts,
            ticket=ticket,
            coalescing_thread_id=coalescing_thread_id,
        )
