"""Reserved framework waiting metadata, separate from application arguments."""

from __future__ import annotations

import sys
from copy import deepcopy
from typing import Any, Literal, cast

type ToolWaitMode = Literal["native", "inline", "managed"]
_WAIT_MODES_KEY = "mindroom_tool_wait_modes"


def bind_tool_wait_modes(
    stored_metadata: dict[str, Any],
    active_metadata: dict[str, Any],
    run_id: str,
) -> None:
    """Restore this run's captured modes and share later captures with its saved output."""
    state = stored_metadata.get(_WAIT_MODES_KEY, {})
    state = deepcopy(state) if state.get("run_id") == run_id else {"run_id": run_id, "calls": {}}
    stored_metadata[_WAIT_MODES_KEY] = active_metadata[_WAIT_MODES_KEY] = state


def saved_tool_wait_mode(
    metadata: dict[str, Any] | None,
    run_id: str,
    call_id: str,
) -> ToolWaitMode | None:
    """Read only the exact accepted call, never a prior run's inherited metadata."""
    state = (metadata or {}).get(_WAIT_MODES_KEY, {})
    return cast("ToolWaitMode | None", state.get("calls", {}).get(call_id)) if state.get("run_id") == run_id else None


def record_tool_wait_mode(
    metadata: dict[str, Any],
    run_id: str,
    call_id: str,
    mode: ToolWaitMode,
) -> None:
    """Retain argument semantics in the SDK run's existing approval snapshot."""
    state: dict[str, Any] = metadata.get(_WAIT_MODES_KEY, {})
    if state.get("run_id") != run_id:
        state = metadata[_WAIT_MODES_KEY] = {"run_id": run_id, "calls": {}}
    state["calls"].setdefault(call_id, mode)


def run_uses_managed_waits(metadata: dict[str, Any] | None, run_id: str | None) -> bool:
    """Classify captured execution ownership without interpreting application arguments."""
    state = (metadata or {}).get(_WAIT_MODES_KEY, {})
    return state.get("run_id") == run_id and "managed" in state.get("calls", {}).values()


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
