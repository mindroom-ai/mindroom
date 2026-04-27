"""Storage-agnostic Matrix event payload normalization for cache backends."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

    import nio

_RUNTIME_ONLY_EVENT_SOURCE_KEYS = frozenset({"com.mindroom.dispatch_pipeline_timing"})


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
