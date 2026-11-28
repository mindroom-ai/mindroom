"""Async support shared by the graph store, its clients, and ingestion."""

from __future__ import annotations

import asyncio
from contextvars import copy_context
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Coroutine
    from concurrent.futures import Future as ConcurrentFuture


class UnusableExtractionError(ValueError):
    """The extraction model answered with output Graphiti cannot use; repeated ones drop the episode."""


async def run_on_loop[T](loop: asyncio.AbstractEventLoop, coroutine: Coroutine[Any, Any, T]) -> T:
    """Await ``coroutine`` on ``loop`` with the caller's context variables; cancelling the caller cancels it."""
    if loop is asyncio.get_running_loop():
        return await coroutine
    future = cast("ConcurrentFuture[T]", copy_context().run(asyncio.run_coroutine_threadsafe, coroutine, loop))
    try:
        return await asyncio.wrap_future(future)
    except asyncio.CancelledError:
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            raise
        # The work was cancelled on its own loop, for example by closing the store, not by this caller; a caller
        # such as an agent's tool call must see an ordinary failure rather than its own turn being cancelled.
        msg = "The work was cancelled on the other event loop"
        raise RuntimeError(msg) from None


__all__ = ["UnusableExtractionError", "run_on_loop"]
