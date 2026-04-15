"""Background task management for non-blocking operations."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

logger = get_logger(__name__)
_MAX_BACKGROUND_TASK_CANCEL_ROUNDS = 3

# Global registries keep strong references to background tasks and optional owners.
_background_tasks: set[asyncio.Task[Any]] = set()
_background_task_owners: dict[asyncio.Task[Any], object] = {}


def create_background_task(
    coro: Coroutine[Any, Any, Any],
    name: str | None = None,
    error_handler: Callable[[Exception], None] | None = None,
    *,
    owner: object | None = None,
    log_exceptions: bool = True,
) -> asyncio.Task[Any]:
    """Create a background task that won't block the main execution.

    Args:
        coro: The coroutine to run in the background
        name: Optional name for the task (for logging)
        error_handler: Optional error handler function
        owner: Optional logical owner used for scoped shutdown waits
        log_exceptions: Whether unhandled task exceptions should be logged automatically

    Returns:
        The created task

    """
    task: asyncio.Task[Any] = asyncio.create_task(coro)
    if name:
        task.set_name(name)

    # Add to global set to prevent garbage collection
    _background_tasks.add(task)
    if owner is not None:
        _background_task_owners[task] = owner

    # Add completion callback to remove from set and handle errors
    def _task_done_callback(task: asyncio.Task[Any]) -> None:
        _background_tasks.discard(task)
        _background_task_owners.pop(task, None)
        try:
            # This will raise if the task had an exception
            task.result()
        except asyncio.CancelledError:
            # Task was cancelled, this is fine
            pass
        except Exception as e:
            task_name = task.get_name()
            if log_exceptions:
                logger.exception("Background task failed", task_name=task_name, error=str(e))
            if error_handler:
                try:
                    error_handler(e)
                except Exception as handler_error:
                    logger.exception("Error handler for task failed", task_name=task_name, error=str(handler_error))

    task.add_done_callback(_task_done_callback)
    return task


def _tasks_for_owner(owner: object | None) -> tuple[asyncio.Task[Any], ...]:
    if owner is None:
        return tuple(_background_tasks)
    return tuple(task for task in _background_tasks if _background_task_owners.get(task) is owner)


async def _cancel_and_drain_background_tasks(
    tasks: tuple[asyncio.Task[Any], ...],
    *,
    owner: object | None,
) -> None:
    pending_tasks = tasks
    for _ in range(_MAX_BACKGROUND_TASK_CANCEL_ROUNDS):
        if not pending_tasks:
            return
        for task in pending_tasks:
            task.cancel()
        await asyncio.gather(*pending_tasks, return_exceptions=True)
        pending_tasks = _tasks_for_owner(owner)
    if pending_tasks:
        logger.warning(
            "Background tasks still running after bounded cancellation drain",
            task_count=len(pending_tasks),
            cancel_rounds=_MAX_BACKGROUND_TASK_CANCEL_ROUNDS,
        )


async def wait_for_background_tasks(
    timeout: float | None = None,  # noqa: ASYNC109
    *,
    owner: object | None = None,
) -> None:
    """Wait for all background tasks to complete.

    Args:
        timeout: Optional timeout in seconds
        owner: Optional logical owner to scope the wait to one bot

    """
    deadline: float | None = None
    if timeout is not None:
        deadline = asyncio.get_running_loop().time() + timeout

    while True:
        tasks = _tasks_for_owner(owner)
        if not tasks:
            return

        remaining: float | None = None
        if deadline is not None:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                logger.warning("background_tasks_wait_timeout", timeout_seconds=timeout)
                await _cancel_and_drain_background_tasks(tasks, owner=owner)
                return

        done, pending = await asyncio.wait(tasks, timeout=remaining)
        await asyncio.gather(*done, return_exceptions=True)
        if pending:
            logger.warning("background_tasks_wait_timeout", timeout_seconds=timeout)
            await _cancel_and_drain_background_tasks(tuple(pending), owner=owner)
            return


def _get_background_task_count() -> int:
    """Get the number of currently running background tasks."""
    return len(_background_tasks)
