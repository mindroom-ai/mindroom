"""Shared state for synchronous tool bridges that block an event loop."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import TYPE_CHECKING
from weakref import WeakSet

if TYPE_CHECKING:
    import asyncio
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
