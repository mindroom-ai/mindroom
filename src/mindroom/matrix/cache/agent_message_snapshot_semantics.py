"""Pure selection semantics for cached agent-message snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mindroom.matrix.event_info import (
    EventInfo,
    event_source_is_state_event,
    event_source_matches_room,
    replacement_content_for_original,
)

from .agent_message_snapshot import AgentMessageSnapshot, AgentMessageSnapshotUnavailable
from .thread_cache_helpers import thread_cache_rejection_reason

if TYPE_CHECKING:
    from .event_cache import ThreadCacheState
    from .event_cache_events import CachedEventRow

_THREAD_CACHE_REJECTION_NONE_REASONS = frozenset({"no_cache_state", "cache_never_validated"})


@dataclass(frozen=True, slots=True)
class SnapshotLookupResult:
    """Outcome for one matching scope event during latest-message lookup."""

    snapshot: AgentMessageSnapshot | None
    stop_scanning: bool = False


def thread_cache_has_no_snapshot(cache_state: ThreadCacheState | None) -> bool:
    """Return whether a thread has no snapshot, raising when cached state is unsafe."""
    rejection_reason = thread_cache_rejection_reason(cache_state)
    if rejection_reason in _THREAD_CACHE_REJECTION_NONE_REASONS:
        return True
    if rejection_reason is not None:
        msg = f"Thread cache snapshot is not usable: {rejection_reason}"
        raise AgentMessageSnapshotUnavailable(msg)
    return False


def event_matches_snapshot_scope(
    event: dict[str, Any],
    *,
    room_id: str,
    thread_id: str | None,
    sender: str,
) -> bool:
    """Return whether one event is a visible message candidate for a snapshot scope."""
    if (
        event.get("type") != "m.room.message"
        or event.get("sender") != sender
        or event_source_is_state_event(event)
        or not event_source_matches_room(event, room_id)
    ):
        return False
    relation_type = EventInfo.from_event(event).relation_type
    if relation_type == "m.replace":
        return False
    return not (thread_id is None and relation_type == "m.thread")


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
    visible_cached_at = latest_edit.cached_at if latest_edit is not None else cached_at
    if (
        thread_id is None
        and runtime_started_at is not None
        and (visible_cached_at is None or visible_cached_at < runtime_started_at)
    ):
        return SnapshotLookupResult(snapshot=None, stop_scanning=True)

    timestamp = event.get("origin_server_ts")
    if not isinstance(timestamp, int) or isinstance(timestamp, bool):
        return SnapshotLookupResult(snapshot=None)
    original_content = event.get("content")
    normalized_original_content = dict(original_content) if isinstance(original_content, dict) else {}
    visible_content = normalized_original_content
    if latest_edit is not None:
        edit_content = latest_edit.event.get("content")
        new_content = edit_content.get("m.new_content") if isinstance(edit_content, dict) else None
        if isinstance(new_content, dict):
            visible_content = replacement_content_for_original(normalized_original_content, new_content)
    return SnapshotLookupResult(
        snapshot=AgentMessageSnapshot(
            content=visible_content,
            origin_server_ts=timestamp,
        ),
    )
