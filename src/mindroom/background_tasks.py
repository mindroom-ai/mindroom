"""Background task management for non-blocking operations."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from mindroom.cancellation import request_task_cancel
from mindroom.logging_config import get_logger
from mindroom.runtime_shutdown import GENERIC_SHUTDOWN, RuntimeShutdownIntent

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Collection, Coroutine
    from contextvars import Context

logger = get_logger(__name__)
_MAX_BACKGROUND_TASK_CANCEL_ROUNDS = 3
_BACKGROUND_TASK_CANCEL_ROUND_TIMEOUT_SECONDS = 0.1
_LOOP_RESTART_INITIAL_DELAY_SECONDS = 1.0
_LOOP_RESTART_MAX_DELAY_SECONDS = 30.0
# A loop that ran this long before it ended counts as healthy, so its next restart waits the initial delay again.
_LOOP_HEALTHY_RUN_SECONDS = 30.0

# Global registries keep strong references to background tasks and optional owners.
_background_tasks: set[asyncio.Task[Any]] = set()
_background_task_owners: dict[asyncio.Task[Any], object] = {}


async def run_coroutine_until_complete[Result](
    coroutine: Coroutine[Any, Any, Result],
    *,
    on_cancelled: Callable[[], None] | None = None,
) -> Result:
    """Finish one accepted coroutine before propagating cancellation."""
    worker_task = asyncio.create_task(coroutine)
    return await wait_for_future_until_complete(worker_task, on_cancel=on_cancelled)


async def wait_for_future_until_complete[Result](  # noqa: C901
    future: asyncio.Future[Result],
    *,
    on_cancel: Callable[[], None] | None = None,
    chain_cancelled_result: bool = True,
) -> Result:
    """Drain an accepted future before propagating cancellation from its waiter."""
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError as cancellation:
        if on_cancel is not None:
            on_cancel()
        result_error: Exception | asyncio.CancelledError | None = None
        while not future.done():
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError as exc:
                if not future.done() and on_cancel is not None:
                    on_cancel()
                result_error = exc if future.done() else None
            except Exception as exc:
                result_error = exc
            if result_error is not None:
                break
        if result_error is None:
            try:
                future.result()
            except (Exception, asyncio.CancelledError) as exc:
                result_error = exc
        if result_error is not None and (
            chain_cancelled_result or not isinstance(result_error, asyncio.CancelledError)
        ):
            raise cancellation from result_error
        raise


async def run_blocking_until_complete[Result](
    operation: Callable[..., Result],
    *args: object,
) -> Result:
    """Finish one blocking operation before propagating cancellation."""
    return await run_coroutine_until_complete(asyncio.to_thread(operation, *args))


async def run_blocking_or_compensate[Result](
    operation: Callable[[], Result],
    compensate: Callable[[Result], object],
) -> Result:
    """Run one blocking operation off the loop; when cancelled, finish it and compensate for its result.

    The cancelled caller never receives the result, so ``compensate`` undoes what nobody can see, on a thread
    and before the cancellation propagates. An operation that raised needs no compensation.
    """
    running = asyncio.ensure_future(asyncio.to_thread(operation))
    try:
        return await wait_for_future_until_complete(running)
    except asyncio.CancelledError:
        if running.exception() is None:
            await run_blocking_until_complete(compensate, running.result())
        raise


async def drain_tasks(tasks: Collection[asyncio.Future[Any]]) -> None:
    """Wait, without cancelling or raising them, until no task in ``tasks`` runs, including tasks added meanwhile.

    A done callback that discards a finished task from ``tasks`` runs only once this coroutine yields, and gathering
    finished tasks never yields, so whether a task still runs is read from the task, not from ``tasks``.
    """
    while running := [task for task in tasks if not task.done()]:
        await asyncio.gather(*(asyncio.shield(task) for task in running), return_exceptions=True)


def retry_delay_seconds(
    attempt: int,
    *,
    initial_delay_seconds: float,
    max_delay_seconds: float,
) -> float:
    """Return capped exponential backoff delay for a retry attempt.

    Doubling stops at the cap, so an owner that retries for days never overflows.
    """
    delay = initial_delay_seconds
    for _ in range(max(0, attempt - 1)):
        if delay >= max_delay_seconds:
            break
        delay *= 2
    return min(max_delay_seconds, delay)


class SupervisedLoop:
    """Run one long-running loop that restarts with backoff whenever it crashes or returns before ``stop``."""

    def __init__(self, name: str, run: Callable[[], Awaitable[None]]) -> None:
        self.name = name
        self._run = run
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        """Return whether the loop is started and not stopped."""
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Start the loop once; calls while it runs are no-ops."""
        if not self.running:
            self._task = asyncio.create_task(self._supervise(), name=self.name)

    async def stop(self) -> None:
        """Cancel the loop and wait until it has finished."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _supervise(self) -> None:
        task = asyncio.current_task()
        restarts = 0
        while True:
            started_at = time.monotonic()
            try:
                await self._run()
            except Exception:
                if self._task is not task:
                    return
                logger.exception("Supervised loop crashed; restarting", task_name=self.name)
            else:
                # A loop that swallowed the cancellation of ``stop`` returns here, and must not restart.
                if self._task is not task:
                    return
                logger.warning("Supervised loop exited; restarting", task_name=self.name)
            if time.monotonic() - started_at >= _LOOP_HEALTHY_RUN_SECONDS:
                restarts = 0
            restarts += 1
            await asyncio.sleep(
                retry_delay_seconds(
                    restarts,
                    initial_delay_seconds=_LOOP_RESTART_INITIAL_DELAY_SECONDS,
                    max_delay_seconds=_LOOP_RESTART_MAX_DELAY_SECONDS,
                ),
            )


def create_background_task(
    coro: Coroutine[Any, Any, Any],
    name: str | None = None,
    error_handler: Callable[[Exception], None] | None = None,
    *,
    owner: object | None = None,
    log_exceptions: bool = True,
    context: Context | None = None,
) -> asyncio.Task[Any]:
    """Create a background task that won't block the main execution.

    Args:
        coro: The coroutine to run in the background
        name: Optional name for the task (for logging)
        error_handler: Optional error handler function
        owner: Optional logical owner used for scoped shutdown waits
        log_exceptions: Whether unhandled task exceptions should be logged automatically
        context: Execution context for the task and its completion callback. ``None``
            preserves asyncio's normal caller-context inheritance.

    Returns:
        The created task

    """
    task: asyncio.Task[Any] = asyncio.create_task(coro, context=context)
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

    # Otherwise the callback captures the creator's context, which a detached task would keep until it finishes.
    task.add_done_callback(_task_done_callback, context=context)
    return task


def _tasks_for_owner(owner: object | None) -> tuple[asyncio.Task[Any], ...]:
    if owner is None:
        return tuple(_background_tasks)
    return tuple(task for task in _background_tasks if _background_task_owners.get(task) is owner)


async def _cancel_and_drain_background_tasks(
    tasks: tuple[asyncio.Task[Any], ...],
    *,
    owner: object | None,
    shutdown_intent: RuntimeShutdownIntent,
) -> None:
    pending_tasks = tasks
    for _ in range(_MAX_BACKGROUND_TASK_CANCEL_ROUNDS):
        if not pending_tasks:
            return
        for task in pending_tasks:
            request_task_cancel(
                task,
                cancel_source=shutdown_intent.cancel_source,
                process_shutdown=shutdown_intent.hands_off_unfinished_work,
            )
        done, _pending = await asyncio.wait(
            pending_tasks,
            timeout=_BACKGROUND_TASK_CANCEL_ROUND_TIMEOUT_SECONDS,
        )
        await asyncio.gather(*done, return_exceptions=True)
        await asyncio.sleep(0)
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
    shutdown_intent: RuntimeShutdownIntent = GENERIC_SHUTDOWN,
) -> bool:
    """Wait for all background tasks to complete.

    Args:
        timeout: Optional timeout in seconds
        owner: Optional logical owner to scope the wait to one bot
        shutdown_intent: Shutdown provenance to preserve when timeout cancellation is needed

    Returns:
        True when all tasks completed, False when timeout cancellation was needed.

    """
    deadline: float | None = None
    if timeout is not None:
        deadline = asyncio.get_running_loop().time() + timeout

    while True:
        tasks = _tasks_for_owner(owner)
        if not tasks:
            return True

        remaining: float | None = None
        if deadline is not None:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                logger.warning("background_tasks_wait_timeout", timeout_seconds=timeout)
                await _cancel_and_drain_background_tasks(tasks, owner=owner, shutdown_intent=shutdown_intent)
                return False

        done, pending = await asyncio.wait(tasks, timeout=remaining)
        await asyncio.gather(*done, return_exceptions=True)
        if pending:
            logger.warning("background_tasks_wait_timeout", timeout_seconds=timeout)
            await _cancel_and_drain_background_tasks(tuple(pending), owner=owner, shutdown_intent=shutdown_intent)
            return False
