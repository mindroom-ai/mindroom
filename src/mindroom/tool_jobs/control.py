"""Newer human messages that release a reply's waits, and child tool cancellation checkpoints."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Iterator


class _PendingHumanMessages(Protocol):
    """Newer human messages waiting for the running reply of their conversation."""

    async def wait(self) -> None:
        """Return once a waiting human message needs the reply's attention, at once if one already does."""
        ...


class StoppedBeforeStart(asyncio.CancelledError):
    """A reply's Stop refused an approved action before it started, so nothing ran."""


@dataclass
class JobControl:
    """Prevent future tool entry after explicit cancellation."""

    cancelled: bool = False
    shutdown: bool = False
    # A reply's Stop refuses later approval actions while leaving already running calls alone.
    reply_stopped: bool = False

    def cancel(self, *, shutdown: bool = False) -> None:
        """Prevent tool entry even when the operation catches task cancellation; the first cause stands."""
        if not self.cancelled:
            self.cancelled, self.shutdown = True, shutdown

    def checkpoint(self, *, approval: bool = False) -> None:
        """Fail on cancellation, or a stopped reply's approved action, without event-loop waiters."""
        if self.cancelled:
            raise asyncio.CancelledError
        if approval and self.reply_stopped:
            raise StoppedBeforeStart


_pending_human: ContextVar[_PendingHumanMessages | None] = ContextVar("job_pending_human_messages", default=None)
_control: ContextVar[JobControl | None] = ContextVar("job_control", default=None)


@dataclass
class _CallApproval:
    """Whether the tool call whose entrypoint runs was approved, and whether a Stop refused it before its effects."""

    approved: bool
    refused: bool = False


_call_approval: ContextVar[_CallApproval | None] = ContextVar("job_call_approval", default=None)


def current_pending_human_messages() -> _PendingHumanMessages | None:
    """Return the human messages waiting for the reply this task belongs to; background work has none."""
    return _pending_human.get()


@contextmanager
def pending_human_messages_context(messages: _PendingHumanMessages | None) -> Iterator[None]:
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


@contextmanager
def approved_call(approved: bool) -> Iterator[None]:
    """Carry whether a tool call was approved through its entrypoint, to the checks right before its effects."""
    call = _CallApproval(approved)
    token = _call_approval.set(call)
    try:
        yield
    except asyncio.CancelledError:
        # Task and shield boundaries turn a refusal into plain cancellation; the call still says it never ran.
        if call.refused:
            raise StoppedBeforeStart from None
        raise
    finally:
        _call_approval.reset(token)


def job_checkpoint(*, approval: bool | None = None) -> None:
    """Check right before an action's effect: refuse cancelled work, and a stopped reply's approved action.

    The action defaults to the tool call whose entrypoint runs; outside an entrypoint no call is approved.
    """
    control = _control.get()
    call = _call_approval.get() if approval is None else None
    if control is None:
        return
    if call is not None:
        approval = call.approved
        call.refused = approval and control.reply_stopped
    control.checkpoint(approval=bool(approval))


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
