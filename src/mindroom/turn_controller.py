"""Control one inbound turn from ingress to recorded outcome."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, replace
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol, cast

from mindroom import interactive
from mindroom.attachments import parse_attachment_ids_from_event_source
from mindroom.coalescing import CoalescingGate, ReadyPendingEvent
from mindroom.coalescing_batch import (
    CoalescingKey,
    PendingEvent,
    PreparedTurn,
    build_prepared_turn,
    requester_coalescing_key,
)
from mindroom.coalescing_cleanup import close_pending_event_metadata_once
from mindroom.commands.parsing import command_parser
from mindroom.constants import (
    ATTACHMENT_IDS_KEY,
    ORIGINAL_SENDER_KEY,
    ROUTER_AGENT_NAME,
    SOURCE_KIND_KEY,
    STREAM_STATUS_COMPLETED,
    STREAM_STATUS_KEY,
    STREAM_STATUS_PENDING,
    STREAM_STATUS_STREAMING,
    VOICE_PREFIX,
    VOICE_RAW_AUDIO_FALLBACK_KEY,
    RuntimePaths,
)
from mindroom.delivery_gateway import EditTextRequest, SendTextRequest
from mindroom.dispatch_callback_outcome import TurnDispatchOutcome
from mindroom.dispatch_handoff import (
    DispatchEvent,
    DispatchIngressMetadata,
    DispatchPayloadMetadata,
    MediaDispatchEvent,
    PendingDispatchMetadata,
    PreparedIngress,
    payload_metadata_from_source,
    prepare_media_ingress,
)
from mindroom.dispatch_recovery_context import turn_dispatch_recovery_active
from mindroom.dispatch_replay_guard import (
    has_newer_unresponded_in_thread,
    has_newer_unresponded_journal_thread_event,
)
from mindroom.dispatch_source import (
    IMAGE_SOURCE_KIND,
    INTERACTIVE_SELECTION_SOURCE_KIND,
    MEDIA_SOURCE_KIND,
    MESSAGE_SOURCE_KIND,
    TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
    VOICE_SOURCE_KIND,
    ScheduledHistoryBudget,
    scheduled_history_limit_from_content,
    source_kind_allows_internal_relay_detection,
)
from mindroom.entity_resolution import entity_identity_registry
from mindroom.error_handling import get_user_friendly_error_message
from mindroom.handled_turns import TurnRecord
from mindroom.hooks import MessageEnvelope, hook_ingress_policy
from mindroom.inbound_turn_normalizer import (
    DispatchPayloadWithAttachmentsRequest,
    InboundTurnNormalizer,
    TextNormalizationRequest,
    VoiceNormalizationRequest,
)
from mindroom.ingress_lanes import ReceiptLaneKey
from mindroom.logging_config import bound_log_context
from mindroom.matrix.conversation_reads import ThreadReadMode
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.media import (
    AudioMessageEvent,
    FileMessageEvent,
    MatrixMediaEvent,
    extract_media_caption,
    is_audio_message_event,
    is_file_message_event,
    is_image_message_event,
    is_matrix_media_dispatch_event,
)
from mindroom.matrix.message_content import is_v2_sidecar_text_preview
from mindroom.matrix.thread_history_result import ThreadHistoryResult
from mindroom.matrix.thread_membership import ThreadMembershipLookupError
from mindroom.prompt_ingress_reservation import PromptIngressReservationOwner as _PromptIngressReservationOwner
from mindroom.response_lifecycle import response_lifecycle_reservation_context
from mindroom.response_payload_preparation import (
    DispatchPayloadInputs,
    ResponsePayloadPreparation,
)
from mindroom.response_runner import PostLockRequestPreparationError, ResponseRequest
from mindroom.router_relay import execute_router_relay
from mindroom.teams import TeamIntent, TeamMode, select_ad_hoc_team_mode
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
from mindroom.turn_origin import (
    TurnIntent,
    classify_turn_origin,
)
from mindroom.turn_policy import IngressHookRunner, PreparedDispatch, ResponseAction, TurnPolicy
from mindroom.turn_record import canonicalize_turn_record
from mindroom.turn_store import record_deferred_outcome_response, record_user_stop_terminal
from mindroom.visible_voice_echo import VisibleVoiceEchoRequest

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    import nio
    import structlog

    from mindroom.bot_runtime_view import BotRuntimeView
    from mindroom.command_turn_executor import CommandTurnExecutor
    from mindroom.conversation_resolver import ConversationResolver
    from mindroom.delivery_gateway import DeliveryGateway
    from mindroom.event_journal import PendingTurnView
    from mindroom.ingress_validation import IngressValidator
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.matrix.identity import MatrixID
    from mindroom.matrix.relation_lookup import RelationLookup
    from mindroom.message_target import MessageTarget, ResponseLifecycleKey
    from mindroom.response_lifecycle import QueuedHumanNoticeReservation
    from mindroom.response_runner import ResponseRunner
    from mindroom.sync_restart_retry import InterruptedTurnRooms
    from mindroom.tool_system.runtime_context import ToolRuntimeSupport
    from mindroom.turn_store import TurnStore
    from mindroom.visible_response_reconciliation import VisibleResponseReconciler
    from mindroom.visible_voice_echo import VisibleVoiceEchoLifecycle

_QUEUED_NOTICE_METADATA_KIND = "queued_notice_reservation"
_PENDING_TURN_CLAIM_METADATA_KIND = "pending_turn_claim"
_INTERACTIVE_SELECTION_METADATA_KIND = "interactive_selection"


@dataclass(frozen=True)
class _InteractiveSelectionDispatch:
    """Deferred selection work carried through receipt-ordered coalescing."""

    response_factory: Callable[[], Awaitable[None]]
    start_response: Callable[..., Awaitable[None]]
    restore_selection: Callable[[], None]
    response_target: MessageTarget
    source_event_id: str
    user_id: str
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


def _scheduled_history_budget_for_dispatch(
    event: DispatchEvent,
    origin_intent: TurnIntent,
) -> ScheduledHistoryBudget | None:
    """Return the trusted history budget and prompt source for one scheduled dispatch."""
    if origin_intent not in {TurnIntent.SCHEDULED_FIRE, TurnIntent.ROUTER_HANDOFF}:
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


def _queued_notice_dispatch_metadata(
    reservation: QueuedHumanNoticeReservation | None,
    target: MessageTarget | None,
) -> tuple[PendingDispatchMetadata, ...]:
    if reservation is None:
        return ()
    if target is None:
        msg = "Queued notice dispatch metadata requires a response target"
        raise ValueError(msg)
    return (
        PendingDispatchMetadata(
            kind=_QUEUED_NOTICE_METADATA_KIND,
            payload=reservation,
            close=reservation.cancel,
            target_key=target.lifecycle_key,
        ),
    )


def _consume_queued_notice_reservations_from_metadata(
    dispatch_metadata: tuple[PendingDispatchMetadata, ...],
    *,
    target_key: ResponseLifecycleKey,
) -> None:
    reservation_items = [item for item in dispatch_metadata if item.kind == _QUEUED_NOTICE_METADATA_KIND]
    for item in reservation_items:
        reservation = cast("QueuedHumanNoticeReservation", item.payload)
        item.finish_once(reservation.consume if item.target_key == target_key else reservation.cancel)


def _raw_voice_fallback_event(event: AudioMessageEvent, *, thread_id: str | None) -> PreparedIngress:
    """Return a dispatchable fallback when voice normalization itself fails."""
    body = f"{VOICE_PREFIX}{extract_media_caption(event, default='[Attached voice message]')}"
    source = dict(event.source) if isinstance(event.source, dict) else {}
    source_content = source.get("content")
    original_content = source_content if isinstance(source_content, dict) else {}
    content: dict[str, Any] = {
        "msgtype": "m.text",
        "body": body,
        ORIGINAL_SENDER_KEY: event.sender,
        SOURCE_KIND_KEY: VOICE_SOURCE_KIND,
        VOICE_RAW_AUDIO_FALLBACK_KEY: True,
    }
    inherited_mentions = original_content.get("m.mentions")
    if isinstance(inherited_mentions, dict):
        content["m.mentions"] = inherited_mentions
    attachment_ids = parse_attachment_ids_from_event_source(source)
    if attachment_ids:
        content[ATTACHMENT_IDS_KEY] = attachment_ids
    inherited_relation = original_content.get("m.relates_to")
    if isinstance(inherited_relation, dict):
        content["m.relates_to"] = inherited_relation
    if thread_id is not None:
        content["m.relates_to"] = {"rel_type": "m.thread", "event_id": thread_id}
    source["content"] = content
    return PreparedIngress(
        sender=event.sender,
        event_id=event.event_id,
        body=body,
        source=source,
        server_timestamp=event.server_timestamp if isinstance(event.server_timestamp, int) else None,
        source_kind_override=VOICE_SOURCE_KIND,
    )


class _EditRegenerator(Protocol):
    """Minimal edit-regeneration surface needed by turn sequencing."""

    async def handle_message_edit(
        self,
        room: nio.MatrixRoom,
        event: nio.RoomMessageFormatted,
        event_info: EventInfo,
        requester_user_id: str,
    ) -> None:
        """Regenerate the owned response for one edited user turn."""


@dataclass(frozen=True)
class _PrecheckedEvent[T]:
    """A raw or prepared event that already passed ingress prechecks."""

    event: T
    requester_user_id: str


type _PrecheckedInboundMediaEvent = _PrecheckedEvent[MatrixMediaEvent]


class _IngressAdmissionOutcome(Enum):
    ADMITTED = "admitted"
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
class _ReadyVoiceFallback:
    """Fallback event plus its ready ingress wrapper."""

    event: PreparedIngress
    ready: ReadyPendingEvent


@dataclass(frozen=True)
class TurnControllerDeps:
    """Collaborators needed for turn control, policy, and execution."""

    runtime: BotRuntimeView
    logger: structlog.stdlib.BoundLogger
    runtime_paths: RuntimePaths
    agent_name: str
    matrix_id: MatrixID
    relations: RelationLookup
    pending_turns: PendingTurnView
    resolver: ConversationResolver
    normalizer: InboundTurnNormalizer
    command_executor: CommandTurnExecutor
    turn_policy: TurnPolicy
    ingress_hook_runner: IngressHookRunner
    response_runner: ResponseRunner
    delivery_gateway: DeliveryGateway
    tool_runtime: ToolRuntimeSupport
    turn_store: TurnStore
    coalescing_gate: CoalescingGate
    edit_regenerator: _EditRegenerator
    ingress: IngressValidator
    interrupted_turn_rooms: InterruptedTurnRooms
    visible_voice_echo: VisibleVoiceEchoLifecycle
    visible_responses: VisibleResponseReconciler
    retry_dispatch_sources: Callable[[tuple[str, ...]], None]
    settle_dispatch_sources: Callable[[tuple[str, ...]], Awaitable[None]]


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
    ) -> _PromptIngressReservationOwner:
        """Reserve receipt order for one prompt-like Matrix ingress item."""
        return _PromptIngressReservationOwner(
            gate=self.deps.coalescing_gate,
            slot=self.deps.coalescing_gate.enter_lane(
                ReceiptLaneKey(room_id=room.room_id, sender_id=requester_user_id),
                receipt_time=receipt_time,
            ),
        )

    async def _precheck_dispatch_event[T: DispatchEvent | MatrixMediaEvent](
        self,
        room: nio.MatrixRoom,
        event: T,
        *,
        is_edit: bool = False,
    ) -> _PrecheckedEvent[T] | None:
        """Return a typed prechecked event for turn dispatch."""
        requester_user_id = await self.deps.ingress.precheck_event(room, event, is_edit=is_edit)
        if requester_user_id is None:
            return None
        return _PrecheckedEvent(event=event, requester_user_id=requester_user_id)

    def _has_newer_unresponded_in_thread(
        self,
        event: PreparedIngress,
        requester_user_id: str,
        thread_history: Sequence[ResolvedVisibleMessage],
        *,
        may_be_superseded_by_newer_requester_turn: bool,
    ) -> bool:
        """Return True when a newer unresponded message from the same requester exists."""
        return has_newer_unresponded_in_thread(
            event,
            requester_user_id,
            thread_history,
            may_be_superseded_by_newer_requester_turn=may_be_superseded_by_newer_requester_turn,
            requester_user_id_for_event=lambda sender, source: self.deps.ingress.requester_user_id(
                sender=sender,
                source=source,
            ),
            is_visible_router_voice_echo=self.deps.ingress.is_trusted_router_visible_voice_echo_content,
            sender_is_trusted_for_ingress_metadata=self.deps.ingress.sender_is_trusted_for_ingress_metadata,
            is_handled=self.deps.turn_store.is_handled,
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
            requester_user_id_for_event=lambda sender, source: self.deps.ingress.requester_user_id(
                sender=sender,
                source=source,
            ),
            is_visible_router_voice_echo=self.deps.ingress.is_trusted_router_visible_voice_echo_content,
            sender_is_trusted_for_ingress_metadata=self.deps.ingress.sender_is_trusted_for_ingress_metadata,
            is_handled=self.deps.turn_store.is_handled,
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
        existing: QueuedHumanNoticeReservation | None = None,
    ) -> QueuedHumanNoticeReservation | None:
        """Reserve the mid-turn queued notice when this conversation has a running response."""
        if existing is not None:
            return existing
        if not envelope.origin.may_answer_interactive_prompt:
            return None
        if not self.deps.response_runner.has_active_response_for_target(target):
            return None
        return self.deps.response_runner.reserve_waiting_human_message(
            target=target,
            response_envelope=envelope,
        )

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
            queued_notice_reservation.cancel()
            queued_notice_reservation = None
        busy = self.deps.response_runner.has_active_response_for_target(target)
        if queued_notice_reservation is not None:
            if busy:
                return queued_notice_reservation
            queued_notice_reservation.cancel()
            return None
        if not busy:
            return None
        return self._queued_notice_reservation_if_busy(target=target, envelope=envelope)

    async def _enqueue_prepared_text_for_dispatch(
        self,
        *,
        room: nio.MatrixRoom,
        prepared_event: PreparedIngress,
        envelope: MessageEnvelope,
        coalescing_thread_id: str | None,
        requester_user_id: str,
        reservation_owner: _PromptIngressReservationOwner,
        trust_internal_payload_metadata: bool | None = None,
        queued_notice_reservation: QueuedHumanNoticeReservation | None = None,
    ) -> _IngressAdmissionOutcome:
        """Queue one normalized text event; the gate decides busy-conversation routing."""
        target = self.deps.resolver.build_message_target(
            room_id=room.room_id,
            thread_id=coalescing_thread_id,
            reply_to_event_id=prepared_event.event_id,
            event_source=prepared_event.source,
        )
        queued_notice_reservation = self._queued_notice_reservation_if_busy(
            target=target,
            envelope=envelope,
            existing=queued_notice_reservation,
        )
        try:
            await self._enqueue_for_dispatch(
                prepared_event,
                room,
                source_kind=envelope.source_kind,
                dispatch_policy_source_kind=envelope.dispatch_policy_source_kind,
                hook_source=envelope.hook_source,
                message_received_depth=envelope.message_received_depth,
                requester_user_id=requester_user_id,
                reservation_owner=reservation_owner,
                coalescing_key=requester_coalescing_key(room.room_id, coalescing_thread_id, requester_user_id),
                queued_notice_reservation=queued_notice_reservation,
                queued_notice_target=target,
                trust_internal_payload_metadata=trust_internal_payload_metadata,
            )
        except asyncio.CancelledError:
            if queued_notice_reservation is not None:
                queued_notice_reservation.cancel()
            raise
        except Exception:
            if queued_notice_reservation is not None:
                queued_notice_reservation.cancel()
            raise
        else:
            return _IngressAdmissionOutcome.ADMITTED

    async def _enqueue_media_for_dispatch(
        self,
        *,
        room: nio.MatrixRoom,
        event: MediaDispatchEvent,
        coalescing_thread_id: str | None,
        requester_user_id: str,
        reservation_owner: _PromptIngressReservationOwner,
    ) -> _IngressAdmissionOutcome:
        """Queue one media event; the gate decides busy-conversation routing."""
        source_kind = IMAGE_SOURCE_KIND if is_image_message_event(event) else MEDIA_SOURCE_KIND
        target = self.deps.resolver.build_message_target(
            room_id=room.room_id,
            thread_id=coalescing_thread_id,
            reply_to_event_id=event.event_id,
            event_source=event.source,
        )
        envelope = self.deps.resolver.build_ingress_envelope(
            event=event,
            requester_user_id=requester_user_id,
            target=target,
            source_kind=source_kind,
        )
        queued_notice_reservation = self._queued_notice_reservation_if_busy(target=target, envelope=envelope)
        try:
            await self._enqueue_for_dispatch(
                event,
                room,
                source_kind=envelope.source_kind,
                requester_user_id=requester_user_id,
                reservation_owner=reservation_owner,
                coalescing_key=requester_coalescing_key(room.room_id, coalescing_thread_id, requester_user_id),
                queued_notice_reservation=queued_notice_reservation,
                queued_notice_target=target,
            )
        except asyncio.CancelledError:
            if queued_notice_reservation is not None:
                queued_notice_reservation.cancel()
            raise
        except Exception:
            if queued_notice_reservation is not None:
                queued_notice_reservation.cancel()
            raise
        else:
            return _IngressAdmissionOutcome.ADMITTED

    async def _should_skip_router_before_shared_ingress_work(
        self,
        room: nio.MatrixRoom,
        event: nio.RoomMessageFormatted,
        *,
        requester_user_id: str,
        thread_id: str | None,
    ) -> bool:
        """Return whether the router can safely skip shared ingress work for one text event."""
        if (
            self.deps.agent_name != ROUTER_AGENT_NAME
            or command_parser.parse(event.body.strip()) is not None
            or is_v2_sidecar_text_preview(event.source)
        ):
            return False

        mentioned_agents, _am_i_mentioned, has_non_agent_mentions = check_agent_mentioned(
            event.source,
            self.deps.matrix_id,
            self.deps.runtime.config,
            self.deps.runtime_paths,
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
            available_responders_in_room=available_responders,
        )

    async def _coalescing_key_for_event(
        self,
        room: nio.MatrixRoom,
        event: DispatchEvent,
        requester_user_id: str,
    ) -> CoalescingKey:
        """Return the canonical sender/thread scope for one event."""
        coalescing_thread_id = await self.deps.resolver.coalescing_thread_id(room, event)
        return requester_coalescing_key(room.room_id, coalescing_thread_id, requester_user_id)

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
        requester_user_id: str,
        reservation_owner: _PromptIngressReservationOwner,
        coalescing_thread_id: str | None,
    ) -> _IngressAdmissionOutcome:
        """Run shared ingress dispatch for text events and sidecar text previews."""
        target = self.deps.resolver.build_message_target(
            room_id=room.room_id,
            thread_id=coalescing_thread_id,
            reply_to_event_id=prepared_event.event_id,
            event_source=prepared_event.source,
        )
        canonical_thread_id = target.resolved_thread_id
        original_sender = self.deps.ingress.trusted_human_original_sender_for_event(prepared_event)
        content = prepared_event.source.get("content") if isinstance(prepared_event.source, dict) else None
        prepared_source_kind = (
            self.deps.ingress.event_source_kind(prepared_event, content) if isinstance(content, dict) else None
        )
        if self.deps.ingress.is_display_only_router_voice_echo(prepared_event):
            return _IngressAdmissionOutcome.CONSUMED
        trusted_user_relay = original_sender is not None and prepared_source_kind in {
            TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
            VOICE_SOURCE_KIND,
        }
        envelope = self.deps.resolver.build_ingress_envelope(
            event=prepared_event,
            requester_user_id=requester_user_id,
            target=target,
            original_sender=original_sender,
            trusted_user_relay=trusted_user_relay,
        )
        if self._should_skip_deep_synthetic_full_dispatch(
            event_id=prepared_event.event_id,
            envelope=envelope,
        ):
            return _IngressAdmissionOutcome.CONSUMED
        if envelope.origin.may_answer_interactive_prompt:
            selection = await interactive.handle_text_response(
                self._client(),
                room,
                prepared_event,
                self.deps.agent_name,
                resolved_thread_id=canonical_thread_id,
            )
            if selection is not None:
                # A consumed interactive answer never enters the gate, and its
                # response may wait behind this conversation's active turn; the
                # sender's lane slot must settle now, not at response completion.
                await reservation_owner.release()
                await self.handle_interactive_selection(
                    room,
                    selection=selection,
                    user_id=envelope.requester_id,
                    source_event_id=prepared_event.event_id,
                )
                return _IngressAdmissionOutcome.CONSUMED
        if self.deps.ingress.command_control_input(prepared_event, source_kind=envelope.source_kind) is not None:
            if (turn_claim := reservation_owner.pending_turn_claim) is not None:
                self.deps.turn_store.release_pending_turn_claim(turn_claim)
                reservation_owner.pending_turn_claim = None
            await self._dispatch_command_control_input(
                room=room,
                dispatch_event=prepared_event,
                envelope=envelope,
                coalescing_thread_id=coalescing_thread_id,
                requester_user_id=requester_user_id,
            )
            return _IngressAdmissionOutcome.CONSUMED
        return await self._enqueue_prepared_text_for_dispatch(
            room=room,
            prepared_event=prepared_event,
            envelope=envelope,
            coalescing_thread_id=coalescing_thread_id,
            requester_user_id=requester_user_id,
            reservation_owner=reservation_owner,
        )

    async def _handle_edit_event(
        self,
        room: nio.MatrixRoom,
        prechecked_event: _PrecheckedEvent[nio.RoomMessageFormatted],
        event_info: EventInfo,
    ) -> None:
        """Hand one edited user turn to the edit regenerator."""
        await self.deps.edit_regenerator.handle_message_edit(
            room,
            prechecked_event.event,
            event_info,
            prechecked_event.requester_user_id,
        )

    async def _notify_command_target_not_ready(
        self,
        room: nio.MatrixRoom,
        event: nio.RoomMessageFormatted,
    ) -> bool:
        """Fail one command visibly when its conversation cannot be resolved yet."""
        if command_parser.parse(event.body.strip()) is None:
            return False
        self.deps.logger.warning(
            "command_target_not_ready",
            event_id=event.event_id,
            room_id=room.room_id,
            sender=event.sender,
        )
        if self.deps.agent_name == ROUTER_AGENT_NAME:
            target = self.deps.resolver.build_message_target(
                room_id=room.room_id,
                thread_id=None,
                reply_to_event_id=event.event_id,
                event_source=event.source,
            )
            pending_turn, response_event_id = await self.deps.visible_responses.prepare_visible_delivery_turn(
                TurnRecord.create([event.event_id]),
                requester_id=event.sender,
                correlation_id=event.event_id,
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
        envelope: MessageEnvelope,
        coalescing_thread_id: str | None,
        requester_user_id: str,
    ) -> None:
        """Dispatch one command as a control input without entering the coalescing gate."""
        pending_event = PendingEvent(
            event=replace(
                dispatch_event,
                requester_user_id=requester_user_id,
                source_kind=envelope.source_kind,
                dispatch_policy_source_kind=envelope.dispatch_policy_source_kind,
                hook_source=envelope.hook_source,
                message_received_depth=envelope.message_received_depth,
                trust_internal_payload_metadata=self.deps.ingress.should_trust_internal_payload_metadata(
                    dispatch_event,
                ),
            ),
            room=room,
        )
        turn = build_prepared_turn(
            requester_coalescing_key(room.room_id, coalescing_thread_id, requester_user_id),
            [pending_event],
        )
        await dispatch_text_message(self, turn)

    async def _enqueue_for_dispatch(
        self,
        event: DispatchEvent,
        room: nio.MatrixRoom,
        *,
        source_kind: str,
        requester_user_id: str,
        reservation_owner: _PromptIngressReservationOwner,
        dispatch_policy_source_kind: str | None = None,
        hook_source: str | None = None,
        message_received_depth: int = 0,
        coalescing_key: CoalescingKey | None = None,
        queued_notice_reservation: QueuedHumanNoticeReservation | None = None,
        queued_notice_target: MessageTarget | None = None,
        trust_internal_payload_metadata: bool | None = None,
    ) -> _IngressAdmissionOutcome:
        """Route one inbound event through the live coalescing gate."""
        dispatch_timing = get_dispatch_pipeline_timing(event.source)
        if dispatch_timing is not None:
            dispatch_timing.mark("gate_enter")
        enqueue_start = time.monotonic()
        timing_scope = event_timing_scope(event.event_id)
        if source_kind_allows_internal_relay_detection(
            source_kind,
        ) and self.deps.ingress.is_trusted_internal_relay_event(
            event,
        ):
            if dispatch_timing is not None:
                dispatch_timing.note(coalescing_bypassed=True, coalescing_bypass_reason="trusted_internal_relay")
            source_kind = TRUSTED_INTERNAL_RELAY_SOURCE_KIND
        resolved_trust_internal_payload_metadata = (
            self.deps.ingress.should_trust_internal_payload_metadata(event)
            if trust_internal_payload_metadata is None
            else trust_internal_payload_metadata
        )
        coalescing_key_start = time.monotonic()
        resolved_key = coalescing_key or await self._coalescing_key_for_event(room, event, requester_user_id)
        emit_elapsed_timing(
            "ingress_handoff.enqueue_for_dispatch.coalescing_key",
            coalescing_key_start,
            thread_id=resolved_key.thread_id,
            timing_scope=timing_scope,
        )
        gate_enqueue_start = time.monotonic()
        dispatch_metadata = _queued_notice_dispatch_metadata(queued_notice_reservation, queued_notice_target)
        if (turn_claim := reservation_owner.pending_turn_claim) is not None:
            dispatch_metadata += (
                PendingDispatchMetadata(
                    kind=_PENDING_TURN_CLAIM_METADATA_KIND,
                    payload=turn_claim,
                    close=lambda: self.deps.turn_store.release_pending_turn_claim(turn_claim),
                ),
            )
        if isinstance(event, PreparedIngress):
            prepared_event = event
        elif is_matrix_media_dispatch_event(event):
            prepared_event = prepare_media_ingress(event)
        else:
            msg = f"Unsupported dispatch event: {type(event).__name__}"
            raise TypeError(msg)
        pending_event = PendingEvent(
            event=replace(
                prepared_event,
                requester_user_id=requester_user_id,
                source_kind=source_kind,
                dispatch_policy_source_kind=dispatch_policy_source_kind,
                hook_source=hook_source,
                message_received_depth=message_received_depth,
                trust_internal_payload_metadata=resolved_trust_internal_payload_metadata,
                discovery_event_id=self.deps.ingress.router_relay_original_event_id(event),
                turn_dispatch_recovery=turn_dispatch_recovery_active(),
            ),
            room=room,
            dispatch_metadata=dispatch_metadata,
        )
        if turn_claim is not None:
            reservation_owner.pending_turn_claim = None
        await reservation_owner.admit(
            resolved_key,
            source_event_id=event.event_id,
            source_kind=source_kind,
            ready_result=ReadyPendingEvent(pending_event=pending_event),
        )
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
        return _IngressAdmissionOutcome.ADMITTED

    async def _prepare_dispatch(
        self,
        room: nio.MatrixRoom,
        event: PreparedIngress,
        requester_user_id: str,
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
        use_trusted_router_relay_context = False
        coalescing_key = ingress_metadata.coalescing_key if ingress_metadata is not None else None
        context_event = (
            _room_level_context_event(event)
            if coalescing_key is not None and coalescing_key.thread_id is None
            else event
        )
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
        elif use_trusted_router_relay_context := self.deps.ingress.should_use_trusted_router_relay_context(
            event,
            ingress_metadata=ingress_metadata,
            payload_metadata=payload_metadata,
        ):
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
                reply_to_event_id=event.event_id,
                event_source=context_event.source,
            )
        else:
            target = (
                thread_context.stable_target
                if thread_context is not None
                else self.deps.resolver.build_message_target(
                    room_id=room.room_id,
                    thread_id=context.thread_id,
                    reply_to_event_id=event.event_id,
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
        original_sender = payload_metadata.original_sender if payload_metadata is not None else None
        if original_sender is None and use_trusted_router_relay_context:
            original_sender = payload_metadata_from_source(
                event.source,
                trust_internal_metadata=True,
            ).original_sender
        envelope = self.deps.resolver.build_message_envelope(
            event=event,
            requester_user_id=requester_user_id,
            context=context,
            target=target,
            attachment_ids=list(payload_metadata.attachment_ids)
            if payload_metadata is not None and payload_metadata.attachment_ids is not None
            else None,
            source_kind=ingress_metadata.source_kind if ingress_metadata is not None else None,
            dispatch_policy_source_kind=(
                ingress_metadata.dispatch_policy_source_kind if ingress_metadata is not None else None
            ),
            hook_source=ingress_metadata.hook_source if ingress_metadata is not None else None,
            message_received_depth=(ingress_metadata.message_received_depth if ingress_metadata is not None else None),
            original_sender=original_sender,
            trusted_user_relay=use_trusted_router_relay_context,
        )
        emit_elapsed_timing(
            "dispatch_handoff.prepare_dispatch.build_message_envelope",
            envelope_start,
            source_kind=envelope.source_kind,
        )
        ingress_policy = hook_ingress_policy(envelope)
        hooks_start = time.monotonic()
        suppressed = await self.deps.ingress_hook_runner.emit_message_received_hooks(
            envelope=envelope,
            correlation_id=correlation_id,
            policy=ingress_policy,
        )
        emit_elapsed_timing(
            "dispatch_handoff.prepare_dispatch.emit_message_received_hooks",
            hooks_start,
            suppressed=suppressed,
        )
        if suppressed:
            await self.deps.visible_responses.settle_source_events_ignored(handled_turn)
            return None

        origin = envelope.origin
        sender_agent_name = origin.requester_entity_name
        blocks_unmentioned_managed_sender = origin.blocks_unmentioned_managed_sender
        if blocks_unmentioned_managed_sender and not context.am_i_mentioned:
            self.deps.logger.debug(
                "ignore_unmentioned_agent_event",
                agent=sender_agent_name,
                event_label=event_label,
                user_id=requester_user_id,
            )
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
            ),
            replay_guard=replay_guard,
        )

    def _interactive_selection_response_envelope(
        self,
        *,
        target: MessageTarget,
        user_id: str,
        source_event_id: str,
        selected_value: str,
        attachment_ids: tuple[str, ...] = (),
    ) -> MessageEnvelope:
        """Build the canonical response envelope for one interactive selection."""
        registry = entity_identity_registry(self.deps.runtime.config, self.deps.runtime_paths)
        return MessageEnvelope(
            source_event_id=source_event_id,
            target=target,
            body=f"The user selected: {selected_value}",
            attachment_ids=attachment_ids,
            mentioned_agents=(),
            agent_name=self.deps.agent_name,
            origin=classify_turn_origin(
                transport_sender_id=user_id,
                requester_id=user_id,
                sender_entity_name=registry.current_entity_name_for_user_id(user_id),
                requester_entity_name=registry.current_entity_name_for_user_id(user_id),
                source_kind=MESSAGE_SOURCE_KIND,
                original_sender=None,
                trusted_user_relay=False,
            ),
        )

    async def start_interactive_selection(
        self,
        response_factory: Callable[[], Awaitable[None]],
        *,
        response_target: MessageTarget,
        source_event_id: str,
        user_id: str,
        selected_value: str,
        on_failure: Callable[[], None],
    ) -> None:
        """Transfer one selection response from journal dispatch to runner ownership."""
        response_envelope = self._interactive_selection_response_envelope(
            target=response_target,
            user_id=user_id,
            source_event_id=source_event_id,
            selected_value=selected_value,
        )
        try:
            reservation = await self.deps.response_runner.reserve_response_lifecycle(response_envelope)
        except BaseException:
            on_failure()
            raise

        async def run_owned_response() -> None:
            response_claim_transferred = False
            try:
                with response_lifecycle_reservation_context(reservation):
                    await reservation.wait_until_acquired()
                    response = response_factory()
                    response_claim_transferred = True
                    await response
                # Terminal delivery normally settles the discovery alias in its
                # outbox transaction. This idempotent fallback also handles an
                # already-terminal replay that has no new delivery to enqueue.
                await self.deps.settle_dispatch_sources((source_event_id,))
            except BaseException:
                if not response_claim_transferred:
                    on_failure()
                raise
            finally:
                await reservation.release()

        owned_response = run_owned_response()
        try:
            self.deps.response_runner.track_inbox_response(
                owned_response,
                name=f"interactive_selection_response:{source_event_id}",
                recovery_proof_ready=lambda: (
                    response_target.source_thread_id is not None
                    and self.deps.interrupted_turn_rooms.contains(source_event_id)
                ),
                on_failure=lambda: self.deps.retry_dispatch_sources((source_event_id,)),
                source_event_ids=(source_event_id,),
            )
        except BaseException:
            owned_response.close()
            await reservation.release()
            on_failure()
            raise
        # Ownership registration is synchronous, and the task cannot execute
        # until this callback yields after reporting its deferred handoff.

    async def enqueue_interactive_selection(
        self,
        reservation_owner: _PromptIngressReservationOwner,
        room: nio.MatrixRoom,
        *,
        selection: interactive.InteractiveSelection,
        requester_user_id: str,
        user_id: str,
        source_event_id: str,
        response_target: MessageTarget,
        handle_interactive_selection: Callable[..., Awaitable[None]],
        start_interactive_selection: Callable[..., Awaitable[None]],
    ) -> None:
        """Queue a selection handoff as a FIFO barrier behind earlier ingress."""

        def restore_selection() -> None:
            interactive.restore_selection(selection)

        dispatch = _InteractiveSelectionDispatch(
            response_factory=lambda: handle_interactive_selection(
                room,
                selection=selection,
                user_id=user_id,
                source_event_id=source_event_id,
                response_target=response_target,
            ),
            start_response=start_interactive_selection,
            restore_selection=restore_selection,
            response_target=response_target,
            source_event_id=source_event_id,
            user_id=user_id,
            selected_value=selection.selected_value,
        )
        pending_event = PendingEvent(
            event=PreparedIngress(
                sender=user_id,
                event_id=source_event_id,
                body="",
                source={},
                source_kind_override=INTERACTIVE_SELECTION_SOURCE_KIND,
                requester_user_id=requester_user_id,
                source_kind=INTERACTIVE_SELECTION_SOURCE_KIND,
            ),
            room=room,
            dispatch_metadata=(
                PendingDispatchMetadata(
                    kind=_INTERACTIVE_SELECTION_METADATA_KIND,
                    payload=dispatch,
                    close=restore_selection,
                    target_key=response_target.lifecycle_key,
                ),
            ),
        )
        await reservation_owner.admit(
            requester_coalescing_key(
                room.room_id,
                response_target.lifecycle_key.thread_id,
                requester_user_id,
            ),
            source_event_id=source_event_id,
            source_kind=INTERACTIVE_SELECTION_SOURCE_KIND,
            ready_result=ReadyPendingEvent(pending_event=pending_event),
        )

    async def _dispatch_interactive_selection_turn(self, turn: PreparedTurn) -> bool:
        """Start one receipt-ordered selection barrier and transfer its claim."""
        items = [item for item in turn.dispatch_metadata if item.kind == _INTERACTIVE_SELECTION_METADATA_KIND]
        if not items:
            return False
        if len(items) != 1 or len(turn.handled_turn.source_event_ids) != 1:
            msg = "Interactive selection dispatch must be a single FIFO barrier"
            raise ValueError(msg)
        item = items[0]
        dispatch = cast("_InteractiveSelectionDispatch", item.payload)
        await dispatch.start_response(
            dispatch.response_factory,
            response_target=dispatch.response_target,
            source_event_id=dispatch.source_event_id,
            user_id=dispatch.user_id,
            selected_value=dispatch.selected_value,
            on_failure=dispatch.restore_selection,
        )
        item.finish_once(lambda: None)
        return True

    async def handle_interactive_selection(
        self,
        room: nio.MatrixRoom,
        *,
        selection: interactive.InteractiveSelection,
        user_id: str,
        source_event_id: str,
        response_target: MessageTarget | None = None,
    ) -> None:
        """Own claim settlement around one validated interactive selection."""
        target = response_target or self.deps.resolver.build_message_target(
            room_id=room.room_id,
            thread_id=selection.thread_id,
            reply_to_event_id=selection.question_event_id,
        )
        try:
            await self._execute_interactive_selection(
                room,
                selection=selection,
                user_id=user_id,
                source_event_id=source_event_id,
                response_target=target,
            )
            interactive.commit_selection(selection)
        except BaseException:
            interactive.restore_selection(selection)
            raise

    async def _execute_interactive_selection(
        self,
        room: nio.MatrixRoom,
        *,
        selection: interactive.InteractiveSelection,
        user_id: str,
        source_event_id: str,
        response_target: MessageTarget,
    ) -> None:
        """Execute one selection after its caller transfers claim ownership."""
        if await self._interactive_selection_is_durably_terminal(
            selection.question_event_id,
            source_event_id,
        ):
            return
        reconcile_visible_response = self.deps.turn_store.has_pending_response_intent(
            (selection.question_event_id,),
        )
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
                [selection.question_event_id],
                discovery_event_ids=((source_event_id,) if source_event_id != selection.question_event_id else ()),
                requester_id=user_id,
                correlation_id=selection.question_event_id,
            ),
            history_scope=self.deps.turn_store.response_history_scope(ResponseAction(kind="individual")),
            conversation_target=response_target,
        )
        pending_turn = await self.deps.turn_store.record_pending_turn(selection_handled_turn)
        if pending_turn is None:
            await self._require_durable_interactive_selection(
                selection.question_event_id,
                source_event_id,
            )
            return
        if pending_turn.completed or pending_turn.redacted_source_event_ids:
            await self._require_durable_interactive_selection(
                selection.question_event_id,
                source_event_id,
            )
            return
        selection_handled_turn = pending_turn
        ack_event_id = (
            await self.deps.visible_responses.recovered_response_event_id(
                selection_handled_turn,
                room_id=room.room_id,
            )
            if reconcile_visible_response
            else None
        )
        ack_event_id = await self.deps.visible_responses.deliver_recoverable_text(
            selection_handled_turn,
            target=response_target,
            response_text=(
                f"You selected: {selection.selection_key} {selection.selected_value}\n\nProcessing your response..."
            ),
            recovered_response_event_id=ack_event_id,
            # This acknowledgement is the placeholder the selection's answer
            # then edits, which is what `existing_event_is_placeholder` below
            # says, so it is the turn's initial delivery and not its answer.
            # Staging it that way also keeps it from settling the journal
            # source: a placeholder discharges nothing, and a crash before the
            # model finished would otherwise leave "Processing your
            # response..." in the room with nothing pending to replay.
            as_placeholder=True,
        )
        if not ack_event_id:
            self.deps.logger.error(
                "Failed to send acknowledgment for interactive selection",
                source_event_id=selection.question_event_id,
            )
            raise self._interactive_selection_retry_error(source_event_id)
        selection_handled_turn = canonicalize_turn_record(selection_handled_turn, response_event_id=ack_event_id)
        # The selection is a synthetic turn with no Matrix message of its own, so
        # the attachment context that ingress normally resolves per message must
        # be rebuilt here from the conversation that asked the question.
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
                existing_event_id=ack_event_id,
                on_visible_response=lambda event_id: self.deps.visible_responses.record_pending_visible_response(
                    selection_handled_turn,
                    event_id,
                ),
                delivery_turn_id=selection_handled_turn.anchor_event_id,
            )
            if response_event_id is not None:
                await self.deps.turn_store.record_responded_turn(
                    canonicalize_turn_record(selection_handled_turn, response_event_id=response_event_id),
                )
                await self._require_durable_interactive_selection(selection.question_event_id, source_event_id)
                return
            raise self._interactive_selection_retry_error(source_event_id) from error
        selection_attachment_ids = tuple(selection_payload.attachment_ids or ())
        selection_matrix_run_metadata = self.deps.turn_store.build_run_metadata(selection_handled_turn)
        response_envelope = self._interactive_selection_response_envelope(
            target=response_target,
            user_id=user_id,
            source_event_id=source_event_id,
            selected_value=selection.selected_value,
            attachment_ids=selection_attachment_ids,
        )

        record_interrupted_turn, record_deferred_outcome, record_user_stop = self._build_response_settlement_callbacks(
            room,
            source_event_id=source_event_id,
            handled_turn=selection_handled_turn,
        )

        response_event_id = await self.deps.response_runner.generate_response(
            ResponseRequest(
                prompt=selection_payload.prompt,
                model_prompt=selection_payload.model_prompt,
                thread_history=thread_history,
                existing_event_id=ack_event_id,
                existing_event_is_placeholder=True,
                user_id=user_id,
                attachment_ids=selection_attachment_ids or None,
                response_envelope=response_envelope,
                matrix_run_metadata=selection_matrix_run_metadata,
                prepare_source_turn=lambda: self.deps.turn_store.prepare_pending_response_source(
                    target=response_target,
                    source_event_ids=selection_handled_turn.indexed_event_ids,
                    terminal_source_event_ids=selection_handled_turn.source_event_ids,
                ),
                on_interrupted_response_recoverable=record_interrupted_turn,
                on_deferred_outcome_handled=record_deferred_outcome,
                on_user_stop_handled=record_user_stop,
            ),
        )
        if response_event_id is not None:
            await self.deps.turn_store.record_responded_turn(
                canonicalize_turn_record(selection_handled_turn, response_event_id=response_event_id),
            )
            await self._require_durable_interactive_selection(selection.question_event_id, source_event_id)
            return
        await self._require_durable_interactive_selection(
            selection.question_event_id,
            source_event_id,
        )

    async def _interactive_selection_is_durably_terminal(
        self,
        question_event_id: str,
        source_event_id: str,
    ) -> bool:
        """Return whether the question or exact selection source is durably terminal."""
        return any(self.deps.turn_store.is_handled(event_id) for event_id in {question_event_id, source_event_id})

    async def _require_durable_interactive_selection(
        self,
        question_event_id: str,
        source_event_id: str,
    ) -> None:
        """Fail retryably until one selected question reaches durable terminal truth."""
        if await self._interactive_selection_is_durably_terminal(question_event_id, source_event_id):
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

    def _router_handled_turn_outcome(
        self,
        handled_turn: TurnRecord,
    ) -> TurnRecord | None:
        """Return the terminal handled-turn outcome for one ignored router turn."""
        visible_router_echo_event_id = self.deps.turn_store.finalized_visible_echo_for_sources(
            handled_turn.source_event_ids,
        )
        if visible_router_echo_event_id is None:
            return None
        if all(self.deps.turn_store.is_handled(source_event_id) for source_event_id in handled_turn.source_event_ids):
            return None
        return canonicalize_turn_record(handled_turn, response_event_id=visible_router_echo_event_id)

    async def _finalize_dispatch_failure(
        self,
        *,
        target: MessageTarget,
        error: Exception,
        existing_event_id: str | None = None,
        on_visible_response: Callable[[str], Awaitable[None]] | None = None,
        delivery_turn_id: str | None = None,
    ) -> str | None:
        """Convert dispatch setup failures into a visible terminal message.

        The edit carries the turn when the caller has one, which puts this
        failure notice behind the same durable row an answer would use: the
        journal sources settle inside the enqueue, so the terminal record the
        caller writes next cannot land while the journal still calls this turn
        unfinished.

        A failed durable edit is not followed by a direct send, and that is the
        whole of the ownership rule here. Once the edit has been offered to the
        outbox, exactly one of two things is true and neither wants another
        message in the room.

        Either the enqueue was refused, which only the membership fence does,
        and the conversation this notice belongs to is one the bot has left.
        Sending anyway puts an old turn's error in front of whoever is in that
        room now. Or the row exists, attempted and unacknowledged, and the
        outbox still owes this turn an answer: the next recovery pass resends
        the frozen envelope and the placeholder becomes the error notice, which
        is the outcome this method wanted. Sending as well races that pass --
        no crash required -- and the loser is invisible, because
        acknowledgement is first-writer-wins, so the room keeps both notices
        while durable state names only one.

        An earlier version sent anyway and then tried to adopt the message as
        the row's outcome. Adoption cannot win that race, and it could not tell
        a fence refusal from a Matrix failure either, because both surface as a
        false return.

        The remaining direct send is for the case with no durable owner at all:
        no placeholder to edit, or an edit that never belonged to a turn. There
        is no row to race and nothing else will ever put this notice in the
        room.
        """
        error_text = get_user_friendly_error_message(error, self.deps.agent_name)
        terminal_extra_content = {STREAM_STATUS_KEY: STREAM_STATUS_COMPLETED}
        if existing_event_id is not None:
            edited = await self.deps.delivery_gateway.edit_text(
                EditTextRequest(
                    target=target,
                    event_id=existing_event_id,
                    new_text=error_text,
                    extra_content=terminal_extra_content,
                    delivery_turn_id=delivery_turn_id,
                ),
            )
            if edited:
                return existing_event_id
            if delivery_turn_id is not None:
                self.deps.logger.info(
                    "dispatch_failure_notice_left_to_the_outbox",
                    turn_id=delivery_turn_id,
                    existing_event_id=existing_event_id,
                )
                return None
        response_event_id = await self.deps.delivery_gateway.send_text(
            SendTextRequest(
                target=target,
                response_text=error_text,
                extra_content=terminal_extra_content,
            ),
        )
        if response_event_id is None:
            return None
        if on_visible_response is not None:
            await on_visible_response(response_event_id)
        return response_event_id

    def _build_response_settlement_callbacks(
        self,
        room: nio.MatrixRoom,
        *,
        source_event_id: str,
        handled_turn: TurnRecord,
    ) -> tuple[
        Callable[[], None],
        Callable[[str], Awaitable[None]],
        Callable[[str, int], Awaitable[None]],
    ]:
        """Build callbacks for interrupted-turn recording and deferred handled recording."""

        def record_interrupted_turn() -> None:
            self.deps.interrupted_turn_rooms.register(source_event_id, room_id=room.room_id)

        async def record_deferred_outcome(response_event_id: str) -> None:
            await record_deferred_outcome_response(
                self.deps.turn_store,
                handled_turn,
                response_event_id,
            )

        async def record_user_stop(response_event_id: str, stop_receipt_order: int) -> None:
            await record_user_stop_terminal(
                self.deps.turn_store,
                handled_turn,
                response_event_id,
                stop_receipt_order,
            )

        return record_interrupted_turn, record_deferred_outcome, record_user_stop

    async def _execute_response_action(  # noqa: C901, PLR0912, PLR0915
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
                registry = entity_identity_registry(self.deps.runtime.config, self.deps.runtime_paths)
                target_member_names = tuple(
                    registry.current_entity_name_for_user_id(member.full_id) or member.username
                    for member in action.form_team.eligible_members
                )

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
                        action.form_team.eligible_members,
                        self.deps.runtime.config,
                        self.deps.runtime_paths,
                    )

            record_interrupted_turn, record_deferred_outcome, record_user_stop = (
                self._build_response_settlement_callbacks(
                    room,
                    source_event_id=event.event_id,
                    handled_turn=handled_turn,
                )
            )

            recovered_response_event_id = (
                await self.deps.visible_responses.recovered_response_event_id(
                    handled_turn,
                    room_id=dispatch.target.room_id,
                )
                if reconcile_visible_response
                else None
            )

            async def record_visible_response(response_event_id: str) -> None:
                await self.deps.visible_responses.record_pending_visible_response(handled_turn, response_event_id)

            async def settle_redacted_sources() -> None:
                await self.deps.visible_responses.settle_source_events_ignored(handled_turn)

            try:
                response_request = ResponseRequest(
                    thread_history=dispatch.context.thread_history,
                    prompt=event.body,
                    user_id=dispatch.requester_user_id,
                    existing_event_id=recovered_response_event_id,
                    existing_event_is_placeholder=recovered_response_event_id is not None,
                    response_envelope=dispatch.envelope,
                    correlation_id=dispatch.correlation_id,
                    matrix_run_metadata=matrix_run_metadata,
                    requires_model_history_refresh=dispatch.context.requires_model_history_refresh,
                    scheduled_history_budget=dispatch.scheduled_history_budget,
                    payload_preparation=payload_preparation,
                    current_timestamp_ms=current_timestamp_ms,
                    current_prompt_is_structured=dispatch.current_prompt_is_structured,
                    pipeline_timing=dispatch_timing,
                    on_lifecycle_lock_acquired=on_lifecycle_lock_acquired,
                    prepare_source_turn=lambda: self.deps.turn_store.prepare_pending_response_source(
                        target=dispatch.target,
                        source_event_ids=handled_turn.indexed_event_ids,
                        terminal_source_event_ids=handled_turn.source_event_ids,
                    ),
                    on_source_turn_suppressed=settle_redacted_sources,
                    on_interrupted_response_recoverable=record_interrupted_turn,
                    on_deferred_outcome_handled=record_deferred_outcome,
                    on_user_stop_handled=record_user_stop,
                    on_visible_response=record_visible_response,
                )
                if action.kind == "team":
                    assert action.form_team is not None
                    assert team_mode is not None
                    response_event_id = await self.deps.response_runner.generate_team_response_helper(
                        response_request,
                        team_agents=action.form_team.eligible_members,
                        team_mode=team_mode.value,
                    )
                else:
                    response_event_id = await self.deps.response_runner.generate_response(
                        response_request,
                    )
            except PostLockRequestPreparationError as error:
                failure = error.__cause__ if isinstance(error.__cause__, Exception) else error
                response_event_id = await self._finalize_dispatch_failure(
                    target=dispatch.target,
                    error=failure,
                    existing_event_id=error.placeholder_event_id,
                    on_visible_response=record_visible_response,
                    delivery_turn_id=handled_turn.anchor_event_id,
                )
                await self.deps.turn_store.record_responded_turn(
                    canonicalize_turn_record(handled_turn, response_event_id=response_event_id),
                )
                return
            if response_event_id is not None:
                await self.deps.turn_store.record_responded_turn(
                    canonicalize_turn_record(handled_turn, response_event_id=response_event_id),
                )

    async def handle_prepared_turn(self, turn: PreparedTurn) -> None:
        """Dispatch one logical turn emitted directly by the coalescing gate."""
        if await self._dispatch_interactive_selection_turn(turn):
            return
        coalescing_key = turn.ingress.coalescing_key
        assert coalescing_key is not None
        _consume_queued_notice_reservations_from_metadata(
            turn.dispatch_metadata,
            target_key=self._queued_notice_target_key(turn.room, turn.event, coalescing_key),
        )
        timing_scope = event_timing_scope(turn.event.event_id)
        dispatch_timing = get_dispatch_pipeline_timing(turn.event.source)
        if dispatch_timing is not None:
            dispatch_timing.mark("gate_exit")
        async with self.deps.resolver.turn_lookup_scope():
            dispatch_start = time.monotonic()
            for item in turn.dispatch_metadata:
                item.close_once()
            await dispatch_text_message(self, turn)
            emit_elapsed_timing(
                "coalescing.handle_turn.dispatch_text_message",
                dispatch_start,
                source_event_count=len(turn.handled_turn.source_event_ids),
                timing_scope=timing_scope,
            )

    def _queued_notice_target_key(
        self,
        room: nio.MatrixRoom,
        event: PreparedIngress,
        coalescing_key: CoalescingKey,
    ) -> ResponseLifecycleKey:
        """Return the response lifecycle key for one prepared logical turn."""
        context_event = _room_level_context_event(event) if coalescing_key.thread_id is None else event
        return self.deps.resolver.build_message_target(
            room_id=room.room_id,
            thread_id=coalescing_key.thread_id,
            reply_to_event_id=event.event_id,
            event_source=context_event.source,
        ).lifecycle_key

    async def _claim_live_turn(
        self,
        turn_claim: TurnRecord,
        *,
        source_event_id: str,
        lane_reservation: _PromptIngressReservationOwner | None = None,
    ) -> TurnRecord | TurnDispatchOutcome:
        """Claim one live source or return its explicit competing-owner outcome.

        A contended claim waits for the competing owner's turn to settle, and
        that wait must not hold an ingress lane slot. The competing owner's
        coalesced batch does not flush until every undelivered slot in the
        sender's lane settles, and its turn does not settle until it flushes,
        so a held slot wedges both sides permanently and silently.
        """
        if self.deps.turn_store.try_claim_turn(turn_claim):
            return turn_claim

        if lane_reservation is not None:
            await lane_reservation.release()
        await self.deps.turn_store.wait_for_turn_settled(turn_claim.indexed_event_ids)
        if self.deps.turn_store.is_handled(source_event_id):
            return TurnDispatchOutcome.DEFERRED
        if self.deps.turn_store.try_claim_turn(turn_claim):
            if lane_reservation is not None:
                lane_reservation.reenter_lane()
            return turn_claim
        # A settled discovery-alias owner or a newer competing claimant owns
        # this duplicate semantic turn.
        return TurnDispatchOutcome.INTENTIONALLY_IGNORED

    async def handle_text_event(
        self,
        room: nio.MatrixRoom,
        event: nio.RoomMessageFormatted,
        *,
        receipt_time: float | None = None,
        reservation_owner: _PromptIngressReservationOwner | None = None,
    ) -> TurnDispatchOutcome:
        """Handle one inbound text event."""
        async with self.deps.resolver.turn_lookup_scope():
            return await self._handle_message_inner(
                room,
                event,
                receipt_time=receipt_time,
                reservation_owner=reservation_owner,
            )

    async def _handle_message_inner(
        self,
        room: nio.MatrixRoom,
        event: nio.RoomMessageFormatted,
        *,
        receipt_time: float | None = None,
        reservation_owner: _PromptIngressReservationOwner | None = None,
    ) -> TurnDispatchOutcome:
        """Handle one text message inside the per-turn conversation lookup scope."""
        event_info = EventInfo.from_event(event.source)
        event_content = event.source.get("content") if isinstance(event.source, dict) else None
        is_nonterminal_stream = isinstance(event_content, dict) and event_content.get(STREAM_STATUS_KEY) in {
            STREAM_STATUS_PENDING,
            STREAM_STATUS_STREAMING,
        }
        if not isinstance(event.body, str) or (is_nonterminal_stream and event_info.is_edit):
            return TurnDispatchOutcome.INTENTIONALLY_IGNORED
        prechecked_event = await self._precheck_dispatch_event(room, event, is_edit=event_info.is_edit)
        if prechecked_event is None:
            return TurnDispatchOutcome.INTENTIONALLY_IGNORED
        if is_nonterminal_stream:
            return TurnDispatchOutcome.INTENTIONALLY_IGNORED

        dispatch_timing = create_dispatch_pipeline_timing(
            event_id=event.event_id,
            room_id=room.room_id,
        )
        attach_dispatch_pipeline_timing(event.source, dispatch_timing)
        owns_reservation = reservation_owner is None
        if reservation_owner is None:
            reservation_owner = self.reserve_prompt_ingress_order(
                room,
                prechecked_event.requester_user_id,
                receipt_time=receipt_time,
            )
        try:
            if event_info.is_edit:
                await reservation_owner.release()
                await self._handle_edit_event(room, prechecked_event, event_info)
                return TurnDispatchOutcome.INTENTIONALLY_IGNORED
            routed_alias = self.deps.ingress.router_relay_original_event_id(event)
            claim_aliases = (routed_alias,) if routed_alias else ()
            pending_turn = TurnRecord.create(
                [event.event_id],
                discovery_event_ids=claim_aliases,
                completed=False,
            )
            turn_claim = await self._claim_live_turn(
                pending_turn,
                source_event_id=event.event_id,
                lane_reservation=reservation_owner,
            )
            if isinstance(turn_claim, TurnDispatchOutcome):
                return turn_claim
            reservation_owner.pending_turn_claim = turn_claim
            outcome = await self._ingest_live_text_event(
                room,
                prechecked_event,
                dispatch_timing=dispatch_timing,
                reservation_owner=reservation_owner,
            )
            return (
                TurnDispatchOutcome.DEFERRED
                if outcome is _IngressAdmissionOutcome.ADMITTED
                else TurnDispatchOutcome.INTENTIONALLY_IGNORED
            )
        finally:
            if reservation_owner.pending_turn_claim is not None and not reservation_owner.admitted:
                self.deps.turn_store.release_pending_turn_claim(reservation_owner.pending_turn_claim)
            if owns_reservation:
                await reservation_owner.release()

    async def _ingest_live_text_event(
        self,
        room: nio.MatrixRoom,
        prechecked_event: _PrecheckedEvent[nio.RoomMessageFormatted],
        *,
        dispatch_timing: DispatchPipelineTiming | None,
        reservation_owner: _PromptIngressReservationOwner,
    ) -> _IngressAdmissionOutcome:
        """Resolve, normalize, and admit one live (non-edit) text event."""
        event = prechecked_event.event
        try:
            ingress_thread_id = await self.deps.resolver.coalescing_thread_id(room, event)
        except ThreadMembershipLookupError:
            if await self._notify_command_target_not_ready(room, event):
                return _IngressAdmissionOutcome.CONSUMED
            raise
        if await self._should_skip_router_before_shared_ingress_work(
            room,
            event,
            requester_user_id=prechecked_event.requester_user_id,
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
            requester_user_id=prechecked_event.requester_user_id,
            reservation_owner=reservation_owner,
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
        prechecked_event = await self._precheck_dispatch_event(room, event)
        if prechecked_event is None:
            return TurnDispatchOutcome.INTENTIONALLY_IGNORED
        dispatch_timing = create_dispatch_pipeline_timing(
            event_id=prechecked_event.event.event_id,
            room_id=room.room_id,
        )
        attach_dispatch_pipeline_timing(prechecked_event.event.source, dispatch_timing)
        if (
            is_audio_message_event(prechecked_event.event)
            and self.deps.ingress.managed_entity_name_for_sender(prechecked_event.event.sender) is not None
        ):
            self.deps.logger.debug(
                "Ignoring agent audio event for voice transcription",
                event_id=prechecked_event.event.event_id,
                sender=prechecked_event.event.sender,
            )
            return TurnDispatchOutcome.INTENTIONALLY_IGNORED
        pending_turn = TurnRecord.create([prechecked_event.event.event_id], completed=False)
        turn_claim = await self._claim_live_turn(
            pending_turn,
            source_event_id=prechecked_event.event.event_id,
        )
        if isinstance(turn_claim, TurnDispatchOutcome):
            return turn_claim
        try:
            reservation_owner = self.reserve_prompt_ingress_order(
                room,
                prechecked_event.requester_user_id,
                receipt_time=receipt_time,
            )
        except BaseException:
            self.deps.turn_store.release_pending_turn_claim(turn_claim)
            raise
        reservation_owner.pending_turn_claim = turn_claim
        try:
            if is_audio_message_event(prechecked_event.event):
                await self._on_audio_media_message(
                    room,
                    _PrecheckedEvent(
                        event=prechecked_event.event,
                        requester_user_id=prechecked_event.requester_user_id,
                    ),
                    dispatch_timing=dispatch_timing,
                    reservation_owner=reservation_owner,
                    turn_claim=turn_claim,
                )
                reservation_owner.pending_turn_claim = None
                dispatch_outcome = TurnDispatchOutcome.DEFERRED
            else:
                coalescing_thread_id = await self.deps.resolver.coalescing_thread_id(room, prechecked_event.event)
                admission_outcome = await self._dispatch_special_media_as_text(
                    room,
                    prechecked_event,
                    reservation_owner=reservation_owner,
                    coalescing_thread_id=coalescing_thread_id,
                )
                if admission_outcome is _IngressAdmissionOutcome.ADMITTED:
                    dispatch_outcome = TurnDispatchOutcome.DEFERRED
                elif admission_outcome is _IngressAdmissionOutcome.CONSUMED or not is_matrix_media_dispatch_event(
                    prechecked_event.event,
                ):
                    dispatch_outcome = TurnDispatchOutcome.INTENTIONALLY_IGNORED
                else:
                    await self._enqueue_media_for_dispatch(
                        room=room,
                        event=prechecked_event.event,
                        coalescing_thread_id=coalescing_thread_id,
                        requester_user_id=prechecked_event.requester_user_id,
                        reservation_owner=reservation_owner,
                    )
                    dispatch_outcome = TurnDispatchOutcome.DEFERRED
        finally:
            if reservation_owner.pending_turn_claim is not None and not reservation_owner.admitted:
                self.deps.turn_store.release_pending_turn_claim(reservation_owner.pending_turn_claim)
            await reservation_owner.release()
        return dispatch_outcome

    async def _dispatch_special_media_as_text(
        self,
        room: nio.MatrixRoom,
        prechecked_event: _PrecheckedInboundMediaEvent,
        *,
        reservation_owner: _PromptIngressReservationOwner,
        coalescing_thread_id: str | None,
    ) -> _IngressAdmissionOutcome:
        """Handle media events that normalize into the text dispatch pipeline."""
        event = prechecked_event.event
        if is_file_message_event(event):
            return await self._dispatch_file_sidecar_text_preview(
                room,
                _PrecheckedEvent(
                    event=event,
                    requester_user_id=prechecked_event.requester_user_id,
                ),
                reservation_owner=reservation_owner,
                coalescing_thread_id=coalescing_thread_id,
            )
        return _IngressAdmissionOutcome.IGNORED

    async def _on_audio_media_message(
        self,
        room: nio.MatrixRoom,
        prechecked_event: _PrecheckedEvent[AudioMessageEvent],
        *,
        dispatch_timing: DispatchPipelineTiming | None,
        reservation_owner: _PromptIngressReservationOwner,
        turn_claim: TurnRecord,
    ) -> None:
        """Resolve the audio conversation key once, then defer voice normalization."""
        event = prechecked_event.event

        voice_target, admission_key = await self._resolve_ready_voice_target(
            room,
            event,
            requester_user_id=prechecked_event.requester_user_id,
        )

        ready_task = asyncio.create_task(
            self._ready_voice_event(
                room=room,
                prechecked_event=prechecked_event,
                voice_target=voice_target,
                dispatch_timing=dispatch_timing,
                turn_claim=turn_claim,
            ),
            name=f"voice_ready:{room.room_id}:{event.event_id}",
        )
        await reservation_owner.admit(
            admission_key,
            ready_task=ready_task,
            source_event_id=event.event_id,
            source_kind=VOICE_SOURCE_KIND,
        )

    async def _resolve_ready_voice_target(
        self,
        room: nio.MatrixRoom,
        event: AudioMessageEvent,
        *,
        requester_user_id: str,
    ) -> tuple[MessageTarget, CoalescingKey]:
        coalescing_thread_id = await self.deps.resolver.coalescing_thread_id(room, event)
        voice_target = self.deps.resolver.build_message_target(
            room_id=room.room_id,
            thread_id=coalescing_thread_id,
            reply_to_event_id=event.event_id,
            event_source=event.source,
        )
        return voice_target, requester_coalescing_key(room.room_id, coalescing_thread_id, requester_user_id)

    async def _ready_voice_event(
        self,
        *,
        room: nio.MatrixRoom,
        prechecked_event: _PrecheckedEvent[AudioMessageEvent],
        voice_target: MessageTarget,
        dispatch_timing: DispatchPipelineTiming | None,
        turn_claim: TurnRecord,
    ) -> ReadyPendingEvent | None:
        """Normalize a raw voice event after its conversation key is fixed."""
        event = prechecked_event.event
        queued_notice_reservation = None
        visible_echo = self.deps.visible_voice_echo.start(
            VisibleVoiceEchoRequest(
                source_event_id=event.event_id,
                target=voice_target,
                requester_user_id=prechecked_event.requester_user_id,
                raw_source=event.source,
            ),
        )
        reservation_released_or_handed_off = False
        claim_transferred = False
        claim_metadata = PendingDispatchMetadata(
            kind=_PENDING_TURN_CLAIM_METADATA_KIND,
            payload=turn_claim,
            close=lambda: self.deps.turn_store.release_pending_turn_claim(turn_claim),
        )
        try:
            envelope = self.deps.resolver.build_ingress_envelope(
                event=cast("DispatchEvent", event),
                requester_user_id=prechecked_event.requester_user_id,
                target=voice_target,
                source_kind=VOICE_SOURCE_KIND,
            )
            queued_notice_reservation = self._queued_notice_reservation_if_busy(
                target=voice_target,
                envelope=envelope,
            )
            normalized_event, effective_thread_id = await self._normalize_voice_event_or_fallback(
                room=room,
                event=event,
                thread_id=voice_target.resolved_thread_id,
                dispatch_timing=dispatch_timing,
            )

            await self.deps.visible_voice_echo.finish(visible_echo, normalized_event)

            if not await self.deps.visible_voice_echo.await_publication(
                room=room,
                source_event_id=event.event_id,
                requester_user_id=prechecked_event.requester_user_id,
            ):
                return None

            normalized_target = self.deps.resolver.build_message_target(
                room_id=room.room_id,
                thread_id=effective_thread_id,
                reply_to_event_id=normalized_event.event_id,
                event_source=normalized_event.source,
            )
            envelope = self.deps.resolver.build_ingress_envelope(
                event=normalized_event,
                requester_user_id=prechecked_event.requester_user_id,
                target=normalized_target,
                source_kind=VOICE_SOURCE_KIND,
            )
            queued_notice_reservation = self._voice_queued_notice_reservation(
                preliminary_target=voice_target,
                target=normalized_target,
                envelope=envelope,
                queued_notice_reservation=queued_notice_reservation,
            )
            reservation_released_or_handed_off = True
            claim_transferred = True
            return ReadyPendingEvent(
                pending_event=PendingEvent(
                    event=replace(
                        normalized_event,
                        source_kind=envelope.source_kind,
                        requester_user_id=prechecked_event.requester_user_id,
                        dispatch_policy_source_kind=envelope.dispatch_policy_source_kind,
                        hook_source=envelope.hook_source,
                        message_received_depth=envelope.message_received_depth,
                        trust_internal_payload_metadata=True,
                        turn_dispatch_recovery=turn_dispatch_recovery_active(),
                    ),
                    room=room,
                    dispatch_metadata=(
                        *_queued_notice_dispatch_metadata(queued_notice_reservation, normalized_target),
                        claim_metadata,
                    ),
                ),
            )
        except asyncio.CancelledError:
            self.deps.visible_voice_echo.finish_after_cancellation(
                visible_echo,
                _raw_voice_fallback_event(event, thread_id=voice_target.resolved_thread_id),
            )
            raise
        except Exception as exc:
            if queued_notice_reservation is not None:
                queued_notice_reservation.cancel()
                queued_notice_reservation = None
            try:
                fallback = await self._ready_voice_fallback_event(
                    room=room,
                    event=event,
                    requester_user_id=prechecked_event.requester_user_id,
                    thread_id=voice_target.resolved_thread_id,
                    dispatch_timing=dispatch_timing,
                    error=exc,
                )
            except asyncio.CancelledError:
                self.deps.visible_voice_echo.finish_after_cancellation(
                    visible_echo,
                    _raw_voice_fallback_event(event, thread_id=voice_target.resolved_thread_id),
                )
                raise
            await self.deps.visible_voice_echo.finish(visible_echo, fallback.event)
            publication_allowed = False
            try:
                publication_allowed = await self.deps.visible_voice_echo.await_publication(
                    room=room,
                    source_event_id=event.event_id,
                    requester_user_id=prechecked_event.requester_user_id,
                )
            finally:
                if not publication_allowed:
                    close_pending_event_metadata_once([fallback.ready.pending_event])
            if not publication_allowed:
                return None
            fallback.ready.pending_event.dispatch_metadata += (claim_metadata,)
            claim_transferred = True
            return fallback.ready
        finally:
            self.deps.visible_voice_echo.abandon_unsettled(visible_echo)
            if not reservation_released_or_handed_off and queued_notice_reservation is not None:
                queued_notice_reservation.cancel()
            if not claim_transferred:
                self.deps.turn_store.release_pending_turn_claim(turn_claim)

    async def _prepare_raw_voice_fallback_event(
        self,
        *,
        room: nio.MatrixRoom,
        event: AudioMessageEvent,
        thread_id: str | None,
    ) -> PreparedIngress:
        try:
            fallback = await self.deps.normalizer.prepare_raw_voice_fallback_event(
                VoiceNormalizationRequest(
                    room=room,
                    event=event,
                    thread_id=thread_id,
                ),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.deps.logger.warning(
                "Voice raw-audio fallback preparation failed; dispatching text-only fallback",
                event_id=event.event_id,
                room_id=room.room_id,
                exception_type=exc.__class__.__name__,
                error=str(exc),
            )
            return _raw_voice_fallback_event(event, thread_id=thread_id)
        return fallback.event

    async def _ready_voice_fallback_event(
        self,
        *,
        room: nio.MatrixRoom,
        event: AudioMessageEvent,
        requester_user_id: str,
        thread_id: str | None,
        dispatch_timing: DispatchPipelineTiming | None,
        error: Exception,
    ) -> _ReadyVoiceFallback:
        """Return a raw-audio fallback when voice readiness fails before STT."""
        self.deps.logger.warning(
            "Voice readiness failed; dispatching raw-audio fallback",
            event_id=event.event_id,
            room_id=room.room_id,
            exception_type=error.__class__.__name__,
            error=str(error),
        )
        fallback_event = await self._prepare_raw_voice_fallback_event(room=room, event=event, thread_id=thread_id)
        attach_dispatch_pipeline_timing(fallback_event.source, dispatch_timing)
        queued_notice_reservation = None
        dispatch_policy_source_kind = None
        hook_source = None
        message_received_depth = 0
        target = None
        try:
            target = self.deps.resolver.build_message_target(
                room_id=room.room_id,
                thread_id=thread_id,
                reply_to_event_id=fallback_event.event_id,
                event_source=fallback_event.source,
            )
            envelope = self.deps.resolver.build_ingress_envelope(
                event=fallback_event,
                requester_user_id=requester_user_id,
                target=target,
                source_kind=VOICE_SOURCE_KIND,
            )
            queued_notice_reservation = self._voice_queued_notice_reservation(
                preliminary_target=target,
                target=target,
                envelope=envelope,
                queued_notice_reservation=None,
            )
            dispatch_policy_source_kind = envelope.dispatch_policy_source_kind
            hook_source = envelope.hook_source
            message_received_depth = envelope.message_received_depth
        except Exception as metadata_error:
            self.deps.logger.warning(
                "Voice fallback metadata failed; dispatching without active-turn reservation",
                event_id=event.event_id,
                room_id=room.room_id,
                exception_type=metadata_error.__class__.__name__,
                error=str(metadata_error),
            )
        return _ReadyVoiceFallback(
            event=fallback_event,
            ready=ReadyPendingEvent(
                pending_event=PendingEvent(
                    event=replace(
                        fallback_event,
                        source_kind=VOICE_SOURCE_KIND,
                        requester_user_id=requester_user_id,
                        dispatch_policy_source_kind=dispatch_policy_source_kind,
                        hook_source=hook_source,
                        message_received_depth=message_received_depth,
                        trust_internal_payload_metadata=True,
                        turn_dispatch_recovery=turn_dispatch_recovery_active(),
                    ),
                    room=room,
                    dispatch_metadata=_queued_notice_dispatch_metadata(queued_notice_reservation, target),
                ),
            ),
        )

    async def _normalize_voice_event_or_fallback(
        self,
        *,
        room: nio.MatrixRoom,
        event: AudioMessageEvent,
        thread_id: str | None,
        dispatch_timing: DispatchPipelineTiming | None,
    ) -> tuple[PreparedIngress, str | None]:
        """Normalize voice or return a raw-audio fallback event for unexpected failures."""
        if dispatch_timing is not None:
            dispatch_timing.mark("ingress_normalize_start")
        try:
            normalized_voice = await self.deps.normalizer.prepare_voice_event(
                VoiceNormalizationRequest(
                    room=room,
                    event=event,
                    thread_id=thread_id,
                ),
            )
        except Exception as exc:
            self.deps.logger.warning(
                "Voice normalization failed; dispatching raw-audio fallback",
                event_id=event.event_id,
                room_id=room.room_id,
                exception_type=exc.__class__.__name__,
                error=str(exc),
            )
            normalized_event = await self._prepare_raw_voice_fallback_event(
                room=room,
                event=event,
                thread_id=thread_id,
            )
            effective_thread_id = thread_id
        else:
            if normalized_voice is None:
                self.deps.logger.warning(
                    "Voice normalization returned no event; dispatching raw-audio fallback",
                    event_id=event.event_id,
                    room_id=room.room_id,
                    thread_id=thread_id,
                )
                normalized_event = await self._prepare_raw_voice_fallback_event(
                    room=room,
                    event=event,
                    thread_id=thread_id,
                )
            else:
                normalized_event = normalized_voice.event
            effective_thread_id = thread_id
        if dispatch_timing is not None:
            dispatch_timing.mark("ingress_normalize_ready")
        attach_dispatch_pipeline_timing(
            normalized_event.source,
            dispatch_timing,
        )
        return normalized_event, effective_thread_id

    async def _dispatch_file_sidecar_text_preview(
        self,
        room: nio.MatrixRoom,
        prechecked_event: _PrecheckedEvent[FileMessageEvent],
        *,
        reservation_owner: _PromptIngressReservationOwner,
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
            requester_user_id=prechecked_event.requester_user_id,
            reservation_owner=reservation_owner,
            coalescing_thread_id=coalescing_thread_id,
        )
