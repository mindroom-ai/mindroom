"""Shared transitive thread membership resolution."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass

from mindroom.matrix.event_info import EventInfo

type ThreadIdLookup = Callable[[str, str], Awaitable[str | None]]
type EventInfoLookup = Callable[[str, str], Awaitable[EventInfo | None]]
type ThreadRootChildrenLookup = Callable[[str, str], Awaitable[bool]]
_MAX_THREAD_MEMBERSHIP_HOPS = 512


def _next_related_event_target(
    event_info: EventInfo,
    *,
    current_event_id: str,
) -> str | None:
    """Return the next related event to inspect."""
    return event_info.next_related_event_id(current_event_id)


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
    related_event_id = event_info.next_related_event_id("")
    if related_event_id is not None:
        return await resolve_related_event_thread_id(
            room_id,
            related_event_id,
            access=access,
        )
    return None


async def resolve_related_event_thread_id(
    room_id: str,
    related_event_id: str,
    *,
    access: ThreadMembershipAccess,
) -> str | None:
    """Return canonical thread membership for one related target event."""
    current_event_id = related_event_id
    resolved_thread_id: str | None = None
    visited_event_ids: set[str] = set()

    for _ in range(_MAX_THREAD_MEMBERSHIP_HOPS):
        if current_event_id in visited_event_ids:
            break
        visited_event_ids.add(current_event_id)

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
            current_event_id=current_event_id,
        )
        if next_target is not None:
            current_event_id = next_target
            continue

        if related_event_info.can_be_thread_root and await access.thread_root_has_children(
            room_id,
            current_event_id,
        ):
            resolved_thread_id = current_event_id

        break

    return resolved_thread_id


def map_backed_thread_membership_access(
    *,
    event_infos: Mapping[str, EventInfo],
    resolved_thread_ids: dict[str, str],
) -> ThreadMembershipAccess:
    """Return one thread-membership access adapter backed by in-memory event maps."""

    async def lookup_thread_id(_room_id: str, event_id: str) -> str | None:
        return resolved_thread_ids.get(event_id)

    async def fetch_event_info(_room_id: str, event_id: str) -> EventInfo | None:
        return event_infos.get(event_id)

    async def thread_root_has_children(_room_id: str, thread_root_id: str) -> bool:
        return any(
            event_id != thread_root_id
            and any(
                candidate_thread_id == thread_root_id
                for candidate_thread_id in (
                    event_info.thread_id,
                    event_info.thread_id_from_edit,
                )
            )
            for event_id, event_info in event_infos.items()
        )

    return ThreadMembershipAccess(
        lookup_thread_id=lookup_thread_id,
        fetch_event_info=fetch_event_info,
        thread_root_has_children=thread_root_has_children,
    )


async def resolve_thread_ids_for_event_infos(
    room_id: str,
    *,
    event_infos: Mapping[str, EventInfo],
    ordered_event_ids: Sequence[str],
    resolved_thread_ids: dict[str, str] | None = None,
) -> dict[str, str]:
    """Resolve canonical thread membership for one local event-info graph."""
    resolved = {} if resolved_thread_ids is None else resolved_thread_ids
    access = map_backed_thread_membership_access(
        event_infos=event_infos,
        resolved_thread_ids=resolved,
    )

    progress_made = True
    while progress_made:
        progress_made = False
        for event_id in ordered_event_ids:
            if event_id in resolved:
                continue
            event_info = event_infos.get(event_id)
            if event_info is None:
                continue
            thread_id = await resolve_event_thread_id(
                room_id,
                event_info,
                access=access,
            )
            if thread_id is None:
                continue
            resolved[event_id] = thread_id
            progress_made = True

    return resolved
