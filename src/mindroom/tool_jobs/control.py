"""Lightweight human-follow-up signals and cooperative child tool checkpoints."""

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
    """Keep background subscribers alive beyond their parent's response turn."""

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
        """Synchronously prevent subscribers from starting another tool."""
        self._pending = True
        for callback in tuple(self._subscribers):
            callback()

    def clear(self) -> None:
        """Consume queued intent without resuming already-paused subscribers."""
        self._pending = False


@dataclass
class JobControl:
    """Pause only future tool entry; leave already-running external work alone."""

    paused: asyncio.Event = field(default_factory=asyncio.Event)
    resumed: asyncio.Event = field(default_factory=asyncio.Event)
    cancelled: bool = False
    blocked: asyncio.Event = field(default_factory=asyncio.Event)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    _owner_loop: asyncio.AbstractEventLoop = field(default_factory=asyncio.get_running_loop, repr=False)

    def pause(self) -> None:
        """Latch human intent until explicit resumption."""
        self.resumed.clear()
        self.paused.set()
        self.changed.set()

    def resume(self) -> None:
        """Allow future tools without modifying native approval decisions."""
        self.paused.clear()
        self.resumed.set()
        self.changed.set()

    def cancel(self) -> None:
        """Prevent future tool entry even when a cancelled operation catches cancellation."""
        self.cancelled = True
        self.resumed.set()
        self.changed.set()

    async def checkpoint(self) -> None:
        """Wait for explicit resume or fail on cancellation before entering a tool."""
        if asyncio.get_running_loop() is not self._owner_loop:
            waiting = asyncio.run_coroutine_threadsafe(self._checkpoint_on_owner(), self._owner_loop)
            await asyncio.wrap_future(waiting)
            return
        await self._checkpoint_on_owner()

    async def _checkpoint_on_owner(self) -> None:
        try:
            while self.paused.is_set() and not self.cancelled:
                self.blocked.set()
                self.changed.set()
                await self.resumed.wait()
        finally:
            self.blocked.clear()
            self.changed.set()
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


async def job_checkpoint() -> None:
    """Enforce the active job's human hold immediately before tool execution."""
    control = _control.get()
    if control is not None:
        await control.checkpoint()


def job_owns_execution() -> bool:
    """Return whether this task already belongs to one managed operation."""
    return _control.get() is not None
