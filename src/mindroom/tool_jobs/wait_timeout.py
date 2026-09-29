"""Reserved framework waiting metadata, separate from application arguments."""

from __future__ import annotations

import sys
from typing import Any, Literal

type ToolWaitMode = Literal["native", "inline", "managed"]


def validate_wait_timeout(value: object) -> float | None:
    """Return a finite clock-representable wait budget, or an unbounded wait."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= sys.float_info.max:
        msg = "wait_timeout must be null or a finite nonnegative number of seconds"
        raise ValueError(msg)
    return float(value)


def read_wait_timeout(arguments: dict[str, Any] | None, *, owned_execution: bool = False) -> float | None:
    """Validate a caller's wait budget before accepting execution side effects."""
    value = validate_wait_timeout((arguments or {}).get("wait_timeout"))
    if value is None:
        return None
    if owned_execution:
        msg = "wait_timeout cannot detach nested execution from its outer job; omit it or pass null"
        raise ValueError(msg)
    return value


def application_arguments(arguments: dict[str, Any] | None) -> dict[str, Any] | None:
    """Remove reserved metadata without mutating exact persisted call evidence."""
    if arguments is None:
        return None
    return {key: value for key, value in arguments.items() if key != "wait_timeout"}
