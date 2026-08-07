"""Replay-guard checks for dispatch sequencing."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, cast

from mindroom.commands.parsing import command_parser
from mindroom.dispatch_source import is_voice_event

if TYPE_CHECKING:
    import structlog

    from mindroom.dispatch_handoff import TextDispatchEvent
    from mindroom.event_journal import JournalEvent, PendingTurnView
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage

type _RequesterResolver = Callable[[str, object], str]
type _HandledLookup = Callable[[str], bool]
type _VisibleRouterVoiceEchoLookup = Callable[[str, object], bool]


def has_newer_unresponded_in_thread(
    event: TextDispatchEvent,
    requester_user_id: str,
    thread_history: Sequence[ResolvedVisibleMessage],
    *,
    may_be_superseded_by_newer_requester_turn: bool,
    requester_user_id_for_event: _RequesterResolver,
    is_visible_router_voice_echo: _VisibleRouterVoiceEchoLookup,
    sender_is_trusted_for_ingress_metadata: Callable[[str], bool],
    is_handled: _HandledLookup,
    logger: structlog.stdlib.BoundLogger,
) -> bool:
    """Return True when full thread history proves a newer unhandled requester turn exists."""
    if not may_be_superseded_by_newer_requester_turn:
        return False
    event_ts = event.server_timestamp
    if event_ts is None or not thread_history:
        return False
    for message in thread_history:
        if is_visible_router_voice_echo(message.sender, message.content):
            continue
        if (
            requester_user_id_for_event(
                message.sender,
                {"content": message.content},
            )
            != requester_user_id
        ):
            continue
        if message.timestamp is None or message.timestamp <= event_ts:
            continue
        if message.event_id == event.event_id:
            continue
        if is_handled(message.event_id):
            continue
        if (
            message.body
            and isinstance(message.body, str)
            and not is_voice_event(message, sender_is_trusted=sender_is_trusted_for_ingress_metadata)
            and command_parser.parse(message.body.strip()) is not None
        ):
            continue
        logger.info(
            "Skipping older message — newer unresponded message from same sender in thread",
            skipped_event_id=event.event_id,
            newer_event_id=message.event_id,
        )
        return True
    return False


def _unresponded_requester_event_id(
    journal_event: JournalEvent,
    *,
    requester_user_id: str,
    requester_user_id_for_event: _RequesterResolver,
    is_visible_router_voice_echo: _VisibleRouterVoiceEchoLookup,
    sender_is_trusted_for_ingress_metadata: Callable[[str], bool],
    is_handled: _HandledLookup,
) -> str | None:
    """Return an unanswered requester event id from one pending journal event, when eligible.

    The filters SQL cannot express. Whose turn an event really is depends on
    trusted relay metadata inside its content, a router transcript echo is a
    display artifact rather than a turn, and a command exits dispatch long
    before a response would be owed -- so none of the three can be decided from
    a column.

    ``is_handled`` stays even though a pending row is the journal's own answer
    to "unfinished". The two records settle at different moments: a source is
    handed to the outbox when its answer becomes durable, and the handled-turn
    ledger records the terminal outcome separately, so a turn recorded there
    before a crash replays as a pending row afterwards. Trusting pending alone
    would let an already-answered message suppress an older one forever.
    """
    sender = journal_event.sender
    content = journal_event.source.get("content")
    if (
        is_visible_router_voice_echo(sender, content)
        or requester_user_id_for_event(sender, journal_event.source) != requester_user_id
    ):
        return None
    if is_handled(journal_event.event_id):
        return None
    body = cast("Mapping[str, object]", content).get("body") if isinstance(content, Mapping) else None
    if (
        isinstance(body, str)
        and not is_voice_event(journal_event, sender_is_trusted=sender_is_trusted_for_ingress_metadata)
        and command_parser.parse(body.strip()) is not None
    ):
        return None
    return journal_event.event_id


async def has_newer_unresponded_journal_thread_event(
    *,
    room_id: str,
    event: TextDispatchEvent,
    requester_user_id: str,
    thread_id: str | None,
    may_be_superseded_by_newer_requester_turn: bool,
    pending_turns: PendingTurnView,
    requester_user_id_for_event: _RequesterResolver,
    is_visible_router_voice_echo: _VisibleRouterVoiceEchoLookup,
    sender_is_trusted_for_ingress_metadata: Callable[[str], bool],
    is_handled: _HandledLookup,
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

    for candidate in candidates:
        newer_event_id = _unresponded_requester_event_id(
            candidate,
            requester_user_id=requester_user_id,
            requester_user_id_for_event=requester_user_id_for_event,
            is_visible_router_voice_echo=is_visible_router_voice_echo,
            sender_is_trusted_for_ingress_metadata=sender_is_trusted_for_ingress_metadata,
            is_handled=is_handled,
        )
        if newer_event_id is not None:
            logger.info(
                "Skipping older message — newer pending journal event from same sender in degraded thread replay guard",
                skipped_event_id=event.event_id,
                newer_event_id=newer_event_id,
                thread_id=thread_id,
            )
            return True
    return False
