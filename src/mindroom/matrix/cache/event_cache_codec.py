"""Normalization and serialization helpers for Matrix event cache rows."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

    import nio


_RUNTIME_ONLY_EVENT_SOURCE_KEYS = frozenset({"com.mindroom.dispatch_pipeline_timing"})


@dataclass(frozen=True, slots=True)
class SerializedCachedEvent:
    """One normalized cached event plus its serialized storage row."""

    event_id: str
    origin_server_ts: int
    event_json: str
    event: dict[str, Any]


def event_id_for_cache(event: dict[str, Any]) -> str:
    """Return the required event ID from one normalized cached event."""
    event_id = event.get("event_id")
    if isinstance(event_id, str) and event_id:
        return event_id
    msg = "Cached Matrix event is missing event_id"
    raise ValueError(msg)


def event_timestamp_for_cache(event: dict[str, Any]) -> int:
    """Return the required origin-server timestamp from one normalized cached event."""
    timestamp = event.get("origin_server_ts")
    if isinstance(timestamp, int) and not isinstance(timestamp, bool):
        return timestamp
    msg = f"Cached Matrix event {event_id_for_cache(event)} is missing origin_server_ts"
    raise ValueError(msg)


def serialize_cached_event(event_id: str, event: dict[str, Any]) -> SerializedCachedEvent:
    """Serialize one normalized cached event for SQLite writes."""
    return SerializedCachedEvent(
        event_id=event_id,
        origin_server_ts=event_timestamp_for_cache(event),
        event_json=json.dumps(event, separators=(",", ":")),
        event=event,
    )


def serialize_cacheable_events(
    cacheable_events: list[tuple[str, dict[str, Any]]],
) -> list[SerializedCachedEvent]:
    """Serialize one batch of normalized cacheable events."""
    return [serialize_cached_event(event_id, event) for event_id, event in cacheable_events]


def normalize_event_source_for_cache(
    event_source: Mapping[str, Any],
    *,
    event_id: str | None = None,
    sender: str | None = None,
    origin_server_ts: int | None = None,
) -> dict[str, Any]:
    """Normalize one raw Matrix event payload for persistent cache storage."""
    source = {key: value for key, value in event_source.items() if key not in _RUNTIME_ONLY_EVENT_SOURCE_KEYS}
    if "event_id" not in source and isinstance(event_id, str):
        source["event_id"] = event_id
    if "sender" not in source and isinstance(sender, str):
        source["sender"] = sender
    if (
        "origin_server_ts" not in source
        and isinstance(origin_server_ts, int)
        and not isinstance(origin_server_ts, bool)
    ):
        source["origin_server_ts"] = origin_server_ts
    return source


def normalize_nio_event_for_cache(
    event: nio.Event,
    *,
    event_id: str | None = None,
) -> dict[str, Any]:
    """Normalize one nio event for persistent cache storage."""
    event_source = event.source if isinstance(event.source, dict) else {}
    server_timestamp = event.server_timestamp
    return normalize_event_source_for_cache(
        event_source,
        event_id=event.event_id if isinstance(event.event_id, str) else event_id,
        sender=event.sender if isinstance(event.sender, str) else None,
        origin_server_ts=server_timestamp
        if isinstance(server_timestamp, int) and not isinstance(server_timestamp, bool)
        else None,
    )
