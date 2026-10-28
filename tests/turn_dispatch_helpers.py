"""Test helpers for dispatching through the production PreparedTurn boundary."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import nio

from mindroom.coalescing.batch import PendingEvent, PreparedTurn, build_prepared_turn, requester_coalescing_key
from mindroom.dispatch_handoff import (
    DispatchIngressMetadata,
    DispatchPayloadMetadata,
    PreparedIngress,
    payload_metadata_from_source,
)
from mindroom.dispatch_source import MESSAGE_SOURCE_KIND
from mindroom.handled_turns import TurnRecord
from mindroom.matrix.event_info import EventInfo
from mindroom.turn_controller import _PrecheckedEvent
from mindroom.turn_store import TurnClaim

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mindroom.dispatch_handoff import MediaDispatchEvent
    from mindroom.response_lifecycle import QueuedHumanNoticeReservation
    from mindroom.turn_controller import TurnController


def _prepared_ingress(
    event: PreparedIngress | nio.RoomMessageFormatted | _PrecheckedEvent,
    requester_user_id: str | None,
) -> tuple[PreparedIngress, str]:
    if isinstance(event, _PrecheckedEvent):
        requester_user_id = event.requester_user_id
        event = event.event
    if requester_user_id is None:
        msg = "requester_user_id is required when dispatching a raw test event"
        raise TypeError(msg)
    if isinstance(event, PreparedIngress):
        return event, requester_user_id
    if not isinstance(event, nio.RoomMessageFormatted):
        msg = f"Unsupported test dispatch event: {type(event).__name__}"
        raise TypeError(msg)
    return (
        PreparedIngress(
            sender=event.sender,
            event_id=event.event_id,
            body=event.body,
            source=event.source,
            server_timestamp=event.server_timestamp,
        ),
        requester_user_id,
    )


def make_test_turn(
    room: nio.MatrixRoom,
    event: PreparedIngress | nio.RoomMessageFormatted | _PrecheckedEvent,
    requester_user_id: str | None = None,
    *,
    media_events: list[MediaDispatchEvent] | None = None,
    handled_turn: TurnRecord | None = None,
    queued_notice_reservation: QueuedHumanNoticeReservation | None = None,
    ingress_metadata: DispatchIngressMetadata | None = None,
    payload_metadata: DispatchPayloadMetadata | None = None,
    current_prompt_is_structured: bool = False,
    claim: TurnClaim | None = None,
) -> PreparedTurn:
    """Build realistic gate output for focused downstream-dispatch tests, carrying the ingress claim given."""
    prepared, requester_user_id = _prepared_ingress(event, requester_user_id)
    event_info = EventInfo.from_event(prepared.source)
    key = (
        ingress_metadata.coalescing_key
        if ingress_metadata is not None and ingress_metadata.coalescing_key is not None
        else requester_coalescing_key(room.room_id, event_info.thread_id, requester_user_id)
    )
    source_kind = (
        ingress_metadata.source_kind if ingress_metadata is not None else prepared.source_kind or MESSAGE_SOURCE_KIND
    )
    prepared = replace(
        prepared,
        requester_user_id=requester_user_id,
        source_kind=source_kind,
        dispatch_policy_source_kind=(
            ingress_metadata.dispatch_policy_source_kind if ingress_metadata is not None else None
        ),
        hook_source=ingress_metadata.hook_source if ingress_metadata is not None else None,
        message_received_depth=ingress_metadata.message_received_depth if ingress_metadata is not None else 0,
        trust_internal_payload_metadata=ingress_metadata is not None,
    )
    turn = build_prepared_turn(
        key,
        [PendingEvent(event=prepared, room=room, claim=claim, notice=queued_notice_reservation)],
    )
    return replace(
        turn,
        handled_turn=handled_turn or turn.handled_turn,
        ingress=replace(ingress_metadata, coalescing_key=key) if ingress_metadata is not None else turn.ingress,
        payload=(
            payload_metadata
            if payload_metadata is not None
            else payload_metadata_from_source(prepared.source, trust_internal_metadata=ingress_metadata is not None)
        ),
        media_events=tuple(media_events) if media_events is not None else turn.media_events,
        current_prompt_is_structured=current_prompt_is_structured,
    )


async def dispatch_test_turn(
    controller: TurnController,
    room: nio.MatrixRoom,
    event: PreparedIngress | nio.RoomMessageFormatted | _PrecheckedEvent,
    requester_user_id: str | None = None,
    **kwargs: object,
) -> None:
    """Dispatch one test turn through same single-object production boundary, claimed as ingress claims it.

    A turn some live owner already holds is dispatched without a claim, so the
    gate-exit merge meets that owner as production would.
    """
    turn = make_test_turn(room, event, requester_user_id, **kwargs)
    claimed = await controller.deps.turn_store.claim(
        TurnRecord.create(
            turn.handled_turn.source_event_ids,
            discovery_event_ids=turn.handled_turn.discovery_event_ids,
            completed=False,
        ),
    )
    claims = (claimed,) if isinstance(claimed, TurnClaim) else ()
    await controller.handle_prepared_turn(replace(turn, claims=claims))


def prepared_turn_recorder(callback: Callable[..., Awaitable[None]]) -> Callable[..., Awaitable[None]]:
    """Adapt legacy test observers to the single PreparedTurn callback boundary."""

    async def record(*args: object) -> None:
        turn = args[-1]
        assert isinstance(turn, PreparedTurn)
        await callback(
            turn.room,
            turn.event,
            turn.requester_user_id,
            media_events=list(turn.media_events) or None,
            handled_turn=turn.handled_turn,
            ingress_metadata=turn.ingress,
            payload_metadata=turn.payload,
            current_prompt_is_structured=turn.current_prompt_is_structured,
        )

    return record
