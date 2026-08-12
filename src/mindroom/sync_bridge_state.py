"""Shared state for synchronous tool bridges that block an event loop."""

from __future__ import annotations

import asyncio
import threading
from contextlib import contextmanager
from typing import TYPE_CHECKING
from weakref import WeakSet

if TYPE_CHECKING:
    from collections.abc import Iterator

_SYNC_BRIDGE_BLOCKED_LOOPS: WeakSet[object] = WeakSet()
_SYNC_BRIDGE_BLOCKED_LOOPS_LOCK = threading.Lock()


@contextmanager
def sync_tool_bridge_blocked_loop(loop: asyncio.AbstractEventLoop) -> Iterator[None]:
    """Mark one event loop as blocked by synchronous tool execution."""
    with _SYNC_BRIDGE_BLOCKED_LOOPS_LOCK:
        _SYNC_BRIDGE_BLOCKED_LOOPS.add(loop)
    try:
        yield
    finally:
        with _SYNC_BRIDGE_BLOCKED_LOOPS_LOCK:
            _SYNC_BRIDGE_BLOCKED_LOOPS.discard(loop)


def is_loop_blocked_by_sync_tool_bridge(loop: asyncio.AbstractEventLoop) -> bool:
    """Return whether synchronous tool execution is currently blocking one event loop."""
    with _SYNC_BRIDGE_BLOCKED_LOOPS_LOCK:
        return loop in _SYNC_BRIDGE_BLOCKED_LOOPS


def running_loop_is_sync_tool_bridge() -> bool:
    """Return whether the caller is running on a bridge loop, not the runtime loop.

    A synchronous tool bridge runs its async work on a loop of its own while
    the runtime loop that spawned it is blocked waiting for the thread. Both
    facts are needed to recognise the situation from inside that work: the
    caller's own loop is not the marked one, and some loop is marked. Asking
    only whether a loop is blocked would also be true when read from the
    blocked loop itself, which is never a bridge.
    """
    try:
        current = asyncio.get_running_loop()
    except RuntimeError:
        return False
    with _SYNC_BRIDGE_BLOCKED_LOOPS_LOCK:
        return any(loop is not current for loop in _SYNC_BRIDGE_BLOCKED_LOOPS)
