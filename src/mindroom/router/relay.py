"""Routing a turn that names nobody: the room's steward selects a responder and sends it a hidden route event."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Protocol

from mindroom.attachments.ids import merge_attachment_ids
from mindroom.attachments.storage import parse_attachment_ids_from_event_source
from mindroom.constants import (
    ATTACHMENT_IDS_KEY,
    ORIGINAL_SENDER_KEY,
    PER_FIRE_THREAD_ROOT_EVENT_ID_KEY,
    PER_FIRE_THREAD_ROOT_KEY,
    ROUTE_EVENT_TYPE,
    SOURCE_KIND_KEY,
)
from mindroom.dispatch_handoff import PreparedIngress
from mindroom.dispatch_source import TRUSTED_INTERNAL_RELAY_SOURCE_KIND, content_owns_per_fire_thread_root
from mindroom.entity_resolution import entity_identity_registry
from mindroom.logging_config import bound_log_context
from mindroom.matrix.media import is_matrix_media_dispatch_event
from mindroom.routing import suggest_responder_for_message
from mindroom.turn_origin import original_sender_for_router_relay
from mindroom.turn_record import TurnRecord, canonicalize_turn_record

if TYPE_CHECKING:
    from collections.abc import Sequence

    import nio
    from structlog.stdlib import BoundLogger

    from mindroom.bot_runtime_view import BotRuntimeView
    from mindroom.constants import RuntimePaths
    from mindroom.conversation_resolver import ConversationResolver
    from mindroom.delivery_gateway import DeliveryGateway
    from mindroom.dispatch_handoff import DispatchEvent, MediaDispatchEvent
    from mindroom.inbound_turn_normalizer import InboundTurnNormalizer
    from mindroom.ingress_validation import IngressValidator
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.turn_policy import TurnPolicy
    from mindroom.turn_store import TurnStore
    from mindroom.visible_response_reconciliation import VisibleResponseReconciler


class _RouterRelaySupport(Protocol):
    """Collaborators required by router relay execution."""

    runtime: BotRuntimeView
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


async def _select_responder(
    deps: _RouterRelaySupport,
    room: nio.MatrixRoom,
    event: DispatchEvent,
    thread_history: Sequence[ResolvedVisibleMessage],
    thread_id: str | None,
    message: str | None,
    requester_user_id: str,
) -> str | None:
    """Return the entity selected to answer one turn, or None when no candidate fits it."""
    responder_candidates = await deps.turn_policy.responder_candidates_for_room(room, requester_user_id)
    if not responder_candidates:
        deps.logger.debug("No responders to route to in this room for sender", sender=requester_user_id)
        return None
    with bound_log_context(room_id=room.room_id, thread_id=thread_id):
        if len(responder_candidates) > 1:
            deps.logger.info("Handling AI routing", event_id=event.event_id)
            selection = await suggest_responder_for_message(
                message or event.body,
                responder_candidates,
                deps.runtime.config,
                deps.runtime_paths,
                thread_history,
                room_id=room.room_id,
                thread_id=thread_id,
            )
            if selection is not None:
                return selection.entity_name
            # A failed selection must not leave the person without an answer.
            deps.logger.warning("Routing failed; the candidate with the lowest Matrix ID answers")
        else:
            deps.logger.info("Handling deterministic routing", event_id=event.event_id)
        fallback = min(responder_candidates, key=lambda candidate: candidate.full_id)
        return deps.ingress.managed_entity_name_for_sender(fallback.full_id)


async def _responder_ready(deps: _RouterRelaySupport, entity_name: str, turn: TurnRecord) -> bool:
    """Return whether the selected responder can take the turn now.

    A turn whose responder is still starting stays pending, and is routed again once that responder is ready.
    One whose responder stopped meanwhile settles unanswered.
    """
    readiness = deps.runtime.orchestrator.entity_first_sync_complete(entity_name)
    if readiness is None:
        deps.logger.warning("Selected responder is not running", entity=entity_name)
        await deps.visible_responses.settle_source_events_ignored(turn)
    return readiness is True


async def _still_steward(deps: _RouterRelaySupport, room_id: str, turn: TurnRecord) -> bool:
    """Return whether this bot is still the room's steward, which it was when it planned to route.

    One that lost the room while it selected drops the turn: the new steward routes its own copy.
    """
    if deps.turn_policy.is_room_steward(room_id):
        return True
    deps.logger.info("No longer the room's steward; dropping the turn it was routing")
    await deps.visible_responses.settle_source_events_ignored(turn)
    return False


def _router_handoff_extra_content(
    deps: _RouterRelaySupport,
    *,
    event: DispatchEvent,
    extra_content: dict[str, Any] | None,
    requester_user_id: str,
    thread_event_id: str | None,
) -> dict[str, Any]:
    """Return router relay metadata normalized through the handoff origin policy."""
    routed_extra_content = dict(extra_content) if extra_content is not None else {}
    routed_extra_content.pop(PER_FIRE_THREAD_ROOT_EVENT_ID_KEY, None)
    routed_extra_content.pop(PER_FIRE_THREAD_ROOT_KEY, None)
    inherited_original_sender = routed_extra_content.get(ORIGINAL_SENDER_KEY)
    inherited_original_sender = inherited_original_sender if isinstance(inherited_original_sender, str) else None
    handoff_original_sender = original_sender_for_router_relay(
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
    deps: _RouterRelaySupport,
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


async def execute_router_relay(  # noqa: PLR0911
    deps: _RouterRelaySupport,
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
) -> bool:
    """Route one turn as the room's steward; return whether the steward itself was selected and answers it.

    Another responder is told by a hidden `io.mindroom.route` event, which no client shows.
    Nobody answers a turn that no candidate fits.
    """
    source_turn = handled_turn or TurnRecord.create([event.event_id])
    suggested_entity = await _select_responder(
        deps,
        room,
        event,
        thread_history,
        thread_id,
        message,
        requester_user_id,
    )
    if suggested_entity is None:
        await deps.visible_responses.settle_source_events_ignored(source_turn)
        return False
    if suggested_entity == deps.agent_name:
        return await _still_steward(deps, room.room_id, source_turn)
    if not await _responder_ready(deps, suggested_entity, source_turn):
        return False

    resolved_target = deps.resolver.build_message_target(
        room_id=room.room_id,
        thread_id=thread_id,
        reply_to_event_id=event.event_id,
        event_source=event.source,
        thread_mode_override=deps.runtime.config.get_entity_thread_mode(
            suggested_entity,
            deps.runtime_paths,
            room_id=room.room_id,
        ),
    )
    thread_event_id = resolved_target.resolved_thread_id
    routed_extra_content = _router_handoff_extra_content(
        deps,
        event=event,
        extra_content=extra_content,
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

    (
        tracked_handled_turn,
        recovered_response_event_id,
    ) = await deps.visible_responses.prepare_visible_delivery_turn(
        source_turn,
        requester_id=requester_user_id,
        target=resolved_target,
        # The router's own voice echoes are publication, never the relay this turn still owes.
        excluded_event_ids=await deps.turn_store.finalized_visible_echoes(source_turn.source_event_ids),
    )
    if tracked_handled_turn is None:
        return False
    if recovered_response_event_id is not None:
        await deps.turn_store.record_responded_turn(
            canonicalize_turn_record(tracked_handled_turn, response_event_id=recovered_response_event_id),
        )
        return False
    # The responder may have stopped or restarted, and this bot may have lost the room, while the route was prepared.
    if not await _responder_ready(deps, suggested_entity, tracked_handled_turn) or not await _still_steward(
        deps,
        room.room_id,
        tracked_handled_turn,
    ):
        return False
    route = {
        "entity": entity_identity_registry(deps.runtime.config, deps.runtime_paths)
        .current_id(suggested_entity)
        .full_id,
        "event_id": event.event_id,
    }
    if thread_event_id is not None:
        route["thread_id"] = thread_event_id
    content: dict[str, Any] = {**routed_extra_content, ROUTE_EVENT_TYPE: route}
    if scheduled_prompt is not None:
        # A scheduled task's prompt may be in no visible message, so its route carries it.
        content["body"] = scheduled_prompt
    event_id = await deps.delivery_gateway.send_route(room.room_id, content)
    with bound_log_context(**resolved_target.log_context):
        if event_id is None:
            deps.logger.error("Failed to route to entity", entity=suggested_entity)
            msg = f"Failed to route to entity {suggested_entity!r}"
            raise RuntimeError(msg)
        deps.logger.info("Routed to entity", suggested_entity=suggested_entity)
        # The route is not an outbox delivery, so a replay before the turn is recorded answered adopts it from here.
        await deps.turn_store.record_placeholder(tracked_handled_turn, event_id)
        await deps.turn_store.record_responded_turn(
            canonicalize_turn_record(tracked_handled_turn, response_event_id=event_id),
        )
    return False
