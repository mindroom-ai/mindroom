"""Shared timestamp rendering for model-facing prompt metadata."""

from __future__ import annotations

from datetime import datetime
from math import isfinite
from zoneinfo import ZoneInfo


def format_timestamp_ms(
    timestamp_ms: float | None,
    *,
    timezone: str,
) -> str | None:
    """Return one millisecond timestamp rendered in the configured local time."""
    if timestamp_ms is None:
        return None
    try:
        timestamp_seconds = timestamp_ms / 1000
    except OverflowError:
        return None
    if not isfinite(timestamp_seconds):
        return None
    tz = ZoneInfo(timezone)
    try:
        current = datetime.fromtimestamp(timestamp_seconds, tz)
    except (OSError, OverflowError, ValueError):
        return None
    timezone_abbrev = current.tzname() or timezone
    return f"{current.strftime('%Y-%m-%d %H:%M')} {timezone_abbrev}"
