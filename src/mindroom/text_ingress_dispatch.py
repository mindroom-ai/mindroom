"""Text ingress dispatch path used by TurnController."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol

from mindroom.attachments import merge_attachment_ids, parse_attachment_ids_from_event_source
from mindroom.commands.parsing import command_parser
from mindroom.constants import (
    ATTACHMENT_IDS_KEY,
    ORIGINAL_SENDER_KEY,
    ROUTER_AGENT_NAME,
    VOICE_RAW_AUDIO_FALLBACK_KEY,
)
from mindroom.dispatch_handoff import (
    DispatchEvent,
    DispatchIngressMetadata,
    DispatchPayloadMetadata,
    MediaDispatchEvent,
    TextDispatchEvent,
    merge_payload_metadata,
    payload_metadata_from_source,
)
from mindroom.dispatch_source import VOICE_SOURCE_KIND, is_voice_event
from mindroom.handled_turns import HandledTurnState
from mindroom.inbound_turn_normalizer import (
    BatchMediaAttachmentRequest,
    DispatchPayload,
    DispatchPayloadWithAttachmentsRequest,
    TextNormalizationRequest,
)
from mindroom.matrix.media import is_audio_message_event, is_matrix_media_dispatch_event
from mindroom.matrix.rooms import is_dm_room
from mindroom.timing import (
    DispatchPipelineTiming,
    attach_dispatch_pipeline_timing,
    event_timing_scope,
    get_dispatch_pipeline_timing,
)
from mindroom.timing import timing_scope as timing_scope_context

if TYPE_CHECKING:
    from collections.abc import Sequence

    import nio

    from mindroom.commands.parsing import Command
    from mindroom.conversation_resolver import MessageContext
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.response_lifecycle import QueuedHumanNoticeReservation
    from mindroom.turn_controller import TurnController
    from mindroom.turn_policy import PreparedDispatch, ResponseAction


class _TurnPlan(Protocol):
    kind: Literal["ignore", "route", "respond"]
    response_action: ResponseAction | None
    router_message: str | None
    extra_content: dict[str, Any] | None
    media_events: list[MediaDispatchEvent] | None
    router_event: DispatchEvent | None
    ignore_reason: Literal["router"] | None


class _ReplayGuard(Protocol):
    degraded: bool
    history: Sequence[ResolvedVisibleMessage]
    thread_id: str | None


@dataclass(frozen=True)
class _PreparedTextDispatch:
    event: TextDispatchEvent
    payload_metadata: DispatchPayloadMetadata | None
    handled_turn: HandledTurnState
    command: Command | None
    dispatch: PreparedDispatch
    replay_guard: _ReplayGuard
    dispatch_started_at: float


async def dispatch_text_message(
    controller: TurnController,
    room: nio.MatrixRoom,
    raw_event: TextDispatchEvent,
    requester_user_id: str,
    *,
    media_events: list[MediaDispatchEvent] | None = None,
    handled_turn: HandledTurnState | None = None,
    queued_notice_reservation: QueuedHumanNoticeReservation | None = None,
    ingress_metadata: DispatchIngressMetadata | None = None,
    payload_metadata: DispatchPayloadMetadata | None = None,
    trust_hydrated_internal_metadata: bool | None = None,
) -> None:
    """Run the normal text or command dispatch pipeline for a prepared text event."""
    timing_scope_token = None
    try:
        dispatch_timing = get_dispatch_pipeline_timing(raw_event.source)
        prepared = await _prepare_text_dispatch(
            controller,
            room,
            raw_event,
            requester_user_id,
            media_events=media_events,
            handled_turn=handled_turn,
            ingress_metadata=ingress_metadata,
            payload_metadata=payload_metadata,
            trust_hydrated_internal_metadata=trust_hydrated_internal_metadata,
            dispatch_timing=dispatch_timing,
        )
        if prepared is None:
            return
        timing_scope_token = timing_scope_context.set(event_timing_scope(prepared.event.event_id))
        if await _blocked_before_plan(controller, room, prepared, requester_user_id=requester_user_id):
            return

        message_attachment_ids, trusted_attachment_ids, router_extra_content = _attachment_parts(
            prepared,
            media_events=media_events,
            requester_user_id=requester_user_id,
        )
        if dispatch_timing is not None:
            dispatch_timing.mark("dispatch_plan_start")
        plan = await controller.deps.turn_policy.plan_turn(
            room,
            prepared.event,
            prepared.dispatch,
            is_dm=await is_dm_room(controller._client(), room.room_id),
            has_active_response_for_target=controller.deps.response_runner.has_active_response_for_target,
            extra_content=router_extra_content or None,
            media_events=media_events,
            router_event=media_events[0]
            if media_events and len(prepared.handled_turn.source_event_ids) == 1
            else raw_event,
        )
        if dispatch_timing is not None:
            dispatch_timing.mark("dispatch_plan_ready")
        await _apply_turn_plan(
            controller,
            room,
            prepared,
            plan,
            message_attachment_ids=message_attachment_ids,
            trusted_attachment_ids=trusted_attachment_ids,
            media_events=media_events,
            queued_notice_reservation=queued_notice_reservation,
        )
    finally:
        if queued_notice_reservation is not None:
            queued_notice_reservation.cancel()
        if timing_scope_token is not None:
            timing_scope_context.reset(timing_scope_token)


async def _prepare_text_dispatch(
    controller: TurnController,
    room: nio.MatrixRoom,
    raw_event: TextDispatchEvent,
    requester_user_id: str,
    *,
    media_events: list[MediaDispatchEvent] | None,
    handled_turn: HandledTurnState | None,
    ingress_metadata: DispatchIngressMetadata | None,
    payload_metadata: DispatchPayloadMetadata | None,
    trust_hydrated_internal_metadata: bool | None,
    dispatch_timing: DispatchPipelineTiming | None,
) -> _PreparedTextDispatch | None:
    event = await controller.deps.normalizer.resolve_text_event(TextNormalizationRequest(event=raw_event))
    hydrated_payload_metadata = payload_metadata_from_source(
        event.source,
        trust_internal_metadata=(
            controller._should_trust_internal_payload_metadata(event)
            if trust_hydrated_internal_metadata is None
            else trust_hydrated_internal_metadata
        ),
    )
    payload_metadata = (
        hydrated_payload_metadata
        if payload_metadata is None
        else merge_payload_metadata(
            payload_metadata,
            hydrated_payload_metadata,
            trust_hydrated_internal_metadata=trust_hydrated_internal_metadata
            if trust_hydrated_internal_metadata is not None
            else controller._should_trust_internal_payload_metadata(event),
        )
    )
    attach_dispatch_pipeline_timing(event.source, dispatch_timing)
    if dispatch_timing is not None:
        dispatch_timing.mark("dispatch_start")
    dispatch_started_at = time.monotonic()

    if handled_turn is None:
        handled_turn = HandledTurnState.from_source_event_id(event.event_id)
    elif raw_event is not event and event.event_id in handled_turn.source_event_ids:
        refreshed_prompts = dict(handled_turn.source_event_prompts or {})
        refreshed_prompts[event.event_id] = event.body
        handled_turn = handled_turn.with_source_event_prompts(refreshed_prompts)

    command = _parsed_command_for_event(
        controller,
        event,
        media_events=media_events,
        ingress_metadata=ingress_metadata,
    )
    if dispatch_timing is not None:
        dispatch_timing.mark("dispatch_prepare_start")
    prepared = await controller._prepare_dispatch(
        room,
        event,
        requester_user_id,
        event_label="message",
        handled_turn=handled_turn,
        ingress_metadata=ingress_metadata,
        payload_metadata=payload_metadata,
        use_command_context=command is not None,
    )
    if dispatch_timing is not None:
        dispatch_timing.mark("dispatch_prepare_ready")
    if prepared is None:
        return None
    if command is not None and prepared.dispatch.envelope.source_kind == VOICE_SOURCE_KIND:
        command = None
    return _PreparedTextDispatch(
        event=event,
        payload_metadata=payload_metadata,
        handled_turn=handled_turn.with_request_context(
            requester_id=prepared.dispatch.requester_user_id,
            correlation_id=prepared.dispatch.correlation_id,
        ),
        command=command,
        dispatch=prepared.dispatch,
        replay_guard=prepared.replay_guard,
        dispatch_started_at=dispatch_started_at,
    )


def _parsed_command_for_event(
    controller: TurnController,
    event: TextDispatchEvent,
    *,
    media_events: list[MediaDispatchEvent] | None,
    ingress_metadata: DispatchIngressMetadata | None,
) -> Command | None:
    if media_events:
        return None
    if ingress_metadata is not None and ingress_metadata.source_kind == VOICE_SOURCE_KIND:
        return None
    if is_audio_message_event(event) or is_voice_event(
        event,
        sender_is_trusted=controller._sender_is_trusted_for_ingress_metadata,
    ):
        return None
    return command_parser.parse(event.body)


async def _blocked_before_plan(
    controller: TurnController,
    room: nio.MatrixRoom,
    prepared: _PreparedTextDispatch,
    *,
    requester_user_id: str,
) -> bool:
    if prepared.command is not None:
        if controller.deps.agent_name == ROUTER_AGENT_NAME:
            await controller._execute_command(
                room=room,
                event=prepared.event,
                requester_user_id=requester_user_id,
                command=prepared.command,
                target=prepared.dispatch.target,
            )
        return True
    if controller._should_skip_deep_synthetic_full_dispatch(
        event_id=prepared.event.event_id,
        envelope=prepared.dispatch.envelope,
    ):
        return True

    if prepared.replay_guard.degraded:
        skips_turn = await controller._has_newer_unresponded_cached_thread_event(
            room_id=room.room_id,
            event=prepared.event,
            requester_user_id=requester_user_id,
            thread_id=prepared.replay_guard.thread_id,
            source_kind=prepared.dispatch.envelope.source_kind,
        )
        if not skips_turn:
            controller.deps.logger.warning(
                "Thread replay guard degraded; proceeding without negative newer-message proof",
                event_id=prepared.event.event_id,
                room_id=room.room_id,
                thread_id=prepared.replay_guard.thread_id,
                thread_read_degraded=True,
            )
    else:
        skips_turn = controller._has_newer_unresponded_in_thread(
            prepared.event,
            requester_user_id,
            prepared.replay_guard.history,
            source_kind=prepared.dispatch.envelope.source_kind,
        )
    if skips_turn:
        controller._mark_source_events_responded(prepared.handled_turn)
    return skips_turn


def _attachment_parts(
    prepared: _PreparedTextDispatch,
    *,
    media_events: list[MediaDispatchEvent] | None,
    requester_user_id: str,
) -> tuple[list[str], list[str], dict[str, Any]]:
    payload_metadata = prepared.payload_metadata
    message_attachment_ids = (
        list(payload_metadata.attachment_ids)
        if payload_metadata is not None and payload_metadata.attachment_ids is not None
        else parse_attachment_ids_from_event_source(prepared.event.source)
    )
    trusted_attachment_ids = (
        list(payload_metadata.attachment_ids)
        if payload_metadata is not None and payload_metadata.attachment_ids is not None
        else []
    )
    extra_content: dict[str, Any] = {}
    if message_attachment_ids:
        extra_content[ATTACHMENT_IDS_KEY] = message_attachment_ids
    if payload_metadata is not None and payload_metadata.original_sender is not None:
        extra_content[ORIGINAL_SENDER_KEY] = payload_metadata.original_sender
    if payload_metadata is not None and payload_metadata.raw_audio_fallback:
        extra_content[VOICE_RAW_AUDIO_FALLBACK_KEY] = True
    if media_events and ORIGINAL_SENDER_KEY not in extra_content:
        extra_content[ORIGINAL_SENDER_KEY] = requester_user_id
    return message_attachment_ids, trusted_attachment_ids, extra_content


async def _apply_turn_plan(
    controller: TurnController,
    room: nio.MatrixRoom,
    prepared: _PreparedTextDispatch,
    plan: _TurnPlan,
    *,
    message_attachment_ids: list[str],
    trusted_attachment_ids: list[str],
    media_events: list[MediaDispatchEvent] | None,
    queued_notice_reservation: QueuedHumanNoticeReservation | None,
) -> None:
    if plan.kind == "ignore":
        if plan.ignore_reason == "router":
            router_outcome = controller._router_handled_turn_outcome(prepared.handled_turn)
            if router_outcome is not None:
                controller._mark_source_events_responded(router_outcome)
        return
    if plan.kind == "route":
        await _execute_route_plan(controller, room, prepared, plan, media_events=media_events)
        return

    assert plan.response_action is not None
    response_history_scope = (
        controller.deps.turn_store.response_history_scope(plan.response_action)
        if plan.response_action.kind in {"individual", "team"}
        else None
    )
    handled_turn = controller.deps.turn_store.attach_response_context(
        prepared.handled_turn,
        history_scope=response_history_scope,
        conversation_target=prepared.dispatch.target,
    )

    async def build_payload(context: MessageContext) -> DispatchPayload:
        effective_thread_id = prepared.dispatch.target.resolved_thread_id
        media_attachment_ids: list[str] = []
        fallback_images = None
        if media_events:
            media_result = await controller.deps.normalizer.register_batch_media_attachments(
                BatchMediaAttachmentRequest(
                    room_id=room.room_id,
                    thread_id=effective_thread_id,
                    media_events=media_events,
                ),
            )
            media_attachment_ids = media_result.attachment_ids
            fallback_images = media_result.fallback_images
        return await controller.deps.normalizer.build_dispatch_payload_with_attachments(
            DispatchPayloadWithAttachmentsRequest(
                room_id=room.room_id,
                prompt=prepared.event.body,
                current_attachment_ids=merge_attachment_ids(message_attachment_ids, media_attachment_ids),
                trusted_current_attachment_ids=trusted_attachment_ids,
                thread_id=context.thread_id,
                media_thread_id=effective_thread_id,
                thread_history=context.thread_history,
                fallback_images=fallback_images,
            ),
        )

    await controller._execute_response_action(
        room,
        prepared.event,
        prepared.dispatch,
        plan.response_action,
        build_payload,
        processing_log="Processing",
        dispatch_started_at=prepared.dispatch_started_at,
        handled_turn=handled_turn,
        matrix_run_metadata=controller.deps.turn_store.build_run_metadata(handled_turn),
        queued_notice_reservation=queued_notice_reservation,
    )


async def _execute_route_plan(
    controller: TurnController,
    room: nio.MatrixRoom,
    prepared: _PreparedTextDispatch,
    plan: _TurnPlan,
    *,
    media_events: list[MediaDispatchEvent] | None,
) -> None:
    route_event = plan.router_event or prepared.event
    single_direct_media_route = (
        is_matrix_media_dispatch_event(route_event)
        and media_events == [route_event]
        and prepared.handled_turn.source_event_ids == (prepared.event.event_id,)
    )
    routing_kwargs: dict[str, Any] = {
        "message": prepared.event.body if media_events else plan.router_message,
        "requester_user_id": prepared.dispatch.requester_user_id,
        "extra_content": plan.extra_content,
    }
    if plan.media_events is not None and not single_direct_media_route:
        routing_kwargs["media_events"] = plan.media_events
    tracked_turn = _tracked_route_turn(
        prepared,
        route_event=route_event,
        single_direct_media_route=single_direct_media_route,
    )
    if tracked_turn is not None:
        routing_kwargs["handled_turn"] = controller.deps.turn_store.attach_response_context(
            tracked_turn,
            history_scope=None,
            conversation_target=prepared.dispatch.target,
        )
    await controller._execute_router_relay(
        room,
        route_event,
        prepared.dispatch.context.thread_history,
        prepared.dispatch.target.resolved_thread_id,
        **routing_kwargs,
    )


def _tracked_route_turn(
    prepared: _PreparedTextDispatch,
    *,
    route_event: DispatchEvent,
    single_direct_media_route: bool,
) -> HandledTurnState | None:
    if single_direct_media_route:
        return None
    if not prepared.handled_turn.is_coalesced and (
        not prepared.handled_turn.source_event_ids
        or prepared.handled_turn.source_event_ids[0] == prepared.event.event_id
    ):
        return None
    if list(prepared.handled_turn.source_event_ids) == [route_event.event_id]:
        return None
    return prepared.handled_turn
