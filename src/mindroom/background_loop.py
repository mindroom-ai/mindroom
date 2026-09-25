"""Wakeable loops shared by the durable per-session background workers."""

from __future__ import annotations

import asyncio
import threading
from contextlib import contextmanager, suppress
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator


class WakeSignal:
    """Wake every loop listening in this process, from the event loop or any worker thread."""

    def __init__(self) -> None:
        self._listeners: set[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = set()
        self._lock = threading.Lock()

    def notify(self) -> None:
        """Wake all listeners; each wake is delivered on its own loop."""
        with self._lock:
            listeners = tuple(self._listeners)
        for loop, event in listeners:
            loop.call_soon_threadsafe(event.set)

    @contextmanager
    def listen(self, event: asyncio.Event) -> Iterator[None]:
        """Deliver notifications to ``event`` on the running loop while the block runs."""
        listener = (asyncio.get_running_loop(), event)
        with self._lock:
            self._listeners.add(listener)
        try:
            yield
        finally:
            with self._lock:
                self._listeners.discard(listener)


async def run_until_stopped(
    *,
    stop: asyncio.Event,
    wake: asyncio.Event,
    signal: WakeSignal,
    cycle: Callable[[], Awaitable[float]],
) -> None:
    """Run ``cycle`` until ``stop`` is set, then wait its returned seconds unless woken sooner.

    The wake is cleared before each cycle, so work queued or a stop requested during a cycle is never lost.
    """
    with signal.listen(wake):
        while not stop.is_set():
            wake.clear()
            interval = await cycle()
            with suppress(TimeoutError):
                await asyncio.wait_for(wake.wait(), timeout=interval)
