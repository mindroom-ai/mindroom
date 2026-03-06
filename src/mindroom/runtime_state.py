"""Shared runtime readiness state for the MindRoom process."""

from __future__ import annotations

from dataclasses import dataclass
from threading import Lock


@dataclass(slots=True)
class RuntimeState:
    """Thread-safe snapshot of the current MindRoom runtime phase."""

    phase: str = "idle"
    detail: str | None = None


_state = RuntimeState()
_lock = Lock()


def get_runtime_state() -> RuntimeState:
    """Return a copy of the current runtime state."""
    with _lock:
        return RuntimeState(phase=_state.phase, detail=_state.detail)


def set_runtime_starting(detail: str = "MindRoom startup in progress") -> None:
    """Mark the runtime as starting."""
    with _lock:
        _state.phase = "starting"
        _state.detail = detail


def set_runtime_ready() -> None:
    """Mark the runtime as ready to serve requests."""
    with _lock:
        _state.phase = "ready"
        _state.detail = None


def set_runtime_failed(detail: str) -> None:
    """Mark the runtime as failed during startup or execution."""
    with _lock:
        _state.phase = "failed"
        _state.detail = detail


def reset_runtime_state() -> None:
    """Reset the runtime state after shutdown."""
    with _lock:
        _state.phase = "idle"
        _state.detail = None
