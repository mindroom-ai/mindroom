"""Newer human messages that release a reply's waits, and child tool cancellation checkpoints."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Iterator


class PendingHumanMessages(Protocol):
    """Newer human messages waiting for the running reply of their conversation; set while any waits."""

    def is_set(self) -> bool:
        """Return whether a human message waits now."""
        ...

    async def wait(self) -> None:
        """Return once a human message waits."""
        ...


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


_pending_human: ContextVar[PendingHumanMessages | None] = ContextVar("job_pending_human_messages", default=None)
_control: ContextVar[JobControl | None] = ContextVar("job_control", default=None)


def current_pending_human_messages() -> PendingHumanMessages | None:
    """Return the human messages waiting for the reply this task belongs to; background work has none."""
    return _pending_human.get()


@contextmanager
def pending_human_messages_context(messages: PendingHumanMessages | None) -> Iterator[None]:
    """Bind the waiting human messages of one response lifecycle, or clear them for background work."""
    token = _pending_human.set(messages)
    try:
        yield
    finally:
        _pending_human.reset(token)


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
