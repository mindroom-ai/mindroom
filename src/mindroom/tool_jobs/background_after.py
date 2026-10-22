"""The reserved `background_after` argument of managed calls, kept apart from the application's own arguments."""

from __future__ import annotations

import sys
from typing import Any, Literal

type ToolWaitMode = Literal["native", "inline", "managed"]

# Seconds a managed call may hold its reply before it continues in the background.
BACKGROUND_AFTER = "background_after"
# The optional parameter each managed function's definition gains, so the model can choose that budget.
BACKGROUND_AFTER_SCHEMA = {
    "anyOf": [{"type": "number", "minimum": 0}, {"type": "null"}],
    "description": (
        "Seconds to wait for this call before it continues in the background, without cancelling it. "
        "Omit or pass null to wait until completion or a newer message you answer; "
        "zero continues in the background at once and returns a job handle."
    ),
}


def validate_wait_seconds(value: object, *, argument: str) -> float | None:
    """Return a finite clock-representable number of seconds to wait, or None to wait without a limit."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= sys.float_info.max:
        msg = f"{argument} must be null or a finite nonnegative number of seconds"
        raise ValueError(msg)
    return float(value)


def read_background_after(arguments: dict[str, Any] | None, *, owned_execution: bool) -> float | None:
    """Validate a call's `background_after` before accepting execution side effects."""
    value = validate_wait_seconds((arguments or {}).get(BACKGROUND_AFTER), argument=BACKGROUND_AFTER)
    if value is not None and owned_execution:
        msg = f"{BACKGROUND_AFTER} cannot detach nested execution from its outer job; omit it or pass null"
        raise ValueError(msg)
    return value


def application_arguments(arguments: dict[str, Any] | None) -> dict[str, Any] | None:
    """Remove reserved metadata without mutating exact persisted call evidence."""
    if arguments is None:
        return None
    return {key: value for key, value in arguments.items() if key != BACKGROUND_AFTER}
