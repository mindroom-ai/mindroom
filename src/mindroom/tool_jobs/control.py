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
    """Release a reply's waits inside its model run while newer messages or turns wait for its conversation.

    Releasing such a wait never stops the work: the reply finishes its run, and its message holds what is still
    outstanding while the conversation's other turns run.
    """

    _subscribers: set[Callable[[], None]] = field(default_factory=set)
    # Newer messages and turns of the conversation still waiting for this reply.
    _pending: int = 0

    def subscribe(self, callback: Callable[[], None]) -> None:
        """Subscribe one wait, releasing it at once while anything is still pending."""
        self._subscribers.add(callback)
        if self._pending > 0:
            callback()

    def unsubscribe(self, callback: Callable[[], None]) -> None:
        """Release a finished wait's subscription."""
        self._subscribers.discard(callback)

    def notify(self) -> None:
        """A newer message or turn waits: release subscribed waits without changing the execution of their jobs."""
        self._pending += 1
        for callback in tuple(self._subscribers):
            callback()

    def settle(self) -> None:
        """A pending message or turn was handled, so later waits stay attached unless another one is pending."""
        self._pending -= 1


@dataclass
class JobControl:
    """Prevent future tool entry after explicit cancellation."""

    cancelled: bool = False
    shutdown: bool = False

    def cancel(self, *, shutdown: bool = False) -> None:
        """Prevent tool entry even when the operation catches task cancellation; the first cause stands."""
        if not self.cancelled:
            self.cancelled, self.shutdown = True, shutdown

    def checkpoint(self) -> None:
        """Fail on cancellation without creating waiters on another event loop."""
        if self.cancelled:
            raise asyncio.CancelledError


_human_signal: ContextVar[HumanMessageSignal | None] = ContextVar("job_human_signal", default=None)
_control: ContextVar[JobControl | None] = ContextVar("job_control", default=None)


def current_human_message_signal() -> HumanMessageSignal | None:
    """Return the signal of the reply this task belongs to; background work has none."""
    return _human_signal.get()


@contextmanager
def human_message_signal_context(signal: HumanMessageSignal | None) -> Iterator[None]:
    """Bind the human signal of one response lifecycle, or clear it for background work."""
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
    """Return whether a runtime shutdown or restart, not a cancellation request, stopped the active job."""
    control = _control.get()
    if control is None:
        return False
    if control.cancelled:
        return control.shutdown
    # A task cancelled without any request is event-loop teardown, which recovery reports like a restart; an
    # operation that raises cancellation itself was cancelled.
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0
