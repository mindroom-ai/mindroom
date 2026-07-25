"""Pure selection semantics for cached agent-message snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import nio

from mindroom.matrix.event_info import EventInfo, event_source_is_state_event
from mindroom.matrix.media import parse_room_message_event_source, valid_room_message_replacement
from mindroom.matrix.replacements import ordered_replacements, replacement_content
from mindroom.matrix.thread_membership import event_info_proves_thread_membership

from .agent_message_snapshot import AgentMessageSnapshot, AgentMessageSnapshotUnavailable
from .thread_cache_helpers import thread_cache_rejection_reason

if TYPE_CHECKING:
    from .event_cache import ThreadCacheState
    from .event_cache_events import CachedEventRow


@dataclass(frozen=True, slots=True)
class SnapshotLookupResult:
    """Outcome for one matching scope event during latest-message lookup."""

    snapshot: AgentMessageSnapshot | None
    stop_scanning: bool = False


def thread_cache_has_no_snapshot(cache_state: ThreadCacheState | None) -> bool:
    """Return whether a thread has no snapshot, raising when cached state is unsafe."""
    rejection_reason = thread_cache_rejection_reason(cache_state)
    if rejection_reason in {"no_cache_state", "cache_never_validated"}:
        return True
    if rejection_reason is not None:
        msg = f"Thread cache snapshot is not usable: {rejection_reason}"
        raise AgentMessageSnapshotUnavailable(msg)
    return False


def event_matches_snapshot_scope(
    event: dict[str, Any],
    *,
    thread_id: str | None,
    sender: str,
) -> bool:
    """Return whether one event is a visible message candidate for a snapshot scope."""
    if (
        event.get("type") != "m.room.message"
        or event.get("sender") != sender
        or event_source_is_state_event(event)
        or not isinstance(parse_room_message_event_source(event), nio.RoomMessage)
    ):
        return False
    event_info = EventInfo.from_event(event)
    if event_info.relation_type == "m.replace" or (thread_id is None and event_info.relation_type == "m.thread"):
        return False
    if thread_id is None:
        return True
    event_id = event.get("event_id")
    return (
        isinstance(event_id, str)
        and bool(event_id)
        and event_info_proves_thread_membership(event_info, event_id, thread_id)
    )


def snapshot_lookup_result(
    event: dict[str, Any],
    *,
    latest_edit: CachedEventRow | None,
    room_id: str,
    thread_id: str | None,
    cached_at: float | None,
    runtime_started_at: float | None,
) -> SnapshotLookupResult:
    """Resolve one cached event and optional edit into a visible snapshot outcome."""
    replacements = ordered_replacements(
        event,
        () if latest_edit is None else (latest_edit.event,),
        room_id=room_id,
        validator=valid_room_message_replacement,
    )
    latest_replacement = next(iter(replacements), None)
    visible_cached_at = latest_edit.cached_at if latest_edit and latest_replacement == latest_edit.event else cached_at
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
