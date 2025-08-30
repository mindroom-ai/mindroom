"""Background task management for non-blocking operations."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from .logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

logger = get_logger(__name__)

# Global set to track background tasks and prevent them from being garbage collected
_background_tasks: set[asyncio.Task[Any]] = set()


def create_background_task(
    coro: Coroutine[Any, Any, Any],
    name: str | None = None,
    error_handler: Callable[[Exception], None] | None = None,
) -> asyncio.Task[Any]:
    """Create a background task that won't block the main execution.

    Args:
        coro: The coroutine to run in the background
        name: Optional name for the task (for logging)
        error_handler: Optional error handler function

    Returns:
        The created task

    """
    task: asyncio.Task[Any] = asyncio.create_task(coro)
    if name:
        task.set_name(name)

    # Add to global set to prevent garbage collection
    _background_tasks.add(task)

    # Add completion callback to remove from set and handle errors
    def _task_done_callback(task: asyncio.Task[Any]) -> None:
        _background_tasks.discard(task)
        try:
            # This will raise if the task had an exception
            task.result()
        except asyncio.CancelledError:
            # Task was cancelled, this is fine
            pass
        except Exception as e:
            task_name = task.get_name() if hasattr(task, "get_name") else "unknown"
            logger.exception("Background task failed", task_name=task_name, error=str(e))
            if error_handler:
                try:
                    error_handler(e)
                except Exception as handler_error:
                    logger.exception("Error handler for task failed", task_name=task_name, error=str(handler_error))

    task.add_done_callback(_task_done_callback)
    return task


async def wait_for_background_tasks(timeout: float | None = None) -> None:  # noqa: ASYNC109
    """Wait for all background tasks to complete.

    Args:
        timeout: Optional timeout in seconds

    """
    if not _background_tasks:
        return

    try:
        await asyncio.wait_for(asyncio.gather(*_background_tasks, return_exceptions=True), timeout=timeout)
    except TimeoutError:
        logger.warning(f"Background tasks did not complete within {timeout} seconds")
        # Cancel remaining tasks
        for task in _background_tasks:
            task.cancel()
        # Wait for cancellation to complete
        await asyncio.gather(*_background_tasks, return_exceptions=True)


def get_background_task_count() -> int:
    """Get the number of currently running background tasks."""
    return len(_background_tasks)
