"""Shared direct-target thread membership resolution."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from mindroom.matrix.event_info import EventInfo

type ThreadIdLookup = Callable[[str, str], Awaitable[str | None]]
type EventInfoLookup = Callable[[str, str], Awaitable[EventInfo | None]]
type ThreadRootChildrenLookup = Callable[[str, str], Awaitable[bool]]
_MAX_THREAD_MEMBERSHIP_HOPS = 32


def _next_related_event_target(
    event_info: EventInfo,
    *,
    allow_reply_hop: bool,
) -> tuple[str, bool] | None:
    """Return the next related event to inspect, preserving the one-hop reply rule."""
    if event_info.is_edit and event_info.original_event_id is not None:
        return event_info.original_event_id, allow_reply_hop
    if allow_reply_hop and event_info.reply_to_event_id is not None:
        return event_info.reply_to_event_id, False
    return None


@dataclass(frozen=True)
class ThreadMembershipAccess:
    """Repository-wide accessors used to resolve one event's thread membership."""

    lookup_thread_id: ThreadIdLookup
    fetch_event_info: EventInfoLookup
    thread_root_has_children: ThreadRootChildrenLookup


async def resolve_event_thread_id(
    room_id: str,
    event_info: EventInfo,
    *,
    access: ThreadMembershipAccess,
) -> str | None:
    """Return the explicit or inherited thread membership for one event."""
    explicit_thread_id = event_info.thread_id or event_info.thread_id_from_edit
    if explicit_thread_id is not None:
        return explicit_thread_id
    if event_info.is_edit and event_info.original_event_id is not None:
        return await resolve_related_event_thread_id(
            room_id,
            event_info.original_event_id,
            access=access,
            allow_reply_hop=True,
            allow_cached_membership=True,
        )
    if event_info.reply_to_event_id is not None:
        return await resolve_related_event_thread_id(
            room_id,
            event_info.reply_to_event_id,
            access=access,
            allow_reply_hop=False,
            allow_cached_membership=False,
        )
    return None


async def resolve_related_event_thread_id(
    room_id: str,
    related_event_id: str,
    *,
    access: ThreadMembershipAccess,
    allow_reply_hop: bool,
    allow_cached_membership: bool,
) -> str | None:
    """Return thread membership for one directly related target event."""
    current_event_id = related_event_id
    current_allow_reply_hop = allow_reply_hop
    current_allow_cached_membership = allow_cached_membership
    resolved_thread_id: str | None = None
    visited_event_ids: set[str] = set()

    for _ in range(_MAX_THREAD_MEMBERSHIP_HOPS):
        if current_event_id in visited_event_ids:
            break
        visited_event_ids.add(current_event_id)

        if current_allow_cached_membership:
            thread_id = await access.lookup_thread_id(room_id, current_event_id)
            if thread_id is not None:
                resolved_thread_id = thread_id
                break

        related_event_info = await access.fetch_event_info(room_id, current_event_id)
        if related_event_info is None:
            break

        thread_id = related_event_info.thread_id or related_event_info.thread_id_from_edit
        if thread_id is not None:
            resolved_thread_id = thread_id
            break

        next_target = _next_related_event_target(
            related_event_info,
            allow_reply_hop=current_allow_reply_hop,
        )
        if next_target is not None:
            current_event_id, current_allow_reply_hop = next_target
            if related_event_info.reply_to_event_id == current_event_id:
                current_allow_cached_membership = False
            continue

        if related_event_info.can_be_thread_root and await access.thread_root_has_children(
            room_id,
            current_event_id,
        ):
            resolved_thread_id = current_event_id

        break

    return resolved_thread_id
