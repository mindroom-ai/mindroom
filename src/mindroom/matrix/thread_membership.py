"""Shared direct-target thread membership resolution."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from mindroom.matrix.event_info import EventInfo

type ThreadIdLookup = Callable[[str, str], Awaitable[str | None]]
type EventInfoLookup = Callable[[str, str], Awaitable[EventInfo | None]]
type ThreadRootChildrenLookup = Callable[[str, str], Awaitable[bool]]


async def resolve_event_thread_id(
    room_id: str,
    event_info: EventInfo,
    *,
    lookup_thread_id: ThreadIdLookup,
    fetch_event_info: EventInfoLookup,
    thread_root_has_children: ThreadRootChildrenLookup,
) -> str | None:
    """Return the explicit or inherited thread membership for one event."""
    explicit_thread_id = event_info.thread_id or event_info.thread_id_from_edit
    if explicit_thread_id is not None:
        return explicit_thread_id
    if event_info.is_edit and event_info.original_event_id is not None:
        return await resolve_related_event_thread_id(
            room_id,
            event_info.original_event_id,
            lookup_thread_id=lookup_thread_id,
            fetch_event_info=fetch_event_info,
            thread_root_has_children=thread_root_has_children,
            allow_reply_hop=True,
        )
    if event_info.reply_to_event_id is not None:
        return await resolve_related_event_thread_id(
            room_id,
            event_info.reply_to_event_id,
            lookup_thread_id=lookup_thread_id,
            fetch_event_info=fetch_event_info,
            thread_root_has_children=thread_root_has_children,
            allow_reply_hop=False,
        )
    return None


async def resolve_related_event_thread_id(
    room_id: str,
    related_event_id: str,
    *,
    lookup_thread_id: ThreadIdLookup,
    fetch_event_info: EventInfoLookup,
    thread_root_has_children: ThreadRootChildrenLookup,
    allow_reply_hop: bool,
) -> str | None:
    """Return thread membership for one directly related target event."""
    thread_id = await lookup_thread_id(room_id, related_event_id)
    if thread_id is None:
        related_event_info = await fetch_event_info(room_id, related_event_id)
        if related_event_info is None:
            return None

        thread_id = related_event_info.thread_id or related_event_info.thread_id_from_edit
        if thread_id is None:
            if related_event_info.is_edit and related_event_info.original_event_id is not None:
                thread_id = await resolve_related_event_thread_id(
                    room_id,
                    related_event_info.original_event_id,
                    lookup_thread_id=lookup_thread_id,
                    fetch_event_info=fetch_event_info,
                    thread_root_has_children=thread_root_has_children,
                    allow_reply_hop=allow_reply_hop,
                )
            elif allow_reply_hop and related_event_info.reply_to_event_id is not None:
                thread_id = await resolve_related_event_thread_id(
                    room_id,
                    related_event_info.reply_to_event_id,
                    lookup_thread_id=lookup_thread_id,
                    fetch_event_info=fetch_event_info,
                    thread_root_has_children=thread_root_has_children,
                    allow_reply_hop=False,
                )
            elif related_event_info.can_be_thread_root and await thread_root_has_children(
                room_id,
                related_event_id,
            ):
                thread_id = related_event_id
    return thread_id
