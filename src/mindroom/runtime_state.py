"""Shared runtime readiness state for the MindRoom process."""

from __future__ import annotations

from dataclasses import dataclass
from threading import Lock

_LOOPBACK_BIND_HOSTS = ("0.0.0.0", "::")  # noqa: S104


@dataclass(frozen=True, slots=True)
class _ApiServerAddress:
    """Bind address of the embedded dashboard/API server."""

    host: str
    port: int

    @property
    def base_url(self) -> str:
        """Return the local-client base URL for this bind address."""
        host = "127.0.0.1" if self.host in _LOOPBACK_BIND_HOSTS else self.host
        return f"http://{host}:{self.port}"


@dataclass(slots=True)
class _RuntimeState:
    """Thread-safe snapshot of the current MindRoom runtime phase."""

    phase: str = "idle"
    detail: str | None = None
    api_server_address: _ApiServerAddress | None = None


_state = _RuntimeState()
_lock = Lock()


def get_runtime_state() -> _RuntimeState:
    """Return a copy of the current runtime state."""
    with _lock:
        return _RuntimeState(
            phase=_state.phase,
            detail=_state.detail,
            api_server_address=_state.api_server_address,
        )


def set_api_server_address(host: str, port: int) -> None:
    """Record the bind address of the embedded API server."""
    with _lock:
        _state.api_server_address = _ApiServerAddress(host=host, port=port)


def get_api_server_address() -> _ApiServerAddress | None:
    """Return the embedded API server bind address, if one was started."""
    with _lock:
        return _state.api_server_address


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
        _state.api_server_address = None
