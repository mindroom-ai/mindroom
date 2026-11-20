"""Routing a turn that names nobody: the room's steward selects a responder and sends it a hidden route event."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any, Protocol

from mindroom.attachments.ids import merge_attachment_ids
from mindroom.attachments.storage import parse_attachment_ids_from_event_source
from mindroom.constants import (
    ATTACHMENT_IDS_KEY,
    ORIGINAL_SENDER_KEY,
    PER_FIRE_THREAD_ROOT_EVENT_ID_KEY,
    PER_FIRE_THREAD_ROOT_KEY,
    ROUTE_EVENT_TYPE,
    ROUTE_VOICE_TRANSCRIPTS_KEY,
    ROUTE_VOICE_TRANSCRIPTS_LIMIT,
    SOURCE_KIND_KEY,
    VOICE_RAW_AUDIO_FALLBACK_KEY,
)
from mindroom.dispatch_handoff import PreparedIngress
from mindroom.dispatch_source import (
    SILENT_SCHEDULE_SOURCE_KIND,
    TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
    content_owns_per_fire_thread_root,
    source_kind_from_content,
)
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


async def _earlier_attempt_outcome(deps: _RouterRelaySupport, room_id: str, event_id: str) -> bool | None:
    """Return what an earlier attempt at this turn decided, or None when it decided nothing that still holds.

    A replay keeps that decision and selects nobody anew. True means that attempt selected this steward, which
    answers again. False means it sent its route and stopped before it recorded it, and the route is adopted.
    """
    earlier = await deps.turn_store.get_turn_record(event_id)
    if earlier is None or earlier.completed or earlier.response_owner is None:
        return None
    if earlier.history_scope is not None:
        return await _still_steward(deps, room_id, earlier)
    sent_route = earlier.response_event_id or await deps.delivery_gateway.sent_route(room_id, earlier.source_event_ids)
    if sent_route is None:
        return None
    await deps.turn_store.record_responded_turn(canonicalize_turn_record(earlier, response_event_id=sent_route))
    return False


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
    if not deps.ingress.sender_is_trusted_for_ingress_metadata(event.sender) or not isinstance(event_content, dict):
        return routed_extra_content
    if content_owns_per_fire_thread_root(event_content):
        routed_extra_content[PER_FIRE_THREAD_ROOT_KEY] = True
        if thread_event_id is not None:
            routed_extra_content[PER_FIRE_THREAD_ROOT_EVENT_ID_KEY] = thread_event_id
    if source_kind_from_content(event_content) == SILENT_SCHEDULE_SOURCE_KIND:
        # A silent schedule's route stays one, so its responder runs it as a trigger that named it: quiet, for
        # whoever the task runs as, and in reply to nothing where the hidden trigger stands in the room.
        routed_extra_content[SOURCE_KIND_KEY] = SILENT_SCHEDULE_SOURCE_KIND
        routed_extra_content[ORIGINAL_SENDER_KEY] = requester_user_id
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


async def _voice_transcripts(deps: _RouterRelaySupport, event_ids: list[str]) -> dict[str, dict[str, str]]:
    """Return route content holding each routed voice message's transcript, which the responder posts first."""
    transcripts = {
        event_id: voice.body
        for event_id in event_ids
        if (voice := await deps.turn_store.prepared_voice_for_source(event_id)) is not None
        and VOICE_RAW_AUDIO_FALLBACK_KEY not in voice.to_content()
    }
    carried = transcripts and len(json.dumps(transcripts)) <= ROUTE_VOICE_TRANSCRIPTS_LIMIT
    return {ROUTE_VOICE_TRANSCRIPTS_KEY: transcripts} if carried else {}


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
    replayed = await _earlier_attempt_outcome(deps, room.room_id, event.event_id)
    if replayed is not None:
        return replayed
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

    pending_turn = await deps.turn_store.record_pending_turn(
        deps.turn_store.attach_response_context(
            canonicalize_turn_record(source_turn, requester_id=requester_user_id),
            history_scope=None,
            conversation_target=resolved_target,
        ),
    )
    if pending_turn is None or await deps.visible_responses.settle_deleted_sources(pending_turn, room_id=room.room_id):
        return False
    tracked_handled_turn = pending_turn.record
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
        # Every message of the routed batch, the one the answer replies to last.
        "event_ids": [*(i for i in tracked_handled_turn.source_event_ids if i != event.event_id), event.event_id],
    }
    if thread_event_id is not None:
        route["thread_id"] = thread_event_id
    transcripts = await _voice_transcripts(deps, route["event_ids"])
    content: dict[str, Any] = {**routed_extra_content, ROUTE_EVENT_TYPE: route, **transcripts}
    if scheduled_prompt is not None or thread_event_id is None:
        # A responder in a thread reads the person's messages there. One that answers in the room reads no
        # conversation, and a scheduled task's prompt may be in no visible message, so their route carries the prompt.
        content["body"] = scheduled_prompt or message or event.body
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
