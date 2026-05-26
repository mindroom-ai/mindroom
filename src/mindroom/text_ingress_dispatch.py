"""Text ingress dispatch path used by TurnController."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

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
    attach_dispatch_pipeline_timing,
    event_timing_scope,
    get_dispatch_pipeline_timing,
)
from mindroom.timing import timing_scope as timing_scope_context

if TYPE_CHECKING:
    import nio

    from mindroom.conversation_resolver import MessageContext
    from mindroom.response_lifecycle import QueuedHumanNoticeReservation
    from mindroom.turn_controller import TurnController


async def dispatch_text_message(  # noqa: C901, PLR0912, PLR0915
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
    router_event: DispatchEvent = raw_event
    reservation = queued_notice_reservation
    timing_scope_token = None
    try:
        event = await controller.deps.normalizer.resolve_text_event(
            TextNormalizationRequest(event=raw_event),
        )
        trust_internal_payload_metadata = (
            controller._should_trust_internal_payload_metadata(event)
            if trust_hydrated_internal_metadata is None
            else trust_hydrated_internal_metadata
        )
        hydrated_payload_metadata = payload_metadata_from_source(
            event.source,
            trust_internal_metadata=trust_internal_payload_metadata,
        )
        payload_metadata = (
            hydrated_payload_metadata
            if payload_metadata is None
            else merge_payload_metadata(
                payload_metadata,
                hydrated_payload_metadata,
                trust_hydrated_internal_metadata=trust_internal_payload_metadata,
            )
        )
        dispatch_timing = get_dispatch_pipeline_timing(raw_event.source)
        attach_dispatch_pipeline_timing(event.source, dispatch_timing)
        timing_scope_token = timing_scope_context.set(event_timing_scope(event.event_id))
        if dispatch_timing is not None:
            dispatch_timing.mark("dispatch_start")
        dispatch_started_at = time.monotonic()
        if handled_turn is None:
            handled_turn = HandledTurnState.from_source_event_id(event.event_id)
        elif raw_event is not event and event.event_id in handled_turn.source_event_ids:
            refreshed_prompts = dict(handled_turn.source_event_prompts or {})
            refreshed_prompts[event.event_id] = event.body
            handled_turn = handled_turn.with_source_event_prompts(refreshed_prompts)

        if dispatch_timing is not None:
            dispatch_timing.mark("dispatch_prepare_start")
        command = None
        event_is_voice_dispatch = (
            (ingress_metadata is not None and ingress_metadata.source_kind == VOICE_SOURCE_KIND)
            or is_audio_message_event(event)
            or is_voice_event(event, sender_is_trusted=controller._sender_is_trusted_for_ingress_metadata)
        )
        if not media_events and not event_is_voice_dispatch:
            command = command_parser.parse(event.body)
        prepared_dispatch = await controller._prepare_dispatch(
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
        if prepared_dispatch is None:
            return
        dispatch = prepared_dispatch.dispatch
        replay_guard = prepared_dispatch.replay_guard
        handled_turn = handled_turn.with_request_context(
            requester_id=dispatch.requester_user_id,
            correlation_id=dispatch.correlation_id,
        )

        if command is not None and dispatch.envelope.source_kind == VOICE_SOURCE_KIND:
            command = None
        if command:
            if controller.deps.agent_name == ROUTER_AGENT_NAME:
                await controller._execute_command(
                    room=room,
                    event=event,
                    requester_user_id=requester_user_id,
                    command=command,
                    target=dispatch.target,
                )
            return
        if controller._should_skip_deep_synthetic_full_dispatch(
            event_id=event.event_id,
            envelope=dispatch.envelope,
        ):
            return
        message_attachment_ids = (
            list(payload_metadata.attachment_ids)
            if payload_metadata is not None and payload_metadata.attachment_ids is not None
            else parse_attachment_ids_from_event_source(event.source)
        )
        trusted_current_attachment_ids = (
            list(payload_metadata.attachment_ids)
            if payload_metadata is not None and payload_metadata.attachment_ids is not None
            else []
        )
        message_extra_content: dict[str, Any] = {}
        if message_attachment_ids:
            message_extra_content[ATTACHMENT_IDS_KEY] = message_attachment_ids
        if payload_metadata is not None and payload_metadata.original_sender is not None:
            message_extra_content[ORIGINAL_SENDER_KEY] = payload_metadata.original_sender
        if payload_metadata is not None and payload_metadata.raw_audio_fallback:
            message_extra_content[VOICE_RAW_AUDIO_FALLBACK_KEY] = True
        router_extra_content = dict(message_extra_content)
        if media_events and ORIGINAL_SENDER_KEY not in router_extra_content:
            router_extra_content[ORIGINAL_SENDER_KEY] = requester_user_id
        replay_guard_skips_turn = False
        if replay_guard.degraded:
            replay_guard_skips_turn = await controller._has_newer_unresponded_cached_thread_event(
                room_id=room.room_id,
                event=event,
                requester_user_id=requester_user_id,
                thread_id=replay_guard.thread_id,
                source_kind=dispatch.envelope.source_kind,
            )
            if not replay_guard_skips_turn:
                controller.deps.logger.warning(
                    "Thread replay guard degraded; proceeding without negative newer-message proof",
                    event_id=event.event_id,
                    room_id=room.room_id,
                    thread_id=replay_guard.thread_id,
                    thread_read_degraded=True,
                )
        else:
            replay_guard_skips_turn = controller._has_newer_unresponded_in_thread(
                event,
                requester_user_id,
                replay_guard.history,
                source_kind=dispatch.envelope.source_kind,
            )
        if replay_guard_skips_turn:
            controller._mark_source_events_responded(handled_turn)
            return
        if dispatch_timing is not None:
            dispatch_timing.mark("dispatch_plan_start")
        plan = await controller.deps.turn_policy.plan_turn(
            room,
            event,
            dispatch,
            is_dm=await is_dm_room(controller._client(), room.room_id),
            has_active_response_for_target=controller.deps.response_runner.has_active_response_for_target,
            extra_content=router_extra_content or None,
            media_events=media_events,
            router_event=media_events[0] if media_events and len(handled_turn.source_event_ids) == 1 else router_event,
        )
        if dispatch_timing is not None:
            dispatch_timing.mark("dispatch_plan_ready")
        if plan.kind == "ignore":
            if plan.ignore_reason == "router":
                router_outcome = controller._router_handled_turn_outcome(handled_turn)
                if router_outcome is not None:
                    controller._mark_source_events_responded(router_outcome)
            return
        if plan.kind == "route":
            route_event = plan.router_event or event
            tracked_route_handled_turn = (
                handled_turn
                if handled_turn.is_coalesced
                or (handled_turn.source_event_ids and handled_turn.source_event_ids[0] != event.event_id)
                else None
            )
            single_direct_media_route = (
                is_matrix_media_dispatch_event(route_event)
                and media_events == [route_event]
                and handled_turn.source_event_ids == (event.event_id,)
            )
            routing_kwargs: dict[str, Any] = {
                "message": event.body if media_events else plan.router_message,
                "requester_user_id": dispatch.requester_user_id,
                "extra_content": plan.extra_content,
            }
            if plan.media_events is not None and not single_direct_media_route:
                routing_kwargs["media_events"] = plan.media_events
            if (
                tracked_route_handled_turn is not None
                and list(tracked_route_handled_turn.source_event_ids) != [route_event.event_id]
                and not single_direct_media_route
            ):
                routing_kwargs["handled_turn"] = controller.deps.turn_store.attach_response_context(
                    tracked_route_handled_turn,
                    history_scope=None,
                    conversation_target=dispatch.target,
                )
            await controller._execute_router_relay(
                room,
                route_event,
                dispatch.context.thread_history,
                dispatch.target.resolved_thread_id,
                **routing_kwargs,
            )
            return
        assert plan.response_action is not None
        response_history_scope = (
            controller.deps.turn_store.response_history_scope(plan.response_action)
            if plan.response_action.kind in {"individual", "team"}
            else None
        )
        handled_turn = controller.deps.turn_store.attach_response_context(
            handled_turn,
            history_scope=response_history_scope,
            conversation_target=dispatch.target,
        )
        matrix_run_metadata = controller.deps.turn_store.build_run_metadata(handled_turn)

        async def build_payload(context: MessageContext) -> DispatchPayload:
            effective_thread_id = dispatch.target.resolved_thread_id
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
                    prompt=event.body,
                    current_attachment_ids=merge_attachment_ids(
                        message_attachment_ids,
                        media_attachment_ids,
                    ),
                    trusted_current_attachment_ids=trusted_current_attachment_ids,
                    thread_id=context.thread_id,
                    media_thread_id=effective_thread_id,
                    thread_history=context.thread_history,
                    fallback_images=fallback_images,
                ),
            )

        await controller._execute_response_action(
            room,
            event,
            dispatch,
            plan.response_action,
            build_payload,
            processing_log="Processing",
            dispatch_started_at=dispatch_started_at,
            handled_turn=handled_turn,
            matrix_run_metadata=matrix_run_metadata,
            queued_notice_reservation=reservation,
        )
    finally:
        if reservation is not None:
            reservation.cancel()
        if timing_scope_token is not None:
            timing_scope_context.reset(timing_scope_token)
