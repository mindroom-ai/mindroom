"""Pure selection semantics for cached agent-message snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import nio

from mindroom.matrix.event_info import EventInfo, event_source_is_timeline_in_room
from mindroom.matrix.media import parse_room_message_event_source
from mindroom.matrix.replacements import bundled_replacement_candidates, replacement_content
from mindroom.matrix.thread_membership import local_events_prove_thread_root
from mindroom.matrix.thread_projection import resolve_thread_ids_for_event_infos

from .agent_message_snapshot import AgentMessageSnapshot, AgentMessageSnapshotUnavailable
from .thread_cache_helpers import thread_cache_rejection_reason

if TYPE_CHECKING:
    from .event_cache_events import CachedEventRow
    from .thread_cache_state import ThreadCacheGap


@dataclass(frozen=True, slots=True)
class SnapshotLookupResult:
    """Outcome for one matching scope event during latest-message lookup."""

    snapshot: AgentMessageSnapshot | None
    stop_scanning: bool = False


def reject_snapshot_scope_with_gap(gap: ThreadCacheGap | None) -> None:
    """Refuse a snapshot read for one thread whose durable state records a gap.

    A thread with no state row is not a gap: it simply has no snapshot yet, and the scan below
    finds nothing and returns nothing.
    """
    rejection_reason = thread_cache_rejection_reason(gap)
    if rejection_reason is not None:
        msg = f"Thread cache snapshot is not usable: {rejection_reason}"
        raise AgentMessageSnapshotUnavailable(msg)


def event_matches_snapshot_scope(
    event: dict[str, Any],
    *,
    room_id: str,
    thread_id: str | None,
    sender: str,
) -> bool:
    """Return whether one event is a visible message candidate for a snapshot scope."""
    if event.get("sender") != sender or not _event_is_snapshot_graph_member(event, room_id=room_id):
        return False
    relation_type = EventInfo.from_event(event).relation_type
    if relation_type == "m.replace":
        return False
    return not (thread_id is None and relation_type == "m.thread")


def _event_is_snapshot_graph_member(event: dict[str, Any], *, room_id: str) -> bool:
    """Return whether one cached event may contribute timeline thread relations."""
    return (
        event_source_is_timeline_in_room(event, room_id)
        and event.get("type") == "m.room.message"
        and isinstance(parse_room_message_event_source(event), nio.RoomMessage)
    )


async def resolved_snapshot_thread_event_ids(
    events: list[dict[str, Any]],
    *,
    room_id: str,
    thread_id: str,
) -> frozenset[str]:
    """Return the events whose local relation graph resolves to the requested thread.

    A thread-scoped snapshot cannot stop at the first row matching the scope. Membership can be
    indirect - a reply to a reply to a threaded event - and the ancestors that prove it are older,
    so they arrive after the candidate in a newest-first scan. Resolving the whole indexed graph
    first is what lets the newest proven member win rather than the newest directly-related one.
    """
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


def snapshot_event_id(event: dict[str, Any]) -> str | None:
    """Return one event's usable ID for snapshot edit lookup."""
    event_id = event.get("event_id")
    return event_id if isinstance(event_id, str) and event_id else None


def snapshot_lookup_result(
    event: dict[str, Any],
    *,
    latest_edit: CachedEventRow | None,
    thread_id: str | None,
    cached_at: float | None,
    runtime_started_at: float | None,
) -> SnapshotLookupResult:
    """Resolve one cached event and optional edit into a visible snapshot outcome."""
    latest_replacement = None if latest_edit is None else latest_edit.event
    original_observed_selected_edit = latest_replacement is None or any(
        candidate.get("event_id") == latest_replacement["event_id"]
        for candidate in bundled_replacement_candidates(event)
    )
    visible_cached_at = max(
        (
            observed_at
            for observed_at in (
                cached_at if original_observed_selected_edit else None,
                latest_edit.cached_at if latest_edit else None,
            )
            if observed_at is not None
        ),
        default=None,
    )
    if (
        thread_id is None
        and runtime_started_at is not None
        and (visible_cached_at is None or visible_cached_at < runtime_started_at)
    ):
        return SnapshotLookupResult(snapshot=None, stop_scanning=True)

    visible_content = dict(event["content"])
    if latest_replacement is not None:
        visible_content = replacement_content(visible_content, latest_replacement["content"]["m.new_content"])
    snapshot = AgentMessageSnapshot(content=visible_content, origin_server_ts=event["origin_server_ts"])
    return SnapshotLookupResult(snapshot=snapshot)
