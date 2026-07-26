"""Backend-neutral event values and index decisions for durable Matrix caches."""

from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mindroom.matrix.event_info import (
    EventInfo,
    event_source_is_timeline_in_room,
    event_source_matches_room,
)
from mindroom.matrix.media import event_source_supports_valid_thread_relations
from mindroom.matrix.replacements import bundled_replacement_candidates
from mindroom.matrix.sidecar_content import sidecar_mxc_url

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

_EDITABLE_EVENT_TYPES = frozenset({"m.room.message", "io.mindroom.tool_approval"})

type _CachedEventValue = tuple[str, dict[str, Any]]


@dataclass(frozen=True, slots=True)
class SerializedCachedEvent:
    """One normalized cached event plus its serialized storage values."""

    event_id: str
    origin_server_ts: int
    event_json: str
    event: dict[str, Any]


@dataclass(frozen=True, slots=True)
class CachedEventRow:
    """One cached event payload plus the time its visible row was written."""

    event: dict[str, Any]
    cached_at: float | None


def decode_cached_event(
    *,
    event_json: str,
    cached_at: float | None = None,
) -> CachedEventRow:
    """Decode one cached event row."""
    return CachedEventRow(
        event=json.loads(event_json),
        cached_at=None if cached_at is None else float(cached_at),
    )


@dataclass(frozen=True, slots=True)
class _EventThreadRow:
    """One backend-neutral event-to-thread index row."""

    room_id: str
    event_id: str
    thread_id: str


@dataclass(frozen=True, slots=True)
class _EventEditRow:
    """One backend-neutral event-edit index row."""

    edit_event_id: str
    room_id: str
    original_event_id: str
    origin_server_ts: int


def event_id_for_cache(event: dict[str, Any]) -> str:
    """Return the required event ID from one normalized cached event."""
    event_id = event.get("event_id")
    if isinstance(event_id, str) and event_id:
        return event_id
    msg = "Cached Matrix event is missing event_id"
    raise ValueError(msg)


def _event_timestamp_for_cache(event: dict[str, Any]) -> int:
    """Return the required origin-server timestamp from one normalized cached event."""
    timestamp = event.get("origin_server_ts")
    if isinstance(timestamp, int) and not isinstance(timestamp, bool):
        return timestamp
    msg = f"Cached Matrix event {event_id_for_cache(event)} is missing origin_server_ts"
    raise ValueError(msg)


def serialize_cached_event(event_id: str, event: dict[str, Any]) -> SerializedCachedEvent:
    """Return the storage values for one normalized cached event."""
    return SerializedCachedEvent(
        event_id=event_id,
        origin_server_ts=_event_timestamp_for_cache(event),
        event_json=json.dumps(event, separators=(",", ":")),
        event=event,
    )


def serialize_cacheable_events(cacheable_events: list[_CachedEventValue]) -> list[SerializedCachedEvent]:
    """Return serialized storage values for normalized cacheable events."""
    return [serialize_cached_event(event_id, event) for event_id, event in cacheable_events]


def event_mxc_urls(event: dict[str, Any], *, room_id: str) -> frozenset[str]:
    """Return room-scoped sidecar MXCs referenced by one visible event."""
    content = event.get("content")
    if (
        event.get("type") != "m.room.message"
        or not event_source_is_timeline_in_room(event, room_id)
        or not isinstance(content, dict)
    ):
        return frozenset()
    return frozenset(
        mxc_url
        for candidate_content in (content, content.get("m.new_content"))
        if isinstance(candidate_content, dict)
        if (mxc_url := sidecar_mxc_url(candidate_content)) is not None
    )


def cached_event_owns_mxc(
    *,
    event_json: str,
    room_id: str,
    mxc_url: str,
) -> bool:
    """Return whether one visible event owns an MXC reference."""
    return mxc_url in event_mxc_urls(decode_cached_event(event_json=event_json).event, room_id=room_id)


def validated_mxc_text_rows(rows: Iterable[Sequence[Any]], *, room_id: str) -> dict[tuple[str, str], str]:
    """Return plaintext rows whose event payload still owns the requested MXC.

    Rows are ``(event_id, mxc_url, text_content, event_json)`` in that order.
    """
    return {
        (event_id, mxc_url): text_content
        for event_id, mxc_url, text_content, event_json in rows
        if cached_event_owns_mxc(
            event_json=event_json,
            room_id=room_id,
            mxc_url=mxc_url,
        )
    }


def _bundled_replacement_event_ids(event: dict[str, Any]) -> frozenset[str]:
    """Return every event ID carried by bundled replacement metadata."""
    return frozenset(
        candidate["event_id"]
        for candidate in bundled_replacement_candidates(event)
        if isinstance(candidate.get("event_id"), str)
    )


def _direct_redaction_candidate_ids(event_id: str, event: dict[str, Any], room_id: str) -> frozenset[str]:
    """Return tombstones that suppress this event rather than only its bundled preview."""
    original_event_id = EventInfo.from_event(event).original_event_id
    if (
        event.get("type") in _EDITABLE_EVENT_TYPES
        and event_source_is_timeline_in_room(event, room_id)
        and isinstance(original_event_id, str)
    ):
        return frozenset((event_id, original_event_id))
    return frozenset((event_id,))


def batch_redaction_candidate_ids(events: list[_CachedEventValue], room_id: str) -> frozenset[str]:
    """Return IDs whose tombstones would prevent caching any event in a batch."""
    return frozenset().union(
        *(
            _direct_redaction_candidate_ids(event_id, event, room_id) | _bundled_replacement_event_ids(event)
            for event_id, event in events
        ),
    )


def room_scoped_cache_events(
    events: list[_CachedEventValue],
    room_id: str,
) -> list[_CachedEventValue]:
    """Keep events whose explicit room agrees with their authoritative cache scope."""
    return [(event_id, event) for event_id, event in events if event_source_matches_room(event, room_id)]


def _without_tombstoned_bundled_replacements(
    event: dict[str, Any],
    redacted_event_ids: frozenset[str],
) -> dict[str, Any]:
    """Return one event whose bundled aggregation keeps only its untombstoned shapes.

    ``bundled_replacement_candidates`` treats the nested ``latest_event`` and ``event`` shapes and
    the aggregation itself as candidates that compete on their own identity, so redaction has to
    tombstone them the same way. Dropping the whole aggregation because one shape died would hide
    a surviving replacement that selection would otherwise have chosen.
    """
    sanitized = deepcopy(event)
    relations = sanitized["unsigned"]["m.relations"]
    bundled = relations["m.replace"]
    for key in ("latest_event", "event"):
        nested = bundled.get(key)
        if isinstance(nested, Mapping) and nested.get("event_id") in redacted_event_ids:
            del bundled[key]
    if bundled.get("event_id") in redacted_event_ids:
        # Strip only the dead wrapper identity. Rewriting the aggregation to one surviving nested
        # shape would silently pick a winner instead of letting the survivors compete on timestamp.
        del bundled["event_id"]
    if not _bundled_replacement_event_ids(sanitized):
        del relations["m.replace"]
    return sanitized


def scrub_bundled_replacement_json(event_json: str, event_id: str) -> str:
    """Remove one bundled replacement identity while preserving surviving candidates."""
    sanitized = _without_tombstoned_bundled_replacements(
        json.loads(event_json),
        frozenset((event_id,)),
    )
    return json.dumps(sanitized, separators=(",", ":"))


def filter_redacted_events(
    events: list[_CachedEventValue],
    *,
    room_id: str,
    redacted_event_ids: frozenset[str],
) -> list[_CachedEventValue]:
    """Drop tombstoned events and sanitize bundled replacements with tombstones."""
    retained: list[_CachedEventValue] = []
    for event_id, event in events:
        direct_ids = _direct_redaction_candidate_ids(event_id, event, room_id)
        if event.get("type") == "m.room.redaction" or direct_ids & redacted_event_ids:
            continue
        sanitized = (
            _without_tombstoned_bundled_replacements(event, redacted_event_ids)
            if not _bundled_replacement_event_ids(event).isdisjoint(redacted_event_ids)
            else event
        )
        retained.append((event_id, sanitized))
    return retained


def redaction_removal_event_ids(event_id: str, dependent_edit_ids: list[str]) -> list[str]:
    """Return the deduplicated event IDs removed by one redaction."""
    return list(dict.fromkeys([event_id, *dependent_edit_ids]))


def cache_rows_were_deleted(*row_counts: int) -> bool:
    """Return whether a redaction deleted at least one cached row."""
    return any(row_count > 0 for row_count in row_counts)


def _event_thread_row(room_id: str, event: SerializedCachedEvent) -> _EventThreadRow | None:
    """Return an event-to-thread row when thread membership is explicit."""
    if not _event_can_supply_thread_index(event.event, room_id=room_id):
        return None
    event_info = EventInfo.from_event(event.event)
    thread_id = event_info.thread_id
    if not thread_id:
        return None
    return _EventThreadRow(room_id=room_id, event_id=event.event_id, thread_id=thread_id)


def _event_can_supply_thread_index(event: dict[str, Any], *, room_id: str) -> bool:
    """Return whether one timeline event may create durable thread-index rows."""
    return event_source_supports_valid_thread_relations(event, room_id)


def _with_thread_root_self_rows(thread_rows: list[_EventThreadRow]) -> list[_EventThreadRow]:
    """Ensure learned thread membership also records each root's own row."""
    root_rows = (
        _EventThreadRow(room_id=row.room_id, event_id=row.thread_id, thread_id=row.thread_id) for row in thread_rows
    )
    return list(dict.fromkeys((*thread_rows, *root_rows)))


def _event_edit_row(room_id: str, event: SerializedCachedEvent) -> _EventEditRow | None:
    """Return an edit-index row when one cached event is an editable replacement."""
    if event.event.get("type") not in _EDITABLE_EVENT_TYPES or not event_source_is_timeline_in_room(
        event.event,
        room_id,
    ):
        return None
    event_info = EventInfo.from_event(event.event)
    if not event_info.is_edit or not isinstance(event_info.original_event_id, str):
        return None
    return _EventEditRow(
        edit_event_id=event.event_id,
        room_id=room_id,
        original_event_id=event_info.original_event_id,
        origin_server_ts=event.origin_server_ts,
    )


def event_edit_rows(room_id: str, events: list[SerializedCachedEvent]) -> list[_EventEditRow]:
    """Return the edit-index rows derived from serialized events."""
    return [row for event in events if (row := _event_edit_row(room_id, event)) is not None]


def event_thread_rows(
    room_id: str,
    events: list[SerializedCachedEvent],
    *,
    thread_id: str | None,
) -> list[_EventThreadRow]:
    """Return root-complete event-to-thread rows derived from serialized events."""
    rows = (
        [
            _EventThreadRow(room_id=room_id, event_id=event.event_id, thread_id=thread_id)
            for event in events
            if _event_can_supply_thread_index(event.event, room_id=room_id)
        ]
        if thread_id is not None
        else [row for event in events if (row := _event_thread_row(room_id, event)) is not None]
    )
    return _with_thread_root_self_rows(rows)
