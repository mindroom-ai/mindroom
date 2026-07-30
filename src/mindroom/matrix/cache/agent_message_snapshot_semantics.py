"""Pure selection semantics for cached agent-message snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.visible_body import visible_content_from_content

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
    thread_id: str | None,
    sender: str,
) -> bool:
    """Return whether one event is a visible message candidate for a snapshot scope."""
    if event.get("type") != "m.room.message" or event.get("sender") != sender:
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
    latest_event = latest_edit.event if latest_edit is not None else event
    visible_cached_at = latest_edit.cached_at if latest_edit is not None else cached_at
    if (
        thread_id is None
        and runtime_started_at is not None
        and (visible_cached_at is None or visible_cached_at < runtime_started_at)
    ):
        return SnapshotLookupResult(snapshot=None, stop_scanning=True)

    timestamp = latest_event.get("origin_server_ts")
    if not isinstance(timestamp, int) or isinstance(timestamp, bool):
        return SnapshotLookupResult(snapshot=None)
    content = latest_event.get("content")
    visible_content = visible_content_from_content(content) if isinstance(content, dict) else {}
    return SnapshotLookupResult(
        snapshot=AgentMessageSnapshot(
            content=visible_content,
            origin_server_ts=timestamp,
        ),
    )
