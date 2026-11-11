"""Newer human messages that release a reply's waits, the reply's running tool calls, and cancellation checkpoints."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator


class _PendingHumanMessages(Protocol):
    """Newer human messages waiting for the running reply of their conversation."""

    async def wait(self) -> None:
        """Return once a waiting human message needs the reply's attention, at once if one already does."""
        ...


@dataclass
class ReplyToolCalls:
    """The tool calls one reply runs now, and how many of them do nothing but wait on background jobs."""

    # Called when a call ends or starts waiting, so the reply's waits look again at the messages waiting for it.
    on_change: Callable[[], None]
    running: int = 0
    waiting: int = 0

    def only_waiting(self) -> bool:
        """Return whether every running call waits on jobs, and one at least does: the reply does no work now."""
        return 0 < self.waiting == self.running


@dataclass
class JobControl:
    """Prevent future tool entry after explicit cancellation."""

    cancelled: bool = False
    shutdown: bool = False
    # Set by a Stop of the reply that started the job, which then gets no further approval.
    reply_stopped: bool = False

    def cancel(self, *, shutdown: bool = False) -> None:
        """Prevent tool entry even when the operation catches task cancellation; the first cause stands."""
        if not self.cancelled:
            self.cancelled, self.shutdown = True, shutdown

    def checkpoint(self, *, approval: bool = False) -> None:
        """Fail on cancellation, or on an approval step after the reply's Stop, without event-loop waiters."""
        if self.cancelled or (approval and self.reply_stopped):
            raise asyncio.CancelledError


_pending_human: ContextVar[_PendingHumanMessages | None] = ContextVar("job_pending_human_messages", default=None)
_reply_calls: ContextVar[ReplyToolCalls | None] = ContextVar("job_reply_tool_calls", default=None)
_control: ContextVar[JobControl | None] = ContextVar("job_control", default=None)


def current_pending_human_messages() -> _PendingHumanMessages | None:
    """Return the human messages waiting for the reply this task belongs to; background work has none."""
    return _pending_human.get()


@contextmanager
def pending_human_messages_context(
    messages: _PendingHumanMessages | None,
    *,
    calls: ReplyToolCalls | None = None,
) -> Iterator[None]:
    """Bind the waiting human messages and the running tool calls of one reply, or clear both for background work."""
    token = _pending_human.set(messages)
    calls_token = _reply_calls.set(calls)
    try:
        yield
    finally:
        _reply_calls.reset(calls_token)
        _pending_human.reset(token)


@contextmanager
def counted_tool_call() -> Iterator[None]:
    """Count one tool call of the running reply while it runs."""
    calls = _reply_calls.get()
    if calls is None:
        yield
        return
    calls.running += 1
    try:
        yield
    finally:
        calls.running -= 1
        calls.on_change()


@contextmanager
def waiting_on_jobs() -> Iterator[None]:
    """Mark the running call as doing nothing but waiting on background jobs while it waits."""
    calls = _reply_calls.get()
    if calls is None:
        yield
        return
    calls.waiting += 1
    calls.on_change()
    try:
        yield
    finally:
        calls.waiting -= 1


@contextmanager
def job_control_context(control: JobControl) -> Iterator[None]:
    """Carry one owning job's control through nested child and tool execution."""
    token = _control.set(control)
    try:
        yield
    finally:
        _control.reset(token)


def job_checkpoint(*, approval: bool = False) -> None:
    """Enforce the active job's cancellation immediately before tool execution, or its reply's Stop at an approval."""
    control = _control.get()
    if control is not None:
        control.checkpoint(approval=approval)


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
