"""A room's routing lane: the turn its owner prepared, handed to the responder chosen for it without a Matrix message.

The chosen bot answers the person's own events from the lane owner's normalized turn, as it answered a router relay:
the handoff is the one source its turn owns, and the person's events are the aliases edits and redactions reach it by.
"""

from __future__ import annotations

from dataclasses import asdict, replace
from typing import TYPE_CHECKING, Any, cast

from mindroom.coalescing.batch import PendingEvent, build_prepared_turn, requester_coalescing_key
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.dispatch_handoff import PreparedIngress
from mindroom.entity_resolution import entity_identity_registry
from mindroom.event_journal import ROUTING_HANDOFF_CLAIM, RoutingHandoff, StaleLaneOwnerError, turn_rows
from mindroom.event_journal.identity import agent_of_principal, journal_principal_id
from mindroom.matrix.media import parse_matrix_media_dispatch_event_source
from mindroom.timing import DispatchPipelineTiming
from mindroom.turn_record import SourceEventMetadata, TurnRecord
from mindroom.turns.facts import IngressFacts

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    import nio

    from mindroom.coalescing.batch import PreparedTurn, TimestampFormatter
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.event_journal import EventJournalStore


async def route_turn(
    journal: EventJournalStore,
    config: Config,
    runtime_paths: RuntimePaths,
    owner: str,
    turn: PreparedTurn,
    choose: Callable[[], Awaitable[str | None]],
) -> tuple[str, ...] | None:
    """Hand ``turn`` to the entity chosen for it, as the room's lane owner ``owner``; return the entities it woke work for.

    ``choose`` names the answering entity, None when nobody fits. It runs once per message: a turn that replays, after
    a crash or under a new lane owner, finds its messages handed off and only moves on what the room still holds,
    which includes the handoffs an earlier owner saved and never moved. A handoff is saved before it moves, and moving
    it settles the lane owner's events in the same transaction, so the turn is always in one journal.
    Returns None when another bot took the lane while ``choose`` ran, which saves nothing.
    """
    registry = entity_identity_registry(config, runtime_paths)

    def principal(entity_name: str) -> str:
        return journal_principal_id(entity_name, registry.current_id(entity_name).full_id)

    room_id, me = turn.room.room_id, principal(owner)
    sources = tuple(event.event_id for event, _facts in turn.inputs)
    generation = await journal.hold_routing_lane(room_id, me)
    handed_off = await journal.room_event_claims(room_id, sources, ROUTING_HANDOFF_CLAIM)
    rest = tuple(
        [
            source
            for source in sources
            if source not in handed_off and not await _relayed_by_the_old_router(journal, source)
        ],
    )
    if rest:
        recipient = await choose()
        handoff = RoutingHandoff(
            handoff_id=f"routed:{rest[0]}",
            room_id=room_id,
            source_event_ids=rest,
            recipient="" if recipient is None else principal(recipient),
            payload=None if recipient is None else _handoff_payload(turn, rest),
        )
        try:
            await journal.save_routing_handoff(handoff, owner=me, generation=generation)
        except StaleLaneOwnerError:
            return None
    return tuple(agent_of_principal(recipient) for recipient in await journal.accept_routing_handoffs(room_id))


# LEGACY_COMPAT: a person's message the relaying router already answered with its relay ("@agent could you help with
# this?") or its routing notice, which a lane owner that had not planned its own copy before the upgrade must not
# route too.
# Legacy format: the router's turn row for the message, with a response event that is not its voice echo.
# Last legacy release: v2026.11.396, the newest release whose router relays; see `IngressFacts.router_relay`.
# Handling: such a message gets no handoff, so the relay stays its one delivery; the messages batched with it keep
# theirs.
# Coverage: tests/test_turns_focused.py::test_original_the_old_router_already_relayed_gets_no_second_answer
async def _relayed_by_the_old_router(journal: EventJournalStore, event_id: str) -> bool:
    router_turns = journal.turn_records(ROUTER_AGENT_NAME)
    turn = await router_turns.read(turn_rows.get_turn_record, event_id)
    if turn is None or turn.response_event_id is None:
        return False
    # Until the answering agent posts the transcript, the router records its echo of a voice message as its answer.
    echoes = [await router_turns.read(turn_rows.visible_echo, source) for source in turn.source_event_ids]
    return turn.response_event_id not in {echo.event_id for echo in echoes if echo is not None}


def _handoff_payload(turn: PreparedTurn, sources: tuple[str, ...]) -> dict[str, object]:
    """Return what rebuilds these events of a prepared turn in another bot: each as normalized, edits applied."""
    return {
        "events": [
            {
                "sender": event.sender,
                "event_id": event.event_id,
                "body": event.body,
                # Ingress keeps its timing tracker on the source, which is no part of the event.
                "source": {k: v for k, v in event.source.items() if not isinstance(v, DispatchPipelineTiming)},
                "server_timestamp": event.server_timestamp,
                "payload_trusted": event.payload_trusted,
                "media": event.raw_event is not None,
                "facts": asdict(facts),
            }
            for event, facts in turn.inputs
            if event.event_id in sources
        ],
    }


def routed_events(payload: Mapping[str, object], room: nio.MatrixRoom) -> list[PendingEvent]:
    """Return the events of a handoff's payload as the lane owner queued them."""
    return [
        PendingEvent(event=_prepared_ingress(item), room=room, facts=IngressFacts(**item["facts"]))
        for item in cast("list[dict[str, Any]]", payload["events"])
    ]


def _prepared_ingress(item: dict[str, Any]) -> PreparedIngress:
    raw_event = parse_matrix_media_dispatch_event_source(item["source"]) if item["media"] else None
    if item["media"] and raw_event is None:
        msg = f"Routed media event {item['event_id']!r} does not rebuild as itself"
        raise TypeError(msg)
    return PreparedIngress(
        sender=item["sender"],
        event_id=item["event_id"],
        body=item["body"],
        source=item["source"],
        server_timestamp=item["server_timestamp"],
        payload_trusted=item["payload_trusted"],
        raw_event=raw_event,
    )


def routed_turn(
    handoff_id: str,
    thread_id: str | None,
    events: list[PendingEvent],
    *,
    timestamp_formatter: TimestampFormatter,
) -> PreparedTurn:
    """Return the turn a handoff's recipient answers: the lane owner's batch, owned through the handoff's ID.

    The batch is built as the gate builds it. Its turn then takes the handoff as its one source, which this bot's
    journal holds, and the person's events as its aliases, so they stay apart from this bot's own copies of them.
    """
    facts = events[-1].facts
    turn = build_prepared_turn(
        requester_coalescing_key(events[-1].room.room_id, thread_id, facts.lane_requester_id),
        events,
        queues_other_run=lambda _run_identity: False,
        timestamp_formatter=timestamp_formatter,
    )
    answered = turn.event
    return replace(
        turn,
        event=replace(answered, event_id=handoff_id),
        handled_turn=TurnRecord.create(
            [handoff_id],
            discovery_event_ids=turn.handled_turn.indexed_event_ids,
            source_event_prompts={handoff_id: answered.body},
            source_event_metadata={
                handoff_id: SourceEventMetadata(
                    sender=turn.facts.run_requester_id,
                    timestamp_ms=answered.server_timestamp,
                    discovery_event_id=answered.event_id,
                    speaker=turn.facts.physical_sender if turn.facts.acts_for_requester else None,
                ),
            },
            requester_id=turn.facts.run_requester_id,
        ),
        answers_event_id=answered.event_id,
    )
