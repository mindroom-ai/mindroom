"""Replay-guard checks for dispatch sequencing."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from typing import TYPE_CHECKING, cast

from mindroom.tool_system.events import body_without_tool_markers
from mindroom.turns.facts import command_for, is_router_voice_echo

if TYPE_CHECKING:
    import structlog

    from mindroom.dispatch_handoff import PreparedIngress
    from mindroom.event_journal import JournalEvent, PendingTurnView
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.turns.facts import IngressFacts

# Classify a stored item from its sender and Matrix source the way ingress classifies a live event.
type _Classify = Callable[[str, Mapping[str, object]], IngressFacts]
# Which of these events already have a terminal outcome, answered or redacted in the turn's room.
type _HandledLookup = Callable[[Collection[str]], Awaitable[frozenset[str]]]
# Whether a stored message, by event ID and Matrix source, may be for other agents or people only.
type _ForAnotherParticipant = Callable[[str, Mapping[str, object]], Awaitable[bool]]


async def has_newer_unresponded_in_thread(
    event: PreparedIngress,
    requester_user_id: str,
    thread_history: Sequence[ResolvedVisibleMessage],
    *,
    may_be_superseded_by_newer_requester_turn: bool,
    pending_turns: PendingTurnView,
    classify: _Classify,
    is_handled: _HandledLookup,
    for_another_participant: _ForAnotherParticipant,
    logger: structlog.stdlib.BoundLogger,
) -> bool:
    """Return True when full thread history proves a newer unhandled requester turn exists.

    Only a message the journal still owes work can become that turn: one this
    agent already settled without answering, such as a voice note for another
    agent ignored before a restart, never will.
    """
    if not may_be_superseded_by_newer_requester_turn:
        return False
    event_ts = event.server_timestamp
    if event_ts is None or not thread_history:
        return False
    newer = [
        message
        for message in thread_history
        if message.timestamp is not None and message.timestamp > event_ts and message.event_id != event.event_id
    ]
    pending = await pending_turns.pending_among([message.event_id for message in newer])
    newer = [message for message in newer if message.event_id in pending]
    handled = await is_handled([message.event_id for message in newer]) if newer else frozenset()
    for message in newer:
        if not await _is_unresponded_requester_event(
            message,
            source={"content": message.content},
            body=message.body,
            requester_user_id=requester_user_id,
            classify=classify,
            handled=handled,
            for_another_participant=for_another_participant,
        ):
            continue
        logger.info(
            "Skipping older message — newer unresponded message from same sender in thread",
            skipped_event_id=event.event_id,
            newer_event_id=message.event_id,
        )
        return True
    return False


async def _is_unresponded_requester_event(
    event: JournalEvent | ResolvedVisibleMessage,
    *,
    source: Mapping[str, object],
    body: object,
    requester_user_id: str,
    classify: _Classify,
    handled: Collection[str],
    for_another_participant: _ForAnotherParticipant,
) -> bool:
    """Return whether a normalized candidate is an unanswered requester turn.

    The filters SQL cannot express. Whose turn an event really is depends on
    trusted relay metadata inside its content, a router transcript echo is a
    display artifact rather than a turn, this agent ignores a message for
    another participant without ever answering it, and a command exits dispatch
    long before a response would be owed -- so none of the four can be decided
    from a column.

    ``handled`` stays even though a pending row is the journal's own answer
    to "unfinished". The two records settle at different moments: a source is
    handed to the outbox when its answer becomes durable, and the turn's own
    row records the terminal outcome separately, so a turn recorded there
    before a crash replays as a pending row afterwards. Trusting pending alone
    would let an already-answered message suppress an older one forever.
    """
    facts = classify(event.sender, source)
    content = source.get("content")
    if is_router_voice_echo(facts, content) or facts.lane_requester_id != requester_user_id:
        return False
    if event.event_id in handled or await for_another_participant(event.event_id, source):
        return False
    if not isinstance(body, str):
        return True
    command_text = (
        body_without_tool_markers(body, cast("Mapping[str, object]", content)) if isinstance(content, Mapping) else body
    )
    return command_for(facts, command_text) is None


async def has_newer_unresponded_journal_thread_event(
    *,
    room_id: str,
    event: PreparedIngress,
    requester_user_id: str,
    thread_id: str | None,
    may_be_superseded_by_newer_requester_turn: bool,
    pending_turns: PendingTurnView,
    classify: _Classify,
    is_handled: _HandledLookup,
    for_another_participant: _ForAnotherParticipant,
    logger: structlog.stdlib.BoundLogger,
) -> bool:
    """Return positive journal proof of a newer requester turn for degraded replay history.

    The negative-proof sibling of ``has_newer_unresponded_in_thread``, for
    turns whose thread history could not be read. It acts only on proof, so
    every way of not knowing -- an unreadable journal included -- means the
    older turn runs.
    """
    if thread_id is None or event.server_timestamp is None or not may_be_superseded_by_newer_requester_turn:
        return False
    try:
        candidates = await pending_turns.pending_thread_events_after(
            room_id=room_id,
            thread_id=thread_id,
            after_origin_server_ts=int(event.server_timestamp),
            excluding_event_id=event.event_id,
        )
    except Exception as exc:
        logger.warning(
            "Failed to read pending thread events for degraded thread replay guard",
            event_id=event.event_id,
            room_id=room_id,
            thread_id=thread_id,
            error=str(exc),
        )
        return False

    handled = await is_handled([candidate.event_id for candidate in candidates]) if candidates else frozenset()
    for candidate in candidates:
        content = candidate.source.get("content")
        body = cast("Mapping[str, object]", content).get("body") if isinstance(content, Mapping) else None
        if await _is_unresponded_requester_event(
            candidate,
            source=candidate.source,
            body=body,
            requester_user_id=requester_user_id,
            classify=classify,
            handled=handled,
            for_another_participant=for_another_participant,
        ):
            logger.info(
                "Skipping older message — newer pending journal event from same sender in degraded thread replay guard",
                skipped_event_id=event.event_id,
                newer_event_id=candidate.event_id,
                thread_id=thread_id,
            )
            return True
    return False
