"""Lightweight human-follow-up signals and child tool cancellation checkpoints."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator


@dataclass
class HumanMessageSignal:
    """Wake foreground waits and retain live conversation subscriptions."""

    _subscribers: set[Callable[[], None]] = field(default_factory=set)
    _pending: bool = False

    @property
    def has_subscribers(self) -> bool:
        """Return whether background work still depends on this conversation."""
        return bool(self._subscribers)

    def subscribe(self, callback: Callable[[], None]) -> None:
        """Subscribe one owned job to subsequent admitted human messages."""
        self._subscribers.add(callback)
        if self._pending:
            callback()

    def unsubscribe(self, callback: Callable[[], None]) -> None:
        """Release a terminal job's subscription."""
        self._subscribers.discard(callback)

    def notify(self) -> None:
        """Release subscribed waits without changing the execution of their jobs."""
        self._pending = True
        for callback in tuple(self._subscribers):
            callback()

    def clear(self) -> None:
        """Consume queued input so later waits can remain attached."""
        self._pending = False


@dataclass
class JobControl:
    """Prevent future tool entry after explicit cancellation."""

    cancelled: bool = False
    shutdown: bool = False

    def cancel(self, *, shutdown: bool = False) -> None:
        """Prevent tool entry even when the operation catches task cancellation."""
        self.cancelled = True
        self.shutdown = self.shutdown or shutdown

    def checkpoint(self) -> None:
        """Fail on cancellation without creating waiters on another event loop."""
        if self.cancelled:
            raise asyncio.CancelledError


_human_signal: ContextVar[HumanMessageSignal | None] = ContextVar("job_human_signal", default=None)
_control: ContextVar[JobControl | None] = ContextVar("job_control", default=None)


def current_human_message_signal() -> HumanMessageSignal | None:
    """Return the live canonical conversation signal, without notice persistence state."""
    return _human_signal.get()


@contextmanager
def human_message_signal_context(signal: HumanMessageSignal | None) -> Iterator[None]:
    """Bind the human signal for jobs launched by one response lifecycle."""
    token = _human_signal.set(signal)
    try:
        yield
    finally:
        _human_signal.reset(token)


@contextmanager
def job_control_context(control: JobControl) -> Iterator[None]:
    """Carry one owning job's control through nested child and tool execution."""
    token = _control.set(control)
    try:
        yield
    finally:
        _control.reset(token)


def job_checkpoint() -> None:
    """Enforce the active job's cancellation immediately before tool execution."""
    control = _control.get()
    if control is not None:
        control.checkpoint()


def job_owns_execution() -> bool:
    """Return whether this task already belongs to one managed operation."""
    return _control.get() is not None


def job_stopped_by_shutdown() -> bool:
    """Return whether runtime shutdown, not a cancellation request, stopped the active job."""
    control = _control.get()
    return control is not None and control.shutdown
