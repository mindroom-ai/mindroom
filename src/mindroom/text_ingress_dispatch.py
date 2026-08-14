"""Text ingress dispatch path used by TurnController."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol

from mindroom.attachments import parse_attachment_ids_from_event_source
from mindroom.commands.parsing import command_parser
from mindroom.constants import (
    ATTACHMENT_IDS_KEY,
    ORIGINAL_SENDER_KEY,
    SCHEDULED_HISTORY_LIMIT_KEY,
    VOICE_RAW_AUDIO_FALLBACK_KEY,
    VOICE_TRANSCRIPT_KEY,
)
from mindroom.dispatch_source import VOICE_SOURCE_KIND, is_voice_event
from mindroom.matrix.media import is_audio_message_event, is_matrix_media_dispatch_event
from mindroom.matrix.rooms import is_dm_room
from mindroom.response_admission import admitted_response_decision
from mindroom.response_payload_preparation import DispatchPayloadInputs
from mindroom.timing import (
    DispatchPipelineTiming,
    attach_dispatch_pipeline_timing,
    event_timing_scope,
    get_dispatch_pipeline_timing,
)
from mindroom.timing import (
    timing_scope as timing_scope_context,
)
from mindroom.turn_record import canonicalize_turn_record

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    import nio

    from mindroom.coalescing_batch import PreparedTurn
    from mindroom.commands.parsing import Command
    from mindroom.dispatch_handoff import (
        DispatchEvent,
        DispatchIngressMetadata,
        MediaDispatchEvent,
        PreparedIngress,
    )
    from mindroom.handled_turns import TurnRecord
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
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
    turn: PreparedTurn
    event: PreparedIngress
    handled_turn: TurnRecord
    command: Command | None
    dispatch: PreparedDispatch
    replay_guard: _ReplayGuard
    dispatch_started_at: float


async def dispatch_text_message(
    controller: TurnController,
    turn: PreparedTurn,
) -> None:
    """Run the normal text or command dispatch pipeline for a prepared text event."""
    turn_claim = turn.handled_turn
    if not controller.deps.turn_store.try_claim_turn(turn_claim):
        return
    claim_transferred = False

    def mark_claim_transferred() -> None:
        nonlocal claim_transferred
        claim_transferred = True

    timing_scope_token = None
    try:
        dispatch_timing = get_dispatch_pipeline_timing(turn.event.source)
        prepared = await _prepare_text_dispatch(
            controller,
            turn,
            dispatch_timing=dispatch_timing,
        )
        if prepared is None:
            return
        timing_scope_token = timing_scope_context.set(event_timing_scope(prepared.event.event_id))
        async with admitted_response_decision(
            controller.deps.runtime.response_admission_gate,
            controller.deps.response_runner.wait_for_admission_or_shutdown,
        ):
            if not controller.deps.turn_policy.can_reply_to_sender(turn.requester_user_id):
                await controller.deps.visible_responses.settle_source_events_ignored(prepared.handled_turn)
                return
            if await _blocked_before_plan(
                controller,
                turn.room,
                prepared,
            ):
                return
            message_attachment_ids, trusted_attachment_ids, router_extra_content = _attachment_parts(
                prepared,
                media_events=list(turn.media_events) or None,
                requester_user_id=turn.requester_user_id,
            )
            if dispatch_timing is not None:
                dispatch_timing.mark("dispatch_plan_start")
            plan = await controller.deps.turn_policy.plan_turn(
                turn.room,
                prepared.event,
                prepared.dispatch,
                is_dm=await is_dm_room(controller._client(), turn.room.room_id),
                has_active_response_for_target=controller.deps.response_runner.has_active_response_for_target,
                extra_content=router_extra_content or None,
                media_events=list(turn.media_events) or None,
                router_event=turn.media_events[0]
                if turn.media_events and len(prepared.handled_turn.source_event_ids) == 1
                else turn.event,
            )
            if dispatch_timing is not None:
                dispatch_timing.mark("dispatch_plan_ready")
            await _apply_turn_plan(
                controller,
                turn.room,
                prepared,
                plan,
                message_attachment_ids=message_attachment_ids,
                trusted_attachment_ids=trusted_attachment_ids,
                media_events=list(turn.media_events) or None,
                turn_claim=turn_claim,
                mark_claim_transferred=mark_claim_transferred,
            )
    finally:
        if not claim_transferred:
            controller.deps.turn_store.release_pending_turn_claim(turn_claim)
        if timing_scope_token is not None:
            timing_scope_context.reset(timing_scope_token)


async def _prepare_text_dispatch(
    controller: TurnController,
    turn: PreparedTurn,
    *,
    dispatch_timing: DispatchPipelineTiming | None,
) -> _PreparedTextDispatch | None:
    event = turn.event
    attach_dispatch_pipeline_timing(event.source, dispatch_timing)
    if dispatch_timing is not None:
        dispatch_timing.mark("dispatch_start")
    dispatch_started_at = time.monotonic()

    handled_turn = turn.handled_turn
    routed_original_event_id = controller.deps.ingress.router_relay_original_event_id(event)
    if routed_original_event_id is not None:
        # Keep the routed turn discoverable by the human message the router
        # relayed, so edits and redactions of that message reach this
        # responder's persisted runs.
        handled_turn = canonicalize_turn_record(
            handled_turn,
            discovery_event_ids=(*handled_turn.discovery_event_ids, routed_original_event_id),
        )

    command = _parsed_command_for_event(
        controller,
        event,
        media_events=list(turn.media_events) or None,
        ingress_metadata=turn.ingress,
    )
    if dispatch_timing is not None:
        dispatch_timing.mark("dispatch_prepare_start")
    prepared = await controller._prepare_dispatch(
        turn.room,
        event,
        turn.requester_user_id,
        event_label="message",
        handled_turn=handled_turn,
        ingress_metadata=turn.ingress,
        payload_metadata=turn.payload,
        use_command_context=command is not None,
        current_prompt_is_structured=turn.current_prompt_is_structured,
    )
    if dispatch_timing is not None:
        dispatch_timing.mark("dispatch_prepare_ready")
    if prepared is None:
        return None
    if command is not None and prepared.dispatch.envelope.source_kind == VOICE_SOURCE_KIND:
        command = None
    return _PreparedTextDispatch(
        turn=turn,
        event=event,
        handled_turn=canonicalize_turn_record(
            handled_turn,
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
    event: PreparedIngress,
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
        sender_is_trusted=controller.deps.ingress.sender_is_trusted_for_ingress_metadata,
    ):
        return None
    return command_parser.parse(event.body)


def _turn_sources_all_from_requester(handled_turn: TurnRecord, requester_user_id: str) -> bool:
    """Return whether every replayable source in one turn was sent by ``requester_user_id``.

    Whole-turn suppression settles every source in a coalesced batch, so it is only safe when the
    turn provably belongs to that one requester, and a source the record cannot attribute fails
    closed. Redacted sources are excluded because they own no reply.
    """
    return all(
        handled_turn.requester_id_for_source(source_event_id) == requester_user_id
        for source_event_id in handled_turn.replay_source_event_ids
    )


async def _blocked_before_plan(
    controller: TurnController,
    room: nio.MatrixRoom,
    prepared: _PreparedTextDispatch,
) -> bool:
    requester_user_id = prepared.turn.requester_user_id
    visible_responses = controller.deps.visible_responses
    if prepared.command is not None:
        command_owned = await controller.deps.command_executor.execute_if_owned(
            room=room,
            event=prepared.event,
            requester_user_id=requester_user_id,
            command=prepared.command,
            target=prepared.dispatch.target,
            handled_turn=prepared.handled_turn,
        )
        if not command_owned:
            await visible_responses.settle_source_events_ignored(prepared.handled_turn)
        return True
    if controller._should_skip_deep_synthetic_full_dispatch(
        event_id=prepared.event.event_id,
        envelope=prepared.dispatch.envelope,
    ):
        await visible_responses.settle_source_events_ignored(prepared.handled_turn)
        return True

    may_be_superseded = (
        prepared.dispatch.envelope.origin.may_be_superseded_by_newer_requester_turn
        and _turn_sources_all_from_requester(prepared.handled_turn, requester_user_id)
    )
    if prepared.replay_guard.degraded:
        skips_turn = await controller._has_newer_unresponded_journal_thread_event(
            room_id=room.room_id,
            event=prepared.event,
            requester_user_id=requester_user_id,
            thread_id=prepared.replay_guard.thread_id,
            may_be_superseded_by_newer_requester_turn=may_be_superseded,
        )
        if may_be_superseded and not skips_turn:
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
            may_be_superseded_by_newer_requester_turn=may_be_superseded,
        )
    if skips_turn:
        await visible_responses.settle_source_events_ignored(prepared.handled_turn)
    return skips_turn


def _attachment_parts(
    prepared: _PreparedTextDispatch,
    *,
    media_events: list[MediaDispatchEvent] | None,
    requester_user_id: str,
) -> tuple[list[str], list[str], dict[str, Any]]:
    payload_metadata = prepared.turn.payload
    message_attachment_ids = (
        list(payload_metadata.attachment_ids)
        if payload_metadata.attachment_ids is not None
        else parse_attachment_ids_from_event_source(prepared.event.source)
    )
    trusted_attachment_ids = (
        list(payload_metadata.attachment_ids) if payload_metadata.attachment_ids is not None else []
    )
    extra_content: dict[str, Any] = {}
    if message_attachment_ids:
        extra_content[ATTACHMENT_IDS_KEY] = message_attachment_ids
    if payload_metadata.original_sender is not None:
        extra_content[ORIGINAL_SENDER_KEY] = payload_metadata.original_sender
    if payload_metadata.raw_audio_fallback:
        extra_content[VOICE_RAW_AUDIO_FALLBACK_KEY] = True
    if payload_metadata.voice_transcript:
        extra_content[VOICE_TRANSCRIPT_KEY] = True
    if media_events and ORIGINAL_SENDER_KEY not in extra_content:
        extra_content[ORIGINAL_SENDER_KEY] = requester_user_id
    if prepared.dispatch.scheduled_history_budget is not None:
        extra_content[SCHEDULED_HISTORY_LIMIT_KEY] = prepared.dispatch.scheduled_history_budget.limit
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
    turn_claim: TurnRecord,
    mark_claim_transferred: Callable[[], None],
) -> None:
    visible_responses = controller.deps.visible_responses
    if plan.kind == "ignore":
        if plan.ignore_reason == "router":
            router_outcome = controller._router_handled_turn_outcome(prepared.handled_turn)
            if router_outcome is not None:
                await controller.deps.turn_store.record_responded_turn(router_outcome)
            else:
                await visible_responses.settle_source_events_ignored(prepared.handled_turn)
        else:
            await visible_responses.settle_source_events_ignored(prepared.handled_turn)
        return
    if plan.kind == "route":
        await _execute_route_plan(controller, room, prepared, plan, media_events=media_events)
        return

    assert plan.response_action is not None
    reconcile_visible_response = controller.deps.turn_store.has_pending_response_intent(
        prepared.handled_turn.source_event_ids,
    )
    response_history_scope = (
        controller.deps.turn_store.response_history_scope(
            plan.response_action,
            requester_user_id=prepared.dispatch.requester_user_id,
        )
        if plan.response_action.kind in {"individual", "team"}
        else None
    )

    payload_inputs = DispatchPayloadInputs(
        message_attachment_ids=tuple(message_attachment_ids),
        trusted_attachment_ids=tuple(trusted_attachment_ids),
        media_events=tuple(media_events or ()),
        raw_audio_fallback=prepared.turn.payload.raw_audio_fallback is True,
        voice_transcript=prepared.turn.payload.voice_transcript is True,
    )
    handled_turn = controller.deps.turn_store.attach_response_context(
        prepared.handled_turn,
        history_scope=response_history_scope,
        conversation_target=prepared.dispatch.target,
    )
    pending_turn = await controller.deps.turn_store.record_pending_turn(handled_turn)
    if pending_turn is None or pending_turn.completed:
        return
    if pending_turn.redacted_source_event_ids:
        await visible_responses.settle_source_events_ignored(pending_turn)
        return
    handled_turn = pending_turn

    # The inbox handoff is complete once the runner takes the conversation's
    # response lock; the response itself keeps running on a runner-owned task.
    response_started = asyncio.Event()
    response_task = controller.deps.response_runner.track_inbox_response(
        _run_claimed_response(
            controller,
            turn_claim,
            controller._execute_response_action(
                room,
                prepared.event,
                prepared.dispatch,
                plan.response_action,
                payload_inputs,
                processing_log="Processing",
                dispatch_started_at=prepared.dispatch_started_at,
                handled_turn=handled_turn,
                matrix_run_metadata=controller.deps.turn_store.build_run_metadata(handled_turn),
                on_lifecycle_lock_acquired=response_started.set,
                reconcile_visible_response=reconcile_visible_response,
            ),
        ),
        name=f"inbox_response:{prepared.event.event_id}",
        recovery_proof_ready=lambda: (
            prepared.dispatch.target.resolved_thread_id is not None
            and controller.deps.interrupted_turn_rooms.contains(prepared.event.event_id)
        ),
        on_failure=lambda: (
            controller.deps.retry_dispatch_sources(handled_turn.source_event_ids) if response_started.is_set() else None
        ),
        source_event_ids=handled_turn.source_event_ids,
    )
    # Ownership moves synchronously after task creation. If this dispatch task
    # is cancelled while waiting for the lifecycle lock, its finally block must
    # not reopen the source while the runner-owned response still exists.
    mark_claim_transferred()
    started_wait = asyncio.ensure_future(response_started.wait())
    try:
        await asyncio.wait({started_wait, response_task}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        started_wait.cancel()
    # Surface pre-lock failures to the caller's containment; post-lock
    # failures (including a fast failure racing the FIRST_COMPLETED wait)
    # belong to the runner-owned task. Pre-lock CANCELLATION is
    # deliberately NOT surfaced: this coroutine was not itself cancelled,
    # and re-raising CancelledError here would corrupt the gate drain's
    # own cancellation state.
    if response_task.done() and not response_task.cancelled() and not response_started.is_set():
        response_task.result()


async def _run_claimed_response(
    controller: TurnController,
    turn_claim: TurnRecord,
    response: Awaitable[None],
) -> None:
    """Release exclusive response ownership after every terminal path."""
    try:
        await response
    finally:
        controller.deps.turn_store.release_pending_turn_claim(turn_claim)


async def _run_admitted_router_relay(
    controller: TurnController,
    relay: Callable[[], Awaitable[None]],
) -> None:
    """Keep config application outside one router selection and relay delivery."""
    async with admitted_response_decision(
        controller.deps.runtime.response_admission_gate,
        controller.deps.response_runner.wait_for_admission_or_shutdown,
    ):
        await relay()


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
    if prepared.dispatch.scheduled_history_budget is not None:
        routing_kwargs["scheduled_prompt"] = prepared.event.body
    await _run_admitted_router_relay(
        controller,
        lambda: controller._execute_router_relay(
            room,
            route_event,
            prepared.dispatch.context.thread_history,
            prepared.dispatch.target.resolved_thread_id,
            **routing_kwargs,
        ),
    )


def _tracked_route_turn(
    prepared: _PreparedTextDispatch,
    *,
    route_event: DispatchEvent,
    single_direct_media_route: bool,
) -> TurnRecord | None:
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
