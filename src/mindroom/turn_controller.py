"""Control one inbound turn from ingress to recorded outcome."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, replace
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol, cast

from mindroom import interactive
from mindroom.attachment_ids import merge_attachment_ids
from mindroom.attachments import parse_attachment_ids_from_event_source
from mindroom.coalescing import CoalescingGate, ReadyPendingEvent
from mindroom.coalescing_batch import (
    CoalescedBatch,
    CoalescingKey,
    PendingEvent,
    build_coalesced_batch,
)
from mindroom.coalescing_cleanup import close_pending_event_metadata_once
from mindroom.commands.parsing import command_parser
from mindroom.constants import (
    ATTACHMENT_IDS_KEY,
    ORIGINAL_SENDER_KEY,
    PER_FIRE_THREAD_ROOT_EVENT_ID_KEY,
    PER_FIRE_THREAD_ROOT_KEY,
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
    DispatchHandoff,
    DispatchIngressMetadata,
    DispatchPayloadMetadata,
    MediaDispatchEvent,
    PendingDispatchMetadata,
    PreparedTextEvent,
    TextDispatchEvent,
    build_dispatch_handoff,
    payload_metadata_from_source,
)
from mindroom.dispatch_recovery_context import turn_dispatch_recovery_active
from mindroom.dispatch_replay_guard import has_newer_unresponded_cached_thread_event, has_newer_unresponded_in_thread
from mindroom.dispatch_source import (
    IMAGE_SOURCE_KIND,
    MEDIA_SOURCE_KIND,
    MESSAGE_SOURCE_KIND,
    TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
    VOICE_SOURCE_KIND,
    ScheduledHistoryBudget,
    content_owns_per_fire_thread_root,
    scheduled_history_limit_from_content,
    source_kind_allows_internal_relay_detection,
)
from mindroom.entity_resolution import entity_identity_registry
from mindroom.error_handling import get_user_friendly_error_message
from mindroom.handled_turns import TurnRecord, with_user_stop
from mindroom.hooks import MessageEnvelope, hook_ingress_policy
from mindroom.inbound_turn_normalizer import (
    DispatchPayloadWithAttachmentsRequest,
    InboundTurnNormalizer,
    TextNormalizationRequest,
    VoiceNormalizationRequest,
)
from mindroom.logging_config import bound_log_context
from mindroom.matrix.cache import ThreadHistoryResult
from mindroom.matrix.cache.thread_reads import ThreadReadMode
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
from mindroom.matrix.thread_membership import ThreadMembershipLookupError
from mindroom.prompt_ingress_reservation import PromptIngressReservationOwner as _PromptIngressReservationOwner
from mindroom.response_payload_preparation import (
    DispatchPayloadInputs,
    ResponsePayloadPreparation,
)
from mindroom.response_runner import PostLockRequestPreparationError, ResponseRequest
from mindroom.routing import suggest_responder_for_message
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
    original_sender_for_router_handoff,
)
from mindroom.turn_policy import IngressHookRunner, PreparedDispatch, ResponseAction, TurnPolicy
from mindroom.turn_record import canonicalize_turn_record
from mindroom.visible_voice_echo import VisibleVoiceEchoRequest

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    import nio
    import structlog

    from mindroom.bot_runtime_view import BotRuntimeView
    from mindroom.command_turn_executor import CommandTurnExecutor
    from mindroom.conversation_resolver import ConversationResolver
    from mindroom.delivery_gateway import DeliveryGateway
    from mindroom.ingress_validation import IngressValidator
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.matrix.conversation_cache import MatrixConversationCache
    from mindroom.matrix.identity import MatrixID
    from mindroom.message_target import MessageTarget
    from mindroom.response_lifecycle import QueuedHumanNoticeReservation
    from mindroom.response_runner import ResponseRunner
    from mindroom.runtime_protocols import OrchestratorRuntime
    from mindroom.sync_restart_retry import InterruptedTurnRooms
    from mindroom.tool_system.runtime_context import ToolRuntimeSupport
    from mindroom.turn_store import TurnStore
    from mindroom.visible_response_reconciliation import VisibleResponseReconciler
    from mindroom.visible_voice_echo import VisibleVoiceEchoLifecycle

_QUEUED_NOTICE_METADATA_KIND = "queued_notice_reservation"
_PENDING_TURN_CLAIM_METADATA_KIND = "pending_turn_claim"
_ROUTER_TARGET_STARTING_TEXT = "That agent is still starting. Please try again shortly."
_ROUTER_TARGET_UNAVAILABLE_TEXT = (
    "⚠️ I couldn't determine which agent or team should help with this. "
    "Please try mentioning an agent or team directly with @ or rephrase your request."
)


def _gate_router_target_readiness(
    orchestrator: OrchestratorRuntime | None,
    suggested_entity: str | None,
) -> tuple[str | None, bool]:
    """Drop a known unready or stale router selection before relay delivery."""
    if suggested_entity is None or orchestrator is None:
        return suggested_entity, False
    first_sync_complete = orchestrator.entity_first_sync_complete(suggested_entity)
    return (suggested_entity if first_sync_complete is True else None, first_sync_complete is False)


@dataclass(frozen=True)
class _RouterTargetResolution:
    """One router target after its runtime readiness check."""

    selected_entity: str | None
    suggested_entity: str | None
    response_text: str
    target_unavailable: bool


def _resolve_router_target(
    orchestrator: OrchestratorRuntime | None,
    suggested_entity: str | None,
    scheduled_prompt: str | None,
) -> _RouterTargetResolution | None:
    """Resolve one target or retain recovered work until its runtime is ready."""
    selected_entity = suggested_entity
    suggested_entity, target_starting = _gate_router_target_readiness(
        orchestrator,
        suggested_entity,
    )
    if target_starting and turn_dispatch_recovery_active():
        return None
    if target_starting:
        response_text = _ROUTER_TARGET_STARTING_TEXT
    elif suggested_entity is None:
        response_text = _ROUTER_TARGET_UNAVAILABLE_TEXT
    else:
        response_text = (
            f"@{suggested_entity} {scheduled_prompt}"
            if scheduled_prompt is not None
            else f"@{suggested_entity} could you help with this?"
        )
    return _RouterTargetResolution(
        selected_entity=selected_entity,
        suggested_entity=suggested_entity,
        response_text=response_text,
        target_unavailable=not target_starting and suggested_entity is None,
    )


@dataclass(frozen=True)
class _RouterRelayDelivery:
    """One final router relay delivery decision."""

    event_id: str | None
    suggested_entity: str | None
    deferred_for_recovery: bool = False


async def _send_router_relay_after_readiness_recheck(
    *,
    orchestrator: OrchestratorRuntime | None,
    delivery_gateway: DeliveryGateway,
    selected_entity: str | None,
    suggested_entity: str | None,
    delivery_request: SendTextRequest,
) -> _RouterRelayDelivery:
    """Recheck one sampled target immediately before sending its relay."""
    if selected_entity is None or orchestrator is None:
        return _RouterRelayDelivery(
            event_id=await delivery_gateway.send_text(delivery_request),
            suggested_entity=suggested_entity,
        )
    final_readiness = orchestrator.entity_first_sync_complete(selected_entity)
    if final_readiness is True:
        return _RouterRelayDelivery(
            event_id=await delivery_gateway.send_text(delivery_request),
            suggested_entity=suggested_entity,
        )
    if final_readiness is False and turn_dispatch_recovery_active():
        return _RouterRelayDelivery(
            event_id=None,
            suggested_entity=suggested_entity,
            deferred_for_recovery=True,
        )
    fallback_extra_content = dict(delivery_request.extra_content or {})
    fallback_extra_content.pop(ORIGINAL_SENDER_KEY, None)
    fallback_extra_content.pop(SOURCE_KIND_KEY, None)
    fallback_request = replace(
        delivery_request,
        response_text=_ROUTER_TARGET_STARTING_TEXT if final_readiness is False else _ROUTER_TARGET_UNAVAILABLE_TEXT,
        extra_content=fallback_extra_content or None,
    )
    return _RouterRelayDelivery(
        event_id=await delivery_gateway.send_text(fallback_request),
        suggested_entity=None,
    )


def _room_level_context_event(event: TextDispatchEvent) -> TextDispatchEvent:
    """Return an event view that cannot pull dispatch context through Matrix relations."""
    if not isinstance(event.source, dict):
        return event
    content = event.source.get("content")
    if not isinstance(content, dict) or "m.relates_to" not in content:
        return event
    stripped_content = dict(content)
    stripped_content.pop("m.relates_to", None)
    stripped_source = {**event.source, "content": stripped_content}
    if isinstance(event, PreparedTextEvent):
        return replace(event, source=stripped_source)
    return PreparedTextEvent(
        sender=event.sender,
        event_id=event.event_id,
        body=event.body,
        source=stripped_source,
        server_timestamp=event.server_timestamp,
    )


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
            target_key=(target.room_id, target.resolved_thread_id),
        ),
    )


def _consume_queued_notice_reservations_from_metadata(
    dispatch_metadata: tuple[PendingDispatchMetadata, ...],
    *,
    target_key: tuple[str, str | None],
) -> None:
    reservation_items = [item for item in dispatch_metadata if item.kind == _QUEUED_NOTICE_METADATA_KIND]
    for item in reservation_items:
        reservation = cast("QueuedHumanNoticeReservation", item.payload)
        if item.target_key == target_key:
            reservation.consume()
        else:
            reservation.cancel()


def _raw_voice_fallback_event(event: AudioMessageEvent, *, thread_id: str | None) -> PreparedTextEvent:
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
    return PreparedTextEvent(
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
        event: nio.RoomMessageText,
        event_info: EventInfo,
        requester_user_id: str,
    ) -> None:
        """Regenerate the owned response for one edited user turn."""


@dataclass(frozen=True)
class _PrecheckedEvent[T]:
    """A raw or prepared event that already passed ingress prechecks."""

    event: T
    requester_user_id: str


type _PrecheckedTextDispatchEvent = _PrecheckedEvent[TextDispatchEvent]
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

    event: PreparedTextEvent
    ready: ReadyPendingEvent


@dataclass(frozen=True)
class TurnControllerDeps:
    """Collaborators needed for turn control, policy, and execution."""

    runtime: BotRuntimeView
    logger: structlog.stdlib.BoundLogger
    runtime_paths: RuntimePaths
    agent_name: str
    matrix_id: MatrixID
    conversation_cache: MatrixConversationCache
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
                room_id=room.room_id,
                sender_id=requester_user_id,
                receipt_time=receipt_time,
            ),
        )

    def _precheck_dispatch_event[T: DispatchEvent | MatrixMediaEvent](
        self,
        room: nio.MatrixRoom,
        event: T,
        *,
        is_edit: bool = False,
    ) -> _PrecheckedEvent[T] | None:
        """Return a typed prechecked event for turn dispatch."""
        requester_user_id = self.deps.ingress.precheck_event(room, event, is_edit=is_edit)
        if requester_user_id is None:
            return None
        return _PrecheckedEvent(event=event, requester_user_id=requester_user_id)

    def _has_newer_unresponded_in_thread(
        self,
        event: TextDispatchEvent,
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

    async def _has_newer_unresponded_cached_thread_event(
        self,
        *,
        room_id: str,
        event: TextDispatchEvent,
        requester_user_id: str,
        thread_id: str | None,
        may_be_superseded_by_newer_requester_turn: bool,
    ) -> bool:
        """Return positive replay proof from raw cached room events when thread history degraded."""
        event_cache = self.deps.runtime.event_cache
        return await has_newer_unresponded_cached_thread_event(
            room_id=room_id,
            event=event,
            requester_user_id=requester_user_id,
            thread_id=thread_id,
            may_be_superseded_by_newer_requester_turn=may_be_superseded_by_newer_requester_turn,
            get_recent_room_events=event_cache.get_recent_room_events if event_cache is not None else None,
            get_thread_id_for_event=self.deps.conversation_cache.get_thread_id_for_event,
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
        return left.room_id == right.room_id and left.resolved_thread_id == right.resolved_thread_id

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
        prepared_event: PreparedTextEvent,
        dispatch_event: TextDispatchEvent,
        envelope: MessageEnvelope,
        coalescing_thread_id: str | None,
        requester_user_id: str,
        reservation_owner: _PromptIngressReservationOwner,
        callback_source_kind: str | None = None,
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
                dispatch_event,
                room,
                source_kind=envelope.source_kind,
                callback_source_kind=callback_source_kind,
                dispatch_policy_source_kind=envelope.dispatch_policy_source_kind,
                hook_source=envelope.hook_source,
                message_received_depth=envelope.message_received_depth,
                requester_user_id=requester_user_id,
                reservation_owner=reservation_owner,
                coalescing_key=CoalescingKey(room.room_id, coalescing_thread_id, requester_user_id),
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
                coalescing_key=CoalescingKey(room.room_id, coalescing_thread_id, requester_user_id),
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
        event: nio.RoomMessageText,
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
            thread_history = await self.deps.conversation_cache.get_dispatch_thread_snapshot(
                room.room_id,
                thread_id,
                caller_label="router_pre_ingress_skip",
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
        return CoalescingKey(
            room.room_id,
            coalescing_thread_id,
            requester_user_id,
        )

    async def _append_live_event_with_timing(
        self,
        room_id: str,
        event: nio.RoomMessage,
        *,
        event_info: EventInfo,
        dispatch_timing: DispatchPipelineTiming | None,
    ) -> None:
        """Persist one ingress cache mutation while recording its contribution to ingress latency."""
        if dispatch_timing is not None:
            dispatch_timing.mark("ingress_cache_append_start")
        await self.deps.conversation_cache.append_live_event(room_id, event, event_info=event_info)
        if dispatch_timing is not None:
            dispatch_timing.mark("ingress_cache_append_ready")

    async def _resolve_text_event_with_ingress_timing(
        self,
        event: nio.RoomMessageText,
        *,
        dispatch_timing: DispatchPipelineTiming | None,
    ) -> PreparedTextEvent:
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
        prepared_event: PreparedTextEvent,
        dispatch_event: TextDispatchEvent,
        requester_user_id: str,
        reservation_owner: _PromptIngressReservationOwner,
        coalescing_thread_id: str | None,
        callback_source_kind: str | None = None,
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
                dispatch_event=dispatch_event,
                envelope=envelope,
                coalescing_thread_id=coalescing_thread_id,
                requester_user_id=requester_user_id,
            )
            return _IngressAdmissionOutcome.CONSUMED
        return await self._enqueue_prepared_text_for_dispatch(
            room=room,
            prepared_event=prepared_event,
            dispatch_event=dispatch_event,
            envelope=envelope,
            coalescing_thread_id=coalescing_thread_id,
            requester_user_id=requester_user_id,
            reservation_owner=reservation_owner,
            callback_source_kind=callback_source_kind,
        )

    async def _handle_edit_event(
        self,
        room: nio.MatrixRoom,
        prechecked_event: _PrecheckedEvent[nio.RoomMessageText],
        event_info: EventInfo,
        dispatch_timing: DispatchPipelineTiming | None,
    ) -> None:
        """Hand one edited user turn to the edit regenerator."""
        await self._append_live_event_with_timing(
            room.room_id,
            prechecked_event.event,
            event_info=event_info,
            dispatch_timing=dispatch_timing,
        )
        await self.deps.edit_regenerator.handle_message_edit(
            room,
            prechecked_event.event,
            event_info,
            prechecked_event.requester_user_id,
        )

    async def _notify_command_target_not_ready(
        self,
        room: nio.MatrixRoom,
        event: nio.RoomMessageText,
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
                recovered_response_event_id=response_event_id,
            )
            self.deps.turn_store.record_responded_turn(
                canonicalize_turn_record(pending_turn, response_event_id=response_event_id),
            )
            return True
        await self.deps.visible_responses.settle_source_events_ignored(TurnRecord.create([event.event_id]))
        return True

    async def _dispatch_command_control_input(
        self,
        *,
        room: nio.MatrixRoom,
        dispatch_event: TextDispatchEvent,
        envelope: MessageEnvelope,
        coalescing_thread_id: str | None,
        requester_user_id: str,
    ) -> None:
        """Dispatch one command as a control input without entering the coalescing gate."""
        pending_event = PendingEvent(
            event=dispatch_event,
            room=room,
            requester_user_id=requester_user_id,
            source_kind=envelope.source_kind,
            dispatch_policy_source_kind=envelope.dispatch_policy_source_kind,
            hook_source=envelope.hook_source,
            message_received_depth=envelope.message_received_depth,
            trust_internal_payload_metadata=self.deps.ingress.should_trust_internal_payload_metadata(dispatch_event),
        )
        batch = build_coalesced_batch(
            CoalescingKey(room.room_id, coalescing_thread_id, requester_user_id),
            [pending_event],
        )
        handoff = build_dispatch_handoff(batch)
        handled_turn = TurnRecord.create(
            handoff.source_event_ids,
            source_event_prompts=dict(handoff.source_event_prompts),
            source_event_metadata=dict(handoff.source_event_metadata) if len(handoff.source_event_ids) > 1 else None,
        )
        await self._dispatch_handoff(handoff, handled_turn=handled_turn)

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
        callback_source_kind: str | None = None,
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
        pending_event = PendingEvent(
            event=event,
            room=room,
            requester_user_id=requester_user_id,
            source_kind=source_kind,
            dispatch_policy_source_kind=dispatch_policy_source_kind,
            hook_source=hook_source,
            message_received_depth=message_received_depth,
            trust_internal_payload_metadata=resolved_trust_internal_payload_metadata,
            discovery_event_id=self.deps.ingress.router_relay_original_event_id(event),
            callback_source_kind=callback_source_kind,
            turn_dispatch_recovery=turn_dispatch_recovery_active(),
            dispatch_metadata=dispatch_metadata,
        )
        if turn_claim is not None:
            reservation_owner.pending_turn_claim = None
        await reservation_owner.admit(
            resolved_key,
            source_event_id=event.event_id,
            source_kind=source_kind,
            callback_source_kind=callback_source_kind,
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
        event: TextDispatchEvent,
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
                mode=ThreadReadMode.DISPATCH_SNAPSHOT,
                payload_metadata=payload_metadata,
                caller_label="dispatch_command_context",
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

    async def handle_interactive_selection(
        self,
        room: nio.MatrixRoom,
        *,
        selection: interactive.InteractiveSelection,
        user_id: str,
        source_event_id: str,
    ) -> None:
        """Own claim settlement around one validated interactive selection."""
        try:
            await self._execute_interactive_selection(
                room,
                selection=selection,
                user_id=user_id,
                source_event_id=source_event_id,
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
                caller_label="interactive_selection",
            )
            if selection.thread_id
            else []
        )
        response_target = self.deps.resolver.build_message_target(
            room_id=room.room_id,
            thread_id=selection.thread_id,
            reply_to_event_id=selection.question_event_id,
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
        pending_turn = await asyncio.to_thread(
            self.deps.turn_store.record_pending_turn,
            selection_handled_turn,
        )
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
            )
            if response_event_id is not None:
                self.deps.turn_store.record_responded_turn(
                    canonicalize_turn_record(selection_handled_turn, response_event_id=response_event_id),
                )
                await self._require_durable_interactive_selection(selection.question_event_id, source_event_id)
                return
            raise self._interactive_selection_retry_error(source_event_id) from error
        selection_attachment_ids = tuple(selection_payload.attachment_ids or ())
        selection_matrix_run_metadata = self.deps.turn_store.build_run_metadata(selection_handled_turn)
        registry = entity_identity_registry(self.deps.runtime.config, self.deps.runtime_paths)
        response_envelope = MessageEnvelope(
            source_event_id=source_event_id,
            target=response_target,
            body=f"The user selected: {selection.selected_value}",
            attachment_ids=selection_attachment_ids,
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
            self.deps.turn_store.record_responded_turn(
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
        return any(
            await asyncio.gather(
                *(
                    asyncio.to_thread(self.deps.turn_store.is_durably_handled, event_id)
                    for event_id in {question_event_id, source_event_id}
                ),
            ),
        )

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

    def _router_handoff_extra_content(
        self,
        *,
        event: DispatchEvent,
        extra_content: dict[str, Any] | None,
        suggested_entity: str | None,
        requester_user_id: str,
        thread_event_id: str | None,
    ) -> dict[str, Any]:
        """Return router relay metadata normalized through the handoff origin policy."""
        routed_extra_content = dict(extra_content) if extra_content is not None else {}
        routed_extra_content.pop(PER_FIRE_THREAD_ROOT_EVENT_ID_KEY, None)
        routed_extra_content.pop(PER_FIRE_THREAD_ROOT_KEY, None)
        inherited_original_sender = routed_extra_content.get(ORIGINAL_SENDER_KEY)
        inherited_original_sender = inherited_original_sender if isinstance(inherited_original_sender, str) else None
        handoff_original_sender = original_sender_for_router_handoff(
            target_entity_name=suggested_entity,
            requester_id=requester_user_id,
            requester_entity_name=self.deps.ingress.managed_entity_name_for_sender(requester_user_id),
            inherited_original_sender=inherited_original_sender,
            inherited_original_sender_entity_name=(
                self.deps.ingress.managed_entity_name_for_sender(inherited_original_sender)
                if inherited_original_sender is not None
                else None
            ),
        )
        routed_extra_content.pop(ORIGINAL_SENDER_KEY, None)
        if handoff_original_sender is not None:
            routed_extra_content[SOURCE_KIND_KEY] = TRUSTED_INTERNAL_RELAY_SOURCE_KIND
            routed_extra_content[ORIGINAL_SENDER_KEY] = handoff_original_sender
        event_content = event.source.get("content") if isinstance(event.source, dict) else None
        if (
            self.deps.ingress.sender_is_trusted_for_ingress_metadata(event.sender)
            and isinstance(event_content, dict)
            and content_owns_per_fire_thread_root(event_content)
        ):
            routed_extra_content[PER_FIRE_THREAD_ROOT_KEY] = True
            if thread_event_id is not None:
                routed_extra_content[PER_FIRE_THREAD_ROOT_EVENT_ID_KEY] = thread_event_id
        return routed_extra_content

    async def _router_handoff_with_attachments(
        self,
        *,
        room_id: str,
        thread_id: str | None,
        event: DispatchEvent,
        media_events: Sequence[MediaDispatchEvent],
        extra_content: dict[str, Any],
    ) -> dict[str, Any]:
        """Register routed media and return handoff metadata with attachment IDs."""
        routed_media_events = list(media_events)
        if not routed_media_events and is_matrix_media_dispatch_event(event):
            routed_media_events.append(event)
        if not routed_media_events:
            return extra_content
        routed_attachment_ids = merge_attachment_ids(
            parse_attachment_ids_from_event_source({"content": extra_content}),
            [
                attachment_id
                for attachment_id in await asyncio.gather(
                    *(
                        self.deps.normalizer.register_routed_attachment(
                            room_id=room_id,
                            thread_id=thread_id,
                            event=media_event,
                        )
                        for media_event in routed_media_events
                    ),
                )
                if attachment_id is not None
            ],
        )
        if routed_attachment_ids:
            extra_content[ATTACHMENT_IDS_KEY] = routed_attachment_ids
        else:
            extra_content.pop(ATTACHMENT_IDS_KEY, None)
        return extra_content

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
        """Run one explicit router relay from the turn controller."""
        assert self.deps.agent_name == ROUTER_AGENT_NAME

        permission_sender_id = requester_user_id
        responder_candidates = await self.deps.turn_policy.responder_candidates_for_room(
            room,
            permission_sender_id,
        )
        if not responder_candidates:
            self.deps.logger.debug(
                "No responders to route to in this room for sender",
                sender=permission_sender_id,
            )
            await self.deps.visible_responses.settle_source_events_ignored(
                handled_turn or TurnRecord.create([event.event_id]),
            )
            return

        with bound_log_context(room_id=room.room_id, thread_id=thread_id):
            if len(responder_candidates) == 1:
                suggested_entity = self.deps.ingress.managed_entity_name_for_sender(responder_candidates[0].full_id)
                self.deps.logger.info("Handling deterministic routing", event_id=event.event_id)
            else:
                self.deps.logger.info("Handling AI routing", event_id=event.event_id)

                routing_text = message or event.body
                suggested_entity = await suggest_responder_for_message(
                    routing_text,
                    responder_candidates,
                    self.deps.runtime.config,
                    self.deps.runtime_paths,
                    thread_history,
                )

        target_resolution = _resolve_router_target(
            self.deps.runtime.orchestrator,
            suggested_entity,
            scheduled_prompt,
        )
        if target_resolution is None:
            return
        selected_entity, suggested_entity, response_text = (
            target_resolution.selected_entity,
            target_resolution.suggested_entity,
            target_resolution.response_text,
        )
        if target_resolution.target_unavailable:
            with bound_log_context(room_id=room.room_id, thread_id=thread_id):
                self.deps.logger.warning("Router failed to determine entity")

        target_thread_mode = (
            self.deps.runtime.config.get_entity_thread_mode(
                suggested_entity,
                self.deps.runtime_paths,
                room_id=room.room_id,
            )
            if suggested_entity
            else None
        )
        resolved_target = self.deps.resolver.build_message_target(
            room_id=room.room_id,
            thread_id=thread_id,
            reply_to_event_id=event.event_id,
            event_source=event.source,
            thread_mode_override=target_thread_mode,
        )
        thread_event_id = resolved_target.resolved_thread_id
        routed_extra_content = self._router_handoff_extra_content(
            event=event,
            extra_content=extra_content,
            suggested_entity=suggested_entity,
            requester_user_id=requester_user_id,
            thread_event_id=thread_event_id,
        )
        routed_extra_content = await self._router_handoff_with_attachments(
            room_id=room.room_id,
            thread_id=thread_event_id,
            event=event,
            media_events=media_events or (),
            extra_content=routed_extra_content,
        )

        delivery_request = SendTextRequest(
            target=resolved_target,
            response_text=response_text,
            extra_content=routed_extra_content or None,
        )
        source_turn = handled_turn or TurnRecord.create([event.event_id])
        visible_echo_event_id = self.deps.turn_store.finalized_visible_echo_for_sources(
            source_turn.source_event_ids,
        )
        (
            tracked_handled_turn,
            recovered_response_event_id,
        ) = await self.deps.visible_responses.prepare_visible_delivery_turn(
            source_turn,
            requester_id=requester_user_id,
            correlation_id=event.event_id,
            target=resolved_target,
            excluded_event_ids=(visible_echo_event_id,) if visible_echo_event_id is not None else (),
        )
        if tracked_handled_turn is None:
            return
        if recovered_response_event_id is not None:
            self.deps.turn_store.record_responded_turn(
                canonicalize_turn_record(tracked_handled_turn, response_event_id=recovered_response_event_id),
            )
            return
        relay_delivery = await _send_router_relay_after_readiness_recheck(
            orchestrator=self.deps.runtime.orchestrator,
            delivery_gateway=self.deps.delivery_gateway,
            selected_entity=selected_entity,
            suggested_entity=suggested_entity,
            delivery_request=delivery_request,
        )
        if relay_delivery.deferred_for_recovery:
            return
        event_id = relay_delivery.event_id
        suggested_entity = relay_delivery.suggested_entity
        with bound_log_context(**resolved_target.log_context):
            if event_id:
                self.deps.logger.info("Routed to entity", suggested_entity=suggested_entity)
                await self.deps.visible_responses.record_pending_visible_response(tracked_handled_turn, event_id)
                self.deps.turn_store.record_responded_turn(
                    canonicalize_turn_record(tracked_handled_turn, response_event_id=event_id),
                )
            else:
                self.deps.logger.error("Failed to route to entity", entity=suggested_entity)
                msg = f"Failed to route to entity {suggested_entity!r}"
                raise RuntimeError(msg)

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
    ) -> str | None:
        """Convert dispatch setup failures into a visible terminal message."""
        error_text = get_user_friendly_error_message(error, self.deps.agent_name)
        terminal_extra_content = {STREAM_STATUS_KEY: STREAM_STATUS_COMPLETED}
        if existing_event_id is not None:
            edited = await self.deps.delivery_gateway.edit_text(
                EditTextRequest(
                    target=target,
                    event_id=existing_event_id,
                    new_text=error_text,
                    extra_content=terminal_extra_content,
                ),
            )
            if edited:
                return existing_event_id
        response_event_id = await self.deps.delivery_gateway.send_text(
            SendTextRequest(
                target=target,
                response_text=error_text,
                extra_content=terminal_extra_content,
            ),
        )
        if response_event_id is not None and on_visible_response is not None:
            await on_visible_response(response_event_id)
        return response_event_id

    def _build_response_settlement_callbacks(
        self,
        room: nio.MatrixRoom,
        *,
        source_event_id: str,
        handled_turn: TurnRecord,
    ) -> tuple[Callable[[], None], Callable[[str], None], Callable[[str, int], None]]:
        """Build callbacks for interrupted-turn recording and deferred handled recording."""

        def record_interrupted_turn() -> None:
            self.deps.interrupted_turn_rooms.register(source_event_id, room_id=room.room_id)

        def record_deferred_outcome(response_event_id: str) -> None:
            self.deps.turn_store.record_responded_turn(
                canonicalize_turn_record(handled_turn, response_event_id=response_event_id),
            )

        def record_user_stop(response_event_id: str, stop_receipt_order: int) -> None:
            self.deps.turn_store.record_turn_durably(
                with_user_stop(
                    handled_turn,
                    response_event_id,
                    stop_receipt_order,
                    delivery_settled=True,
                ),
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
        queued_notice_reservation: QueuedHumanNoticeReservation | None = None,
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
                    recovered_response_event_id=response_event_id,
                )
                self.deps.turn_store.record_responded_turn(
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
                    queued_notice_reservation=queued_notice_reservation,
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
                )
                self.deps.turn_store.record_responded_turn(
                    canonicalize_turn_record(handled_turn, response_event_id=response_event_id),
                )
                return
            if response_event_id is not None:
                self.deps.turn_store.record_responded_turn(
                    canonicalize_turn_record(handled_turn, response_event_id=response_event_id),
                )

    async def handle_coalesced_batch(self, batch: CoalescedBatch) -> None:
        """Dispatch one flushed batch through the normal text pipeline."""
        try:
            handoff = build_dispatch_handoff(batch)
        except BaseException:
            # Close-and-clear so the gate's segment owner cannot close the
            # same metadata a second time when this exception reaches it.
            close_pending_event_metadata_once(list(batch.pending_events))
            raise
        _consume_queued_notice_reservations_from_metadata(
            handoff.dispatch_metadata,
            target_key=self._queued_notice_target_key_for_handoff(handoff),
        )
        timing_scope = event_timing_scope(handoff.event.event_id)
        dispatch_timing = get_dispatch_pipeline_timing(handoff.event.source)
        if dispatch_timing is not None:
            dispatch_timing.mark("gate_exit")
        async with self.deps.resolver.turn_thread_cache_scope():
            dispatch_start = time.monotonic()
            source_metadata = dict(handoff.source_event_metadata)
            routed_aliases = tuple(filter(None, (item.discovery_event_id for item in source_metadata.values())))
            handled_turn = TurnRecord.create(
                handoff.source_event_ids,
                discovery_event_ids=routed_aliases,
                source_event_prompts=dict(handoff.source_event_prompts),
                source_event_metadata=source_metadata if len(handoff.source_event_ids) > 1 or routed_aliases else None,
            )
            close_pending_event_metadata_once(list(batch.pending_events))
            await self._dispatch_handoff(
                handoff,
                handled_turn=handled_turn,
            )
            emit_elapsed_timing(
                "coalescing.handle_batch.dispatch_text_message",
                dispatch_start,
                source_event_count=len(batch.source_event_ids),
                timing_scope=timing_scope,
            )

    def _queued_notice_target_key_for_handoff(self, handoff: DispatchHandoff) -> tuple[str, str | None]:
        coalescing_key = handoff.ingress.coalescing_key
        if coalescing_key is None:
            return (handoff.room.room_id, None)
        context_event = _room_level_context_event(handoff.event) if coalescing_key.thread_id is None else handoff.event
        target = self.deps.resolver.build_message_target(
            room_id=handoff.room.room_id,
            thread_id=coalescing_key.thread_id,
            reply_to_event_id=handoff.event.event_id,
            event_source=context_event.source,
        )
        return (target.room_id, target.resolved_thread_id)

    async def _dispatch_handoff(
        self,
        handoff: DispatchHandoff,
        *,
        handled_turn: TurnRecord,
    ) -> None:
        """Dispatch one coalesced handoff and own opaque metadata cleanup."""
        await self._dispatch_text_message(
            handoff.room,
            handoff.event,
            handoff.requester_user_id,
            media_events=list(handoff.media_events) or None,
            handled_turn=handled_turn,
            ingress_metadata=handoff.ingress,
            payload_metadata=handoff.payload,
            trust_hydrated_internal_metadata=handoff.trust_hydrated_internal_metadata,
            current_prompt_is_structured=handoff.current_prompt_is_structured,
        )

    async def _claim_live_turn(
        self,
        turn_claim: TurnRecord,
        *,
        source_event_id: str,
    ) -> TurnRecord | TurnDispatchOutcome:
        """Claim one live source or return its explicit competing-owner outcome."""
        if self.deps.turn_store.try_claim_turn(turn_claim):
            return turn_claim

        await self.deps.turn_store.wait_for_turn_settled(turn_claim.indexed_event_ids)
        if await asyncio.to_thread(self.deps.turn_store.is_durably_handled, source_event_id):
            return TurnDispatchOutcome.DEFERRED
        if self.deps.turn_store.try_claim_turn(turn_claim):
            return turn_claim
        # A settled discovery-alias owner or a newer competing claimant owns
        # this duplicate semantic turn.
        return TurnDispatchOutcome.INTENTIONALLY_IGNORED

    async def handle_text_event(
        self,
        room: nio.MatrixRoom,
        event: nio.RoomMessageText,
        *,
        receipt_time: float | None = None,
        reservation_owner: _PromptIngressReservationOwner | None = None,
    ) -> TurnDispatchOutcome:
        """Handle one inbound text event."""
        async with self.deps.resolver.turn_thread_cache_scope():
            return await self._handle_message_inner(
                room,
                event,
                receipt_time=receipt_time,
                reservation_owner=reservation_owner,
            )

    async def _handle_message_inner(
        self,
        room: nio.MatrixRoom,
        event: nio.RoomMessageText,
        *,
        receipt_time: float | None = None,
        reservation_owner: _PromptIngressReservationOwner | None = None,
    ) -> TurnDispatchOutcome:
        """Handle one text message inside the per-turn conversation lookup scope."""
        event_info = EventInfo.from_event(event.source)
        if not isinstance(event.body, str):
            return TurnDispatchOutcome.INTENTIONALLY_IGNORED
        event_content = event.source.get("content") if isinstance(event.source, dict) else None
        if isinstance(event_content, dict) and event_content.get(STREAM_STATUS_KEY) in {
            STREAM_STATUS_PENDING,
            STREAM_STATUS_STREAMING,
        }:
            return TurnDispatchOutcome.INTENTIONALLY_IGNORED
        prechecked_event = self._precheck_dispatch_event(room, event, is_edit=event_info.is_edit)
        if prechecked_event is None:
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
                await self._handle_edit_event(room, prechecked_event, event_info, dispatch_timing)
                return TurnDispatchOutcome.INTENTIONALLY_IGNORED
            routed_alias = self.deps.ingress.router_relay_original_event_id(event)
            claim_aliases = (routed_alias,) if routed_alias else ()
            pending_turn = TurnRecord.create(
                [event.event_id],
                discovery_event_ids=claim_aliases,
                completed=False,
            )
            turn_claim = await self._claim_live_turn(pending_turn, source_event_id=event.event_id)
            if isinstance(turn_claim, TurnDispatchOutcome):
                return turn_claim
            reservation_owner.pending_turn_claim = turn_claim
            outcome = await self._ingest_live_text_event(
                room,
                prechecked_event,
                event_info=event_info,
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
        prechecked_event: _PrecheckedEvent[nio.RoomMessageText],
        *,
        event_info: EventInfo,
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
        await self._append_live_event_with_timing(
            room.room_id,
            event,
            event_info=event_info,
            dispatch_timing=dispatch_timing,
        )
        prepared_event = await self._resolve_text_event_with_ingress_timing(
            event,
            dispatch_timing=dispatch_timing,
        )
        return await self._dispatch_prepared_text_like_ingress(
            room=room,
            prepared_event=prepared_event,
            dispatch_event=event,
            requester_user_id=prechecked_event.requester_user_id,
            reservation_owner=reservation_owner,
            coalescing_thread_id=ingress_thread_id,
        )

    async def _dispatch_text_message(
        self,
        room: nio.MatrixRoom,
        event: TextDispatchEvent | _PrecheckedTextDispatchEvent,
        requester_user_id: str | None = None,
        *,
        media_events: list[MediaDispatchEvent] | None = None,
        handled_turn: TurnRecord | None = None,
        queued_notice_reservation: QueuedHumanNoticeReservation | None = None,
        ingress_metadata: DispatchIngressMetadata | None = None,
        payload_metadata: DispatchPayloadMetadata | None = None,
        trust_hydrated_internal_metadata: bool | None = None,
        current_prompt_is_structured: bool = False,
    ) -> None:
        """Run the normal text or command dispatch pipeline for a prepared text event."""
        raw_event: TextDispatchEvent
        if isinstance(event, _PrecheckedEvent):
            requester_user_id = event.requester_user_id
            raw_event = cast("TextDispatchEvent", event.event)
        else:
            raw_event = event
        if requester_user_id is None:
            msg = "requester_user_id is required when dispatching a raw event"
            raise TypeError(msg)
        await dispatch_text_message(
            self,
            room,
            raw_event,
            requester_user_id,
            command_executor=self.deps.command_executor,
            visible_responses=self.deps.visible_responses,
            media_events=media_events,
            handled_turn=handled_turn,
            queued_notice_reservation=queued_notice_reservation,
            ingress_metadata=ingress_metadata,
            payload_metadata=payload_metadata,
            trust_hydrated_internal_metadata=trust_hydrated_internal_metadata,
            current_prompt_is_structured=current_prompt_is_structured,
        )

    async def handle_media_event(
        self,
        room: nio.MatrixRoom,
        event: MatrixMediaEvent,
        *,
        receipt_time: float | None = None,
    ) -> TurnDispatchOutcome:
        """Handle one inbound media event."""
        async with self.deps.resolver.turn_thread_cache_scope():
            return await self._handle_media_message_inner(room, event, receipt_time=receipt_time)

    async def _handle_media_message_inner(
        self,
        room: nio.MatrixRoom,
        event: MatrixMediaEvent,
        *,
        receipt_time: float | None = None,
    ) -> TurnDispatchOutcome:
        """Handle one media event inside the per-turn conversation lookup scope."""
        prechecked_event = self._precheck_dispatch_event(room, event)
        if prechecked_event is None:
            return TurnDispatchOutcome.INTENTIONALLY_IGNORED
        dispatch_timing = create_dispatch_pipeline_timing(
            event_id=prechecked_event.event.event_id,
            room_id=room.room_id,
        )
        attach_dispatch_pipeline_timing(prechecked_event.event.source, dispatch_timing)
        event_info = EventInfo.from_event(prechecked_event.event.source)
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
                    event_info=event_info,
                    dispatch_timing=dispatch_timing,
                    reservation_owner=reservation_owner,
                    turn_claim=turn_claim,
                )
                reservation_owner.pending_turn_claim = None
                dispatch_outcome = TurnDispatchOutcome.DEFERRED
            else:
                # Prime transitive ancestor lookups before writing advisory cache membership.
                coalescing_thread_id = await self.deps.resolver.coalescing_thread_id(room, prechecked_event.event)
                await self._append_live_event_with_timing(
                    room.room_id,
                    prechecked_event.event,
                    event_info=event_info,
                    dispatch_timing=dispatch_timing,
                )

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
        event_info: EventInfo,
        dispatch_timing: DispatchPipelineTiming | None,
        reservation_owner: _PromptIngressReservationOwner,
        turn_claim: TurnRecord,
    ) -> None:
        """Resolve the audio conversation key once, then defer voice normalization."""
        event = prechecked_event.event

        voice_target, admission_key = await self._resolve_ready_voice_target(
            room,
            event,
            event_info=event_info,
            requester_user_id=prechecked_event.requester_user_id,
            dispatch_timing=dispatch_timing,
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
        event_info: EventInfo,
        requester_user_id: str,
        dispatch_timing: DispatchPipelineTiming | None,
    ) -> tuple[MessageTarget, CoalescingKey]:
        await self._append_live_event_with_timing(
            room.room_id,
            event,
            event_info=event_info,
            dispatch_timing=dispatch_timing,
        )
        coalescing_thread_id = await self.deps.resolver.coalescing_thread_id(room, event)
        voice_target = self.deps.resolver.build_message_target(
            room_id=room.room_id,
            thread_id=coalescing_thread_id,
            reply_to_event_id=event.event_id,
            event_source=event.source,
        )
        return voice_target, CoalescingKey(room.room_id, coalescing_thread_id, requester_user_id)

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
                    event=normalized_event,
                    room=room,
                    source_kind=envelope.source_kind,
                    requester_user_id=prechecked_event.requester_user_id,
                    dispatch_policy_source_kind=envelope.dispatch_policy_source_kind,
                    hook_source=envelope.hook_source,
                    message_received_depth=envelope.message_received_depth,
                    trust_internal_payload_metadata=True,
                    turn_dispatch_recovery=turn_dispatch_recovery_active(),
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
    ) -> PreparedTextEvent:
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
                    event=fallback_event,
                    room=room,
                    source_kind=VOICE_SOURCE_KIND,
                    requester_user_id=requester_user_id,
                    dispatch_policy_source_kind=dispatch_policy_source_kind,
                    hook_source=hook_source,
                    message_received_depth=message_received_depth,
                    trust_internal_payload_metadata=True,
                    turn_dispatch_recovery=turn_dispatch_recovery_active(),
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
    ) -> tuple[PreparedTextEvent, str | None]:
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
            dispatch_event=prepared_text_event,
            requester_user_id=prechecked_event.requester_user_id,
            reservation_owner=reservation_owner,
            coalescing_thread_id=coalescing_thread_id,
            callback_source_kind=MEDIA_SOURCE_KIND,
        )
