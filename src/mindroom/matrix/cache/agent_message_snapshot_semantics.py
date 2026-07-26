"""Pure selection semantics for cached agent-message snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import nio

from mindroom.matrix.event_info import EventInfo, event_source_is_timeline_in_room
from mindroom.matrix.media import parse_room_message_event_source
from mindroom.matrix.replacements import replacement_content
from mindroom.matrix.thread_membership import local_events_prove_thread_root
from mindroom.matrix.thread_projection import resolve_thread_ids_for_event_infos

from .agent_message_snapshot import AgentMessageSnapshot, AgentMessageSnapshotUnavailable
from .event_cache_events import decode_cached_event
from .thread_cache_helpers import thread_cache_rejection_reason

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Awaitable, Callable

    from .event_cache import ThreadCacheState
    from .event_cache_events import CachedEventRow

    type _LatestEditLookup = Callable[[dict[str, Any]], Awaitable[CachedEventRow | None]]
    type _SnapshotScopeRow = tuple[str, float | None] | sqlite3.Row
    type _SnapshotScopeRowLookup = Callable[[], Awaitable[_SnapshotScopeRow | None]]


@dataclass(frozen=True, slots=True)
class _SnapshotLookupResult:
    """Outcome for one matching scope event during latest-message lookup."""

    snapshot: AgentMessageSnapshot | None
    stop_scanning: bool = False


def _thread_cache_has_no_snapshot(cache_state: ThreadCacheState | None) -> bool:
    """Return whether a thread has no snapshot, raising when cached state is unsafe."""
    rejection_reason = thread_cache_rejection_reason(cache_state)
    if rejection_reason in {"no_cache_state", "cache_never_validated"}:
        return True
    if rejection_reason is not None:
        msg = f"Thread cache snapshot is not usable: {rejection_reason}"
        raise AgentMessageSnapshotUnavailable(msg)
    return False


def _event_matches_snapshot_scope(
    event: dict[str, Any],
    *,
    room_id: str,
    thread_id: str | None,
    sender: str,
) -> bool:
    """Return whether one indexed event is a visible message candidate for a snapshot scope."""
    if event.get("sender") != sender or not _event_is_snapshot_graph_member(event, room_id=room_id):
        return False
    relation_type = EventInfo.from_event(event).relation_type
    return relation_type != "m.replace" and not (thread_id is None and relation_type == "m.thread")


def _event_is_snapshot_graph_member(event: dict[str, Any], *, room_id: str) -> bool:
    """Return whether one cached event may contribute timeline thread relations."""
    return (
        event_source_is_timeline_in_room(event, room_id)
        and event.get("type") == "m.room.message"
        and isinstance(parse_room_message_event_source(event), nio.RoomMessage)
    )


async def _resolved_snapshot_thread_event_ids(
    events: list[dict[str, Any]],
    *,
    room_id: str,
    thread_id: str,
) -> frozenset[str]:
    """Return events whose local relation graph resolves to the requested thread."""
    event_infos = {
        event_id: EventInfo.from_event(event)
        for event in events
        if _event_is_snapshot_graph_member(event, room_id=room_id)
        and isinstance(event_id := event.get("event_id"), str)
        and event_id
    }
    resolved = await resolve_thread_ids_for_event_infos(
        room_id,
        event_infos=event_infos,
        event_sources_by_event_id={
            event_id: event for event in events if isinstance(event_id := event.get("event_id"), str) and event_id
        },
        ordered_event_ids=list(event_infos),
        # Seed the root only once these events prove it is one. Seeding unconditionally would let a
        # plain reply to the root inherit membership in a thread that never established one.
        resolved_thread_ids={thread_id: thread_id} if local_events_prove_thread_root(thread_id, event_infos) else None,
    )
    return frozenset(event_id for event_id, resolved_thread_id in resolved.items() if resolved_thread_id == thread_id)


async def _snapshot_result_for_event(
    event: dict[str, Any],
    *,
    cached_at: float | None,
    latest_edit_lookup: _LatestEditLookup,
    room_id: str,
    thread_id: str | None,
    sender: str,
    runtime_started_at: float | None,
) -> _SnapshotLookupResult | None:
    """Return one matching event's snapshot outcome, or no scope match."""
    if not _event_matches_snapshot_scope(event, room_id=room_id, thread_id=thread_id, sender=sender):
        return None
    return _snapshot_lookup_result(
        event,
        latest_edit=await latest_edit_lookup(event),
        thread_id=thread_id,
        cached_at=cached_at,
        runtime_started_at=runtime_started_at,
    )


async def _next_decoded_snapshot_row(
    *,
    next_row: _SnapshotScopeRowLookup,
) -> CachedEventRow | None:
    """Decode the next cache row."""
    row = await next_row()
    return None if row is None else decode_cached_event(event_json=row[0], cached_at=row[1])


async def _load_room_agent_message_snapshot(
    *,
    latest_edit_lookup: _LatestEditLookup,
    next_row: _SnapshotScopeRowLookup,
    room_id: str,
    sender: str,
    runtime_started_at: float | None,
) -> AgentMessageSnapshot | None:
    """Stream one room-scoped query until its latest visible message is known."""
    while (decoded := await _next_decoded_snapshot_row(next_row=next_row)) is not None:
        result = await _snapshot_result_for_event(
            decoded.event,
            cached_at=decoded.cached_at,
            latest_edit_lookup=latest_edit_lookup,
            room_id=room_id,
            thread_id=None,
            sender=sender,
            runtime_started_at=runtime_started_at,
        )
        if result is None:
            continue
        if result.stop_scanning:
            return None
        if result.snapshot is not None:
            return result.snapshot
    return None


async def _load_thread_agent_message_snapshot(
    *,
    latest_edit_lookup: _LatestEditLookup,
    next_row: _SnapshotScopeRowLookup,
    room_id: str,
    thread_id: str,
    sender: str,
    runtime_started_at: float | None,
) -> AgentMessageSnapshot | None:
    """Resolve one complete indexed thread graph before selecting its latest message.

    Unlike the room scope this cannot stop at the first scope match. Membership can be indirect
    (a reply to a reply to a threaded event), and the ancestors that prove it are older, so they
    arrive after the candidate in this newest-first scan. Returning early would answer with a
    lower row whenever the newest row is an indirect member, so the whole indexed thread is
    resolved first and the newest proven member wins.
    """
    decoded_rows: list[CachedEventRow] = []
    while (decoded := await _next_decoded_snapshot_row(next_row=next_row)) is not None:
        decoded_rows.append(decoded)
    resolved_thread_event_ids = await _resolved_snapshot_thread_event_ids(
        [decoded.event for decoded in decoded_rows],
        room_id=room_id,
        thread_id=thread_id,
    )
    for decoded in decoded_rows:
        event = decoded.event
        if event.get("event_id") not in resolved_thread_event_ids:
            continue
        result = await _snapshot_result_for_event(
            event,
            cached_at=decoded.cached_at,
            latest_edit_lookup=latest_edit_lookup,
            room_id=room_id,
            thread_id=thread_id,
            sender=sender,
            runtime_started_at=runtime_started_at,
        )
        if result is None:
            continue
        if result.stop_scanning:
            return None
        if result.snapshot is not None:
            return result.snapshot
    return None


async def load_agent_message_snapshot(
    *,
    cache_state: ThreadCacheState | None,
    latest_edit_lookup: _LatestEditLookup,
    next_row: _SnapshotScopeRowLookup,
    room_id: str,
    thread_id: str | None,
    sender: str,
    runtime_started_at: float | None,
) -> AgentMessageSnapshot | None:
    """Return the latest valid visible message from one backend-neutral scope query."""
    if thread_id is None:
        return await _load_room_agent_message_snapshot(
            latest_edit_lookup=latest_edit_lookup,
            next_row=next_row,
            room_id=room_id,
            sender=sender,
            runtime_started_at=runtime_started_at,
        )
    if _thread_cache_has_no_snapshot(cache_state):
        return None
    return await _load_thread_agent_message_snapshot(
        latest_edit_lookup=latest_edit_lookup,
        next_row=next_row,
        room_id=room_id,
        thread_id=thread_id,
        sender=sender,
        runtime_started_at=runtime_started_at,
    )


def _snapshot_lookup_result(
    event: dict[str, Any],
    *,
    latest_edit: CachedEventRow | None,
    thread_id: str | None,
    cached_at: float | None,
    runtime_started_at: float | None,
) -> _SnapshotLookupResult:
    """Resolve one cached event and optional edit into a visible snapshot outcome."""
    latest_replacement = None if latest_edit is None else latest_edit.event
    visible_cached_at = latest_edit.cached_at if latest_edit and latest_edit.cached_at is not None else cached_at
    if (
        thread_id is None
        and runtime_started_at is not None
        and (visible_cached_at is None or visible_cached_at < runtime_started_at)
    ):
        return _SnapshotLookupResult(snapshot=None, stop_scanning=True)

    visible_content = dict(event["content"])
    if latest_replacement is not None:
        visible_content = replacement_content(visible_content, latest_replacement["content"]["m.new_content"])
    snapshot = AgentMessageSnapshot(content=visible_content, origin_server_ts=event["origin_server_ts"])
    return _SnapshotLookupResult(snapshot=snapshot)
