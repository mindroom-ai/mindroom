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
from mindroom.matrix.event_info import EventInfo
from mindroom.turn_record import TurnRecord
from mindroom.turn_store import TurnClaim
from mindroom.turns.facts import PrecheckedEvent
from tests.conftest import make_ingress_facts

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mindroom.dispatch_handoff import MediaDispatchEvent
    from mindroom.response_lifecycle import QueuedHumanNoticeReservation
    from mindroom.turn_controller import TurnController
    from mindroom.turns.facts import IngressFacts


def _prepared_ingress(
    event: PreparedIngress | nio.RoomMessageFormatted | PrecheckedEvent,
    requester_user_id: str | None,
) -> tuple[PreparedIngress, IngressFacts]:
    """Return the prepared event and its facts: a prechecked event's own, else a plain message from its sender."""
    if isinstance(event, PrecheckedEvent):
        facts = event.facts
        event = event.event
    elif requester_user_id is None:
        msg = "requester_user_id is required when dispatching a raw test event"
        raise TypeError(msg)
    else:
        facts = make_ingress_facts(event.sender, requester_user_id=requester_user_id)
    if isinstance(event, PreparedIngress):
        return event, facts
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
        facts,
    )


def make_test_turn(
    room: nio.MatrixRoom,
    event: PreparedIngress | nio.RoomMessageFormatted | PrecheckedEvent,
    requester_user_id: str | None = None,
    *,
    media_events: list[MediaDispatchEvent] | None = None,
    handled_turn: TurnRecord | None = None,
    queued_notice_reservation: QueuedHumanNoticeReservation | None = None,
    ingress_metadata: DispatchIngressMetadata | None = None,
    payload_metadata: DispatchPayloadMetadata | None = None,
    current_prompt_is_structured: bool = False,
    claim: TurnClaim | None = None,
    facts: IngressFacts | None = None,
) -> PreparedTurn:
    """Build realistic gate output for focused downstream-dispatch tests, carrying the ingress claim given.

    Without ``facts`` the event is a plain message its sender wrote as ``requester_user_id``.
    """
    prepared, default_facts = _prepared_ingress(event, requester_user_id)
    facts = facts or default_facts
    event_info = EventInfo.from_event(prepared.source)
    key = (
        ingress_metadata.coalescing_key
        if ingress_metadata is not None and ingress_metadata.coalescing_key is not None
        else requester_coalescing_key(room.room_id, event_info.thread_id, facts.lane_requester_id)
    )
    prepared = replace(
        prepared,
        active_follow_up=ingress_metadata is not None and ingress_metadata.active_follow_up,
        payload_trusted=facts.sender_is_managed,
    )
    turn = build_prepared_turn(
        key,
        [PendingEvent(event=prepared, room=room, facts=facts, claim=claim, notice=queued_notice_reservation)],
    )
    return replace(
        turn,
        handled_turn=handled_turn or turn.handled_turn,
        ingress=replace(ingress_metadata, coalescing_key=key) if ingress_metadata is not None else turn.ingress,
        payload=(
            payload_metadata
            if payload_metadata is not None
            else payload_metadata_from_source(prepared.source, trust_internal_metadata=prepared.payload_trusted)
        ),
        media_events=tuple(media_events) if media_events is not None else turn.media_events,
        current_prompt_is_structured=current_prompt_is_structured,
    )


async def dispatch_test_turn(
    controller: TurnController,
    room: nio.MatrixRoom,
    event: PreparedIngress | nio.RoomMessageFormatted | PrecheckedEvent,
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
            turn.facts.run_requester_id,
            media_events=list(turn.media_events) or None,
            handled_turn=turn.handled_turn,
            ingress_metadata=turn.ingress,
            payload_metadata=turn.payload,
            current_prompt_is_structured=turn.current_prompt_is_structured,
        )

    return record
