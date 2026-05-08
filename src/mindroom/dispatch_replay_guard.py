"""Replay-guard checks for dispatch sequencing."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mindroom.commands.parsing import command_parser
from mindroom.dispatch_source import is_automation_source_kind, is_voice_event
from mindroom.matrix.event_info import EventInfo

if TYPE_CHECKING:
    import structlog

    from mindroom.dispatch_handoff import TextDispatchEvent
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage

type _RequesterResolver = Callable[[str, object], str]
type _HandledLookup = Callable[[str], bool]
type _RecentRoomEventsLookup = Callable[..., Awaitable[Sequence[dict[str, Any]]]]
# Implementations fail open by returning None after logging lookup failures.
type _ThreadIdForEventLookup = Callable[[str, str], Awaitable[str | None]]


@dataclass(frozen=True)
class _CachedEventView:
    """Minimal event view used for trusted source-kind checks on raw cache rows."""

    sender: str
    source: Mapping[str, object]


def has_newer_unresponded_in_thread(
    event: TextDispatchEvent,
    requester_user_id: str,
    thread_history: Sequence[ResolvedVisibleMessage],
    *,
    source_kind: str | None,
    requester_user_id_for_event: _RequesterResolver,
    sender_is_trusted_for_ingress_metadata: Callable[[str], bool],
    is_handled: _HandledLookup,
    logger: structlog.stdlib.BoundLogger,
) -> bool:
    """Return True when full thread history proves a newer unhandled requester turn exists."""
    if is_automation_source_kind(source_kind or ""):
        return False
    event_ts = event.server_timestamp
    if event_ts is None or not thread_history:
        return False
    for message in thread_history:
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


async def _cached_event_is_in_thread(
    event_source: dict[str, Any],
    *,
    room_id: str,
    thread_id: str,
    get_thread_id_for_event: _ThreadIdForEventLookup | None,
) -> bool:
    """Return whether raw event metadata or the cache index proves thread membership."""
    event_info = EventInfo.from_event(event_source)
    if thread_id in {event_info.thread_id, event_info.thread_id_from_edit}:
        return True
    if get_thread_id_for_event is None:
        return False
    event_id = event_source.get("event_id")
    if not isinstance(event_id, str):
        return False
    return await get_thread_id_for_event(room_id, event_id) == thread_id


def _unresponded_requester_event_id(
    event_source: dict[str, Any],
    *,
    skipped_event_id: str,
    requester_user_id: str,
    requester_user_id_for_event: _RequesterResolver,
    sender_is_trusted_for_ingress_metadata: Callable[[str], bool],
    is_handled: _HandledLookup,
) -> str | None:
    """Return an unhandled requester event id from a cached event source when eligible."""
    if EventInfo.from_event(event_source).is_edit:
        return None
    event_id = event_source.get("event_id")
    sender = event_source.get("sender")
    if not isinstance(event_id, str) or event_id == skipped_event_id or not isinstance(sender, str):
        return None
    if requester_user_id_for_event(sender, event_source) != requester_user_id:
        return None
    if is_handled(event_id):
        return None
    content = event_source.get("content")
    body = content.get("body") if isinstance(content, dict) else None
    event_view = _CachedEventView(sender=sender, source=event_source)
    if (
        isinstance(body, str)
        and not is_voice_event(event_view, sender_is_trusted=sender_is_trusted_for_ingress_metadata)
        and command_parser.parse(body.strip()) is not None
    ):
        return None
    return event_id


async def _newer_unresponded_cached_thread_event_id(
    recent_events: Sequence[dict[str, Any]],
    *,
    room_id: str,
    skipped_event_id: str,
    requester_user_id: str,
    thread_id: str,
    get_thread_id_for_event: _ThreadIdForEventLookup | None,
    requester_user_id_for_event: _RequesterResolver,
    sender_is_trusted_for_ingress_metadata: Callable[[str], bool],
    is_handled: _HandledLookup,
) -> str | None:
    """Return the first newer cached event with positive same-thread proof."""
    for event_source in recent_events:
        if not await _cached_event_is_in_thread(
            event_source,
            room_id=room_id,
            thread_id=thread_id,
            get_thread_id_for_event=get_thread_id_for_event,
        ):
            continue
        event_id = _unresponded_requester_event_id(
            event_source,
            skipped_event_id=skipped_event_id,
            requester_user_id=requester_user_id,
            requester_user_id_for_event=requester_user_id_for_event,
            sender_is_trusted_for_ingress_metadata=sender_is_trusted_for_ingress_metadata,
            is_handled=is_handled,
        )
        if event_id is not None:
            return event_id
    return None


async def has_newer_unresponded_cached_thread_event(
    *,
    room_id: str,
    event: TextDispatchEvent,
    requester_user_id: str,
    thread_id: str | None,
    source_kind: str | None,
    get_recent_room_events: _RecentRoomEventsLookup | None,
    get_thread_id_for_event: _ThreadIdForEventLookup | None,
    requester_user_id_for_event: _RequesterResolver,
    sender_is_trusted_for_ingress_metadata: Callable[[str], bool],
    is_handled: _HandledLookup,
    logger: structlog.stdlib.BoundLogger,
) -> bool:
    """Return positive cached-event proof for degraded dispatch replay history."""
    # Automation backlog replay should not suppress older automation turns by scanning raw cached room events.
    if thread_id is None or event.server_timestamp is None or is_automation_source_kind(source_kind or ""):
        return False
    if get_recent_room_events is None:
        return False
    try:
        recent_events = await get_recent_room_events(
            room_id,
            event_type="m.room.message",
            since_ts_ms=int(event.server_timestamp),
        )
    except Exception as exc:
        logger.warning(
            "Failed to read cached room events for degraded thread replay guard",
            event_id=event.event_id,
            room_id=room_id,
            thread_id=thread_id,
            error=str(exc),
        )
        return False

    event_id = await _newer_unresponded_cached_thread_event_id(
        recent_events,
        room_id=room_id,
        skipped_event_id=event.event_id,
        requester_user_id=requester_user_id,
        thread_id=thread_id,
        get_thread_id_for_event=get_thread_id_for_event,
        requester_user_id_for_event=requester_user_id_for_event,
        sender_is_trusted_for_ingress_metadata=sender_is_trusted_for_ingress_metadata,
        is_handled=is_handled,
    )
    if event_id is not None:
        logger.info(
            "Skipping older message — newer cached event from same sender in degraded thread replay guard",
            skipped_event_id=event.event_id,
            newer_event_id=event_id,
            thread_id=thread_id,
        )
        return True
    return False
