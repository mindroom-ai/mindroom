"""Database work handed to worker threads, and the promise to wait for it.

A cancelled ``await`` cannot stop a thread that is already running. Awaiting
``asyncio.to_thread`` bare therefore returns while the statement is still on the
connection, and whatever the await was holding is released a moment too early:
the writer lock goes to the next caller, the pooled reader is handed back, or
``close()`` closes the connection out from under a live statement. SQLite pays
for that last one with a segmentation fault; PostgreSQL pays for the first two
by running the next transaction inside a stranger's open one.

The fix is the same in both backends and belongs in neither of them: an
offloaded statement is awaited to completion even when the await itself is
cancelled, so ownership is released only once the thread has genuinely stopped.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable


async def settled[T](work: asyncio.Future[T]) -> T:
    """Await ``work``, never returning while it is still running.

    Cancelling this await does not cancel ``work`` and does not shorten it. The
    cancellation is remembered, the wait is re-entered, and only once the thread
    has stopped does the cancellation propagate -- which is the whole point,
    because the caller's next act is to release something ``work`` is using.

    Re-entering rather than escaping matters for a caller cancelled twice: the
    second cancellation would otherwise hand the connection on while the first
    statement is still executing on it.
    """
    cancellation: asyncio.CancelledError | None = None
    while not work.done():
        try:
            # Waiting rather than awaiting ``work`` directly: this must not
            # cancel it, and must not unwind on its failure before the loop has
            # seen that it finished. The only thing that can be raised here is
            # this await's own cancellation.
            await asyncio.wait({work})
        except asyncio.CancelledError as error:
            cancellation = error
    if cancellation is not None:
        if not work.cancelled() and (worker_error := work.exception()) is not None:
            raise cancellation from worker_error
        raise cancellation
    return work.result()


@dataclass(slots=True)
class ThreadOffload:
    """Every statement this hands to a worker thread, it can also wait for."""

    _running: set[asyncio.Future[Any]] = field(default_factory=set)

    def submit[T](self, call: Callable[[], T]) -> asyncio.Future[T]:
        """Hand ``call`` to a worker thread, remembering it until it stops.

        Registration is synchronous, so a ``close()`` that starts after this
        returns already knows the statement exists.
        """
        work = asyncio.ensure_future(asyncio.to_thread(call))
        self._running.add(work)
        work.add_done_callback(self._running.discard)
        return work

    async def run[T](self, call: Callable[[], T]) -> T:
        """Run ``call`` on a worker thread and outlive this await's cancellation."""
        return await settled(self.submit(call))

    async def drain(self) -> None:
        """Wait until no statement of this backend is still on a worker thread."""
        while running := {work for work in self._running if not work.done()}:
            await asyncio.wait(running)
