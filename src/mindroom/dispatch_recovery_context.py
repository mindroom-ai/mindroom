"""Request-local state for durable callback and turn recovery."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

_DISPATCH_RECOVERY_ACTIVE: ContextVar[bool] = ContextVar(
    "dispatch_recovery_active",
    default=False,
)
_TURN_DISPATCH_RECOVERY_ACTIVE: ContextVar[bool] = ContextVar(
    "turn_dispatch_recovery_active",
    default=False,
)


def dispatch_recovery_active() -> bool:
    """Return whether the current callback is replaying a durable obligation."""
    return _DISPATCH_RECOVERY_ACTIVE.get()


def turn_dispatch_recovery_active() -> bool:
    """Return whether the current callback is replaying durable turn work."""
    return _TURN_DISPATCH_RECOVERY_ACTIVE.get()


@contextmanager
def turn_dispatch_recovery_scope(*, active: bool) -> Iterator[None]:
    """Bind durable turn recovery state to the current async context."""
    token = _TURN_DISPATCH_RECOVERY_ACTIVE.set(active)
    try:
        yield
    finally:
        _TURN_DISPATCH_RECOVERY_ACTIVE.reset(token)


@contextmanager
def dispatch_recovery_scope(*, turn_backed: bool = False) -> Iterator[None]:
    """Bind durable callback and optional turn recovery to the current context."""
    token = _DISPATCH_RECOVERY_ACTIVE.set(True)
    try:
        with turn_dispatch_recovery_scope(active=turn_backed):
            yield
    finally:
        _DISPATCH_RECOVERY_ACTIVE.reset(token)
