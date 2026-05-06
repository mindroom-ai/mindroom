"""Pure Matrix event batch grouping helpers for cache backends."""

from __future__ import annotations

from typing import Any

from .event_normalization import normalize_event_source_for_cache


def group_lookup_events_by_room(
    events: list[tuple[str, str, dict[str, Any]]],
) -> dict[str, list[tuple[str, dict[str, Any]]]]:
    """Return normalized lookup events grouped by room encounter order."""
    events_by_room: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for event_id, room_id, event_data in events:
        normalized_event = normalize_event_source_for_cache(event_data, event_id=event_id)
        events_by_room.setdefault(room_id, []).append((event_id, normalized_event))
    return events_by_room
