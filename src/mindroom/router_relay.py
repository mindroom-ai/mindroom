"""Router relay delivery: target readiness, handoff metadata, and visible delivery."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from mindroom.attachment_ids import merge_attachment_ids
from mindroom.attachments import parse_attachment_ids_from_event_source
from mindroom.constants import (
    ATTACHMENT_IDS_KEY,
    ORIGINAL_SENDER_KEY,
    PER_FIRE_THREAD_ROOT_EVENT_ID_KEY,
    PER_FIRE_THREAD_ROOT_KEY,
    ROUTER_AGENT_NAME,
    SOURCE_KIND_KEY,
)
from mindroom.delivery_gateway import SendTextRequest
from mindroom.dispatch_handoff import PreparedIngress
from mindroom.dispatch_recovery_context import turn_dispatch_recovery_active
from mindroom.dispatch_source import TRUSTED_INTERNAL_RELAY_SOURCE_KIND, content_owns_per_fire_thread_root
from mindroom.handled_turns import TurnRecord
from mindroom.logging_config import bound_log_context
from mindroom.matrix.media import is_matrix_media_dispatch_event
from mindroom.routing import suggest_responder_for_message
from mindroom.turn_origin import original_sender_for_router_handoff
from mindroom.turn_record import canonicalize_turn_record

if TYPE_CHECKING:
    from collections.abc import Sequence

    import nio
    from structlog.stdlib import BoundLogger

    from mindroom.constants import RuntimePaths
    from mindroom.conversation_resolver import ConversationResolver
    from mindroom.delivery_gateway import DeliveryGateway
    from mindroom.dispatch_handoff import DispatchEvent, MediaDispatchEvent
    from mindroom.inbound_turn_normalizer import InboundTurnNormalizer
    from mindroom.ingress_validation import IngressValidator
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.runtime_protocols import OrchestratorRuntime, SupportsClientConfigOrchestrator
    from mindroom.turn_policy import TurnPolicy
    from mindroom.turn_store import TurnStore
    from mindroom.visible_response_reconciliation import VisibleResponseReconciler

_ROUTER_TARGET_STARTING_TEXT = "That agent is still starting. Please try again shortly."
_ROUTER_TARGET_UNAVAILABLE_TEXT = (
    "⚠️ I couldn't determine which agent or team should help with this. "
    "Please try mentioning an agent or team directly with @ or rephrase your request."
)


@dataclass(frozen=True)
class RouterRelayDeps:
    """Explicit collaborators for one router relay execution."""

    runtime: SupportsClientConfigOrchestrator
    runtime_paths: RuntimePaths
    logger: BoundLogger
    agent_name: str
    turn_policy: TurnPolicy
    ingress: IngressValidator
    resolver: ConversationResolver
    turn_store: TurnStore
    visible_responses: VisibleResponseReconciler
    delivery_gateway: DeliveryGateway
    normalizer: InboundTurnNormalizer


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


def _router_handoff_extra_content(
    deps: RouterRelayDeps,
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
        requester_entity_name=deps.ingress.managed_entity_name_for_sender(requester_user_id),
        inherited_original_sender=inherited_original_sender,
        inherited_original_sender_entity_name=(
            deps.ingress.managed_entity_name_for_sender(inherited_original_sender)
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
        deps.ingress.sender_is_trusted_for_ingress_metadata(event.sender)
        and isinstance(event_content, dict)
        and content_owns_per_fire_thread_root(event_content)
    ):
        routed_extra_content[PER_FIRE_THREAD_ROOT_KEY] = True
        if thread_event_id is not None:
            routed_extra_content[PER_FIRE_THREAD_ROOT_EVENT_ID_KEY] = thread_event_id
    return routed_extra_content


async def _router_handoff_with_attachments(
    deps: RouterRelayDeps,
    *,
    room_id: str,
    thread_id: str | None,
    event: DispatchEvent,
    media_events: Sequence[MediaDispatchEvent],
    extra_content: dict[str, Any],
) -> dict[str, Any]:
    """Register routed media and return handoff metadata with attachment IDs."""
    routed_media_events = list(media_events)
    if not routed_media_events:
        if isinstance(event, PreparedIngress) and event.raw_event is not None:
            routed_media_events.append(event.raw_event)
        elif is_matrix_media_dispatch_event(event):
            routed_media_events.append(event)
    if not routed_media_events:
        return extra_content
    routed_attachment_ids = merge_attachment_ids(
        parse_attachment_ids_from_event_source({"content": extra_content}),
        [
            attachment_id
            for attachment_id in await asyncio.gather(
                *(
                    deps.normalizer.register_routed_attachment(
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


async def execute_router_relay(
    deps: RouterRelayDeps,
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
    assert deps.agent_name == ROUTER_AGENT_NAME

    permission_sender_id = requester_user_id
    responder_candidates = await deps.turn_policy.responder_candidates_for_room(
        room,
        permission_sender_id,
    )
    if not responder_candidates:
        deps.logger.debug(
            "No responders to route to in this room for sender",
            sender=permission_sender_id,
        )
        await deps.visible_responses.settle_source_events_ignored(
            handled_turn or TurnRecord.create([event.event_id]),
        )
        return

    with bound_log_context(room_id=room.room_id, thread_id=thread_id):
        if len(responder_candidates) == 1:
            suggested_entity = deps.ingress.managed_entity_name_for_sender(responder_candidates[0].full_id)
            deps.logger.info("Handling deterministic routing", event_id=event.event_id)
        else:
            deps.logger.info("Handling AI routing", event_id=event.event_id)

            routing_text = message or event.body
            suggested_entity = await suggest_responder_for_message(
                routing_text,
                responder_candidates,
                deps.runtime.config,
                deps.runtime_paths,
                thread_history,
            )

    target_resolution = _resolve_router_target(
        deps.runtime.orchestrator,
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
            deps.logger.warning("Router failed to determine entity")

    target_thread_mode = (
        deps.runtime.config.get_entity_thread_mode(
            suggested_entity,
            deps.runtime_paths,
            room_id=room.room_id,
        )
        if suggested_entity
        else None
    )
    resolved_target = deps.resolver.build_message_target(
        room_id=room.room_id,
        thread_id=thread_id,
        reply_to_event_id=event.event_id,
        event_source=event.source,
        thread_mode_override=target_thread_mode,
    )
    thread_event_id = resolved_target.resolved_thread_id
    routed_extra_content = _router_handoff_extra_content(
        deps,
        event=event,
        extra_content=extra_content,
        suggested_entity=suggested_entity,
        requester_user_id=requester_user_id,
        thread_event_id=thread_event_id,
    )
    routed_extra_content = await _router_handoff_with_attachments(
        deps,
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
    visible_echo_event_id = deps.turn_store.finalized_visible_echo_for_sources(
        source_turn.source_event_ids,
    )
    (
        tracked_handled_turn,
        recovered_response_event_id,
    ) = await deps.visible_responses.prepare_visible_delivery_turn(
        source_turn,
        requester_id=requester_user_id,
        correlation_id=event.event_id,
        target=resolved_target,
        excluded_event_ids=(visible_echo_event_id,) if visible_echo_event_id is not None else (),
    )
    if tracked_handled_turn is None:
        return
    if recovered_response_event_id is not None:
        await deps.turn_store.record_responded_turn(
            canonicalize_turn_record(tracked_handled_turn, response_event_id=recovered_response_event_id),
        )
        return
    relay_delivery = await _send_router_relay_after_readiness_recheck(
        orchestrator=deps.runtime.orchestrator,
        delivery_gateway=deps.delivery_gateway,
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
            deps.logger.info("Routed to entity", suggested_entity=suggested_entity)
            await deps.visible_responses.record_pending_visible_response(tracked_handled_turn, event_id)
            await deps.turn_store.record_responded_turn(
                canonicalize_turn_record(tracked_handled_turn, response_event_id=event_id),
            )
        else:
            deps.logger.error("Failed to route to entity", entity=suggested_entity)
            msg = f"Failed to route to entity {suggested_entity!r}"
            raise RuntimeError(msg)
