"""The reserved `background_after` argument of managed calls, kept apart from the application's own arguments."""

from __future__ import annotations

import sys
from typing import Any, Literal

type ToolWaitMode = Literal["native", "inline", "managed"]

# Seconds a managed call may hold its reply before it continues in the background.
BACKGROUND_AFTER = "background_after"


def background_after_schema(default_wait: float | None) -> dict[str, Any]:
    """Return the optional parameter a managed function's definition gains, so the model can choose that budget.

    ``default_wait`` is how many seconds a call that omits the budget or passes null waits, None for no limit.
    """
    waits = "until completion or a newer message you answer"
    if default_wait is not None:
        waits = f"until completion, a newer message you answer, or {default_wait:g} seconds"
    return {
        "anyOf": [{"type": "number", "minimum": 0}, {"type": "null"}],
        "description": (
            "Seconds to wait for this call before it continues in the background, without cancelling it. "
            f"Omit or pass null to wait {waits}; "
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


def application_arguments(arguments: dict[str, Any] | None) -> dict[str, Any]:
    """Remove reserved metadata without mutating exact persisted call evidence."""
    return {key: value for key, value in (arguments or {}).items() if key != BACKGROUND_AFTER}
