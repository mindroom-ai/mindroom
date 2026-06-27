"""Model-facing timestamp rendering for user-authored turns."""

from __future__ import annotations

from datetime import datetime
from math import isfinite
from zoneinfo import ZoneInfo

from mindroom.memory import strip_user_turn_time_prefix


def prefix_user_turn_time(
    prompt: str,
    *,
    timezone: str,
    timestamp_ms: float | None = None,
) -> str:
    """Prefix one user-authored turn with local date and time."""
    if timestamp_ms is None or not prompt.strip() or strip_user_turn_time_prefix(prompt) != prompt:
        return prompt
    try:
        timestamp_seconds = timestamp_ms / 1000
    except OverflowError:
        return prompt
    if not isfinite(timestamp_seconds):
        return prompt
    tz = ZoneInfo(timezone)
    try:
        current = datetime.fromtimestamp(timestamp_seconds, tz)
    except (OSError, OverflowError, ValueError):
        return prompt
    timezone_abbrev = current.tzname() or timezone
    return f"[{current.strftime('%Y-%m-%d %H:%M')} {timezone_abbrev}] {prompt}"
