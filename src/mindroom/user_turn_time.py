"""Model-facing timestamp rendering for user-authored turns."""

from __future__ import annotations

from mindroom.memory import strip_user_turn_time_prefix
from mindroom.timestamp_formatting import format_timestamp_ms


def prefix_user_turn_time(
    prompt: str,
    *,
    timezone: str,
    timestamp_ms: float | None = None,
) -> str:
    """Prefix one user-authored turn with local date and time."""
    if timestamp_ms is None or not prompt.strip() or strip_user_turn_time_prefix(prompt) != prompt:
        return prompt
    formatted_time = format_timestamp_ms(timestamp_ms, timezone=timezone)
    if formatted_time is None:
        return prompt
    return f"[{formatted_time}] {prompt}"
