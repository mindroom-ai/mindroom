"""Backend-neutral event values and index decisions for durable Matrix caches."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from mindroom.matrix.event_info import EventInfo

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


def preferred_cached_events_by_id(events: list[SerializedCachedEvent]) -> list[SerializedCachedEvent]:
    """Collapse duplicate IDs while preventing a later opaque payload from replacing clear data."""
    preferred_events: dict[str, SerializedCachedEvent] = {}
    for event in events:
        previous = preferred_events.get(event.event_id)
        event_type = event.event.get("type")
        if (
            previous is None
            or previous.event.get("type") == "m.room.encrypted"
            or (event_type is not None and event_type != "m.room.encrypted")
        ):
            preferred_events[event.event_id] = event
    return list(preferred_events.values())


def event_redaction_candidate_ids(event_id: str, event: dict[str, Any]) -> frozenset[str]:
    """Return IDs whose tombstones would prevent caching one event."""
    candidate_ids = {event_id}
    event_info = EventInfo.from_event(event)
    if event_info.is_edit and isinstance(event_info.original_event_id, str):
        candidate_ids.add(event_info.original_event_id)
    return frozenset(candidate_ids)


def batch_redaction_candidate_ids(events: list[_CachedEventValue]) -> frozenset[str]:
    """Return IDs whose tombstones would prevent caching any event in a batch."""
    return frozenset(
        candidate_id for event_id, event in events for candidate_id in event_redaction_candidate_ids(event_id, event)
    )


def filter_redacted_events(
    events: list[_CachedEventValue],
    *,
    redacted_event_ids: frozenset[str],
) -> list[_CachedEventValue]:
    """Drop events that are tombstoned or edit a tombstoned original."""
    if not redacted_event_ids:
        return events
    return [
        (event_id, event)
        for event_id, event in events
        if event_redaction_candidate_ids(event_id, event).isdisjoint(redacted_event_ids)
    ]


def redaction_removal_event_ids(event_id: str, dependent_edit_ids: list[str]) -> list[str]:
    """Return the deduplicated event IDs removed by one redaction."""
    return list(dict.fromkeys([event_id, *dependent_edit_ids]))


def cache_rows_were_deleted(*row_counts: int) -> bool:
    """Return whether a redaction deleted at least one cached row."""
    return any(row_count > 0 for row_count in row_counts)


def _event_thread_row(room_id: str, event: dict[str, Any]) -> _EventThreadRow | None:
    """Return an event-to-thread row when thread membership is explicit."""
    event_id = event.get("event_id")
    if not isinstance(event_id, str) or not event_id:
        return None
    event_info = EventInfo.from_event(event)
    thread_id = event_info.thread_id
    if not isinstance(thread_id, str):
        thread_id = event_info.thread_id_from_edit
    if not isinstance(thread_id, str) or not thread_id:
        return None
    return _EventThreadRow(room_id=room_id, event_id=event_id, thread_id=thread_id)


def _with_thread_root_self_rows(thread_rows: list[_EventThreadRow]) -> list[_EventThreadRow]:
    """Ensure learned thread membership also records each root's own row."""
    return list(
        dict.fromkeys(
            [
                *thread_rows,
                *(
                    _EventThreadRow(room_id=row.room_id, event_id=row.thread_id, thread_id=row.thread_id)
                    for row in thread_rows
                ),
            ],
        ),
    )


def _event_edit_row(room_id: str, event: dict[str, Any]) -> _EventEditRow | None:
    """Return an edit-index row when one cached event is an editable replacement."""
    if event.get("type") not in _EDITABLE_EVENT_TYPES:
        return None
    event_info = EventInfo.from_event(event)
    if not event_info.is_edit or not isinstance(event_info.original_event_id, str):
        return None
    return _EventEditRow(
        edit_event_id=event_id_for_cache(event),
        room_id=room_id,
        original_event_id=event_info.original_event_id,
        origin_server_ts=_event_timestamp_for_cache(event),
    )


def event_edit_rows(room_id: str, events: list[SerializedCachedEvent]) -> list[_EventEditRow]:
    """Return the edit-index rows derived from serialized events."""
    return [row for event in events if (row := _event_edit_row(room_id, event.event)) is not None]


def event_thread_rows(
    room_id: str,
    events: list[SerializedCachedEvent],
    *,
    thread_id: str | None,
) -> list[_EventThreadRow]:
    """Return root-complete event-to-thread rows derived from serialized events."""
    rows = (
        [_EventThreadRow(room_id=room_id, event_id=event.event_id, thread_id=thread_id) for event in events]
        if thread_id is not None
        else [row for event in events if (row := _event_thread_row(room_id, event.event)) is not None]
    )
    return _with_thread_root_self_rows(rows)
