"""Cancellation-safe execution for owned blocking operations."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from collections.abc import Callable

_Result = TypeVar("_Result")


async def run_owned_blocking_operation(
    operation: Callable[..., _Result],
    *args: object,
    **kwargs: object,
) -> _Result:
    """Drain one off-loop operation before propagating caller cancellation."""
    worker_task = asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
    try:
        return await asyncio.shield(worker_task)
    except asyncio.CancelledError as cancellation:
        worker_error: Exception | None = None
        while not worker_task.done():
            try:
                await asyncio.shield(worker_task)
            except asyncio.CancelledError:
                continue
            except Exception as exc:
                worker_error = exc
                break
        if worker_error is None:
            try:
                worker_task.result()
            except Exception as exc:
                worker_error = exc
        if worker_error is not None:
            raise cancellation from worker_error
        raise
