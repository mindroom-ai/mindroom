# Adapted from Agno (https://github.com/agno-agi/agno), Copyright 2025-2026 Agno Inc., Apache License 2.0; modified by MindRoom.
"""Connect the Agno toolkits that hold a connection around each run or call that uses them.

A toolkit that sets ``requires_connect``, such as Postgres or Redshift, holds a connection its run must close. A run
connects every such toolkit first and closes what it connected once it ends; a failure either way is logged as a
warning and the run goes on. A call that builds its own toolkit, as the MCP gateway and script runs do, holds it for
that call alone (``toolkit_call_lifetime``).
"""

from __future__ import annotations

import asyncio
import inspect
import weakref
from contextlib import asynccontextmanager
from contextvars import copy_context
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Any, Protocol

from mindroom.background_tasks import create_background_task
from mindroom.logging_config import get_logger
from mindroom.tool_system.agno_toolkits import requires_connect
from mindroom.tool_system.tool_hooks import SyncToolCompletionTracker, track_sync_tool_completion

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Collection, Coroutine
    from concurrent.futures import Executor

    from mindroom.tool_system.agno_toolkits import AnyToolkit
    from mindroom.tool_system.tool_function import ToolkitClose, ToolkitConnect

logger = get_logger(__name__)


class _ConnectingToolkit(Protocol):
    """What a run reads of a toolkit that holds a connection for one run."""

    name: str

    def connect(self) -> object:
        """Open the toolkit's connection."""
        ...

    def close(self) -> object:
        """Close the toolkit's connection."""
        ...


# The latest cleanup of each toolkit's connect that a cancelled run stopped waiting for; once done, it closed what it
# connected.
_pending_cleanups: weakref.WeakKeyDictionary[_ConnectingToolkit, asyncio.Task[None]] = weakref.WeakKeyDictionary()


def run_connection(toolkit: AnyToolkit) -> ToolkitConnect | None:
    """Return how a run connects ``toolkit`` before its first request, or None for a toolkit that holds no connection."""
    if requires_connect(toolkit):
        return partial(_connect, toolkit)
    return None


async def _connect(toolkit: _ConnectingToolkit) -> ToolkitClose | None:
    if (cleanup := _pending_cleanups.get(toolkit)) is not None and not cleanup.done():
        # That cleanup closes the toolkit instance, so connecting first would let it close this run's connection.
        await asyncio.wait({cleanup})
    try:
        await _connect_shielded(toolkit)
    except Exception as exc:
        logger.warning("toolkit_connect_failed", toolkit=toolkit.name, error=str(exc))
        return None
    return partial(_close, toolkit)


async def _connect_shielded(toolkit: _ConnectingToolkit) -> None:
    """Connect ``toolkit`` on the loop or a thread; a cancelled caller stops waiting, and what connects is closed."""

    def close_once_connected(connecting: asyncio.Task[None]) -> None:
        cleanup = create_background_task(_close_once_connected(toolkit, connecting), name="toolkit-connect-cleanup")
        _pending_cleanups[toolkit] = cleanup

    await _shielded_lifecycle(run_toolkit_lifecycle(toolkit.connect), retain=close_once_connected)


async def _close_once_connected(toolkit: _ConnectingToolkit, connecting: asyncio.Task[None]) -> None:
    try:
        await connecting
    except Exception as exc:
        logger.warning("toolkit_connect_failed", toolkit=toolkit.name, error=str(exc))
        return
    await _close(toolkit)


async def _close(toolkit: _ConnectingToolkit) -> None:
    try:
        await run_toolkit_lifecycle(toolkit.close)
    except Exception as exc:
        logger.warning("toolkit_close_failed", toolkit=toolkit.name, error=str(exc))


async def _shielded_lifecycle[T](
    lifecycle: Coroutine[Any, Any, T],
    *,
    retain: Callable[[asyncio.Task[T]], object],
) -> T:
    """Run ``lifecycle`` in a task; a caller cancelled meanwhile stops waiting and hands the task to ``retain``.

    A blocking connect or close holds its thread until it returns, so the task goes on after the cancellation, and
    ``retain`` keeps it for whoever closes what it opened or counts it as work still owned.
    """
    task = asyncio.create_task(lifecycle, name="toolkit-lifecycle")
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        retain(task)
        raise


async def run_toolkit_lifecycle(operation: Callable[[], object], *, executor: Executor | None = None) -> None:
    """Run a toolkit's ``connect`` or ``close``: a coroutine function on the loop, a blocking one on a thread.

    The thread comes from ``executor``, None the loop's default one, and runs in the caller's context.
    """
    if inspect.iscoroutinefunction(operation):
        await operation()
        return
    result = await asyncio.get_running_loop().run_in_executor(executor, copy_context().run, operation)
    if inspect.isawaitable(result):
        await result


async def close_connected_toolkit(toolkit: AnyToolkit, *, executor: Executor | None = None) -> None:
    """Close ``toolkit`` when it holds a connection, as ``run_toolkit_lifecycle`` runs its ``close``."""
    if requires_connect(toolkit):
        await run_toolkit_lifecycle(toolkit.close, executor=executor)


@dataclass(slots=True)
class RetainedToolkitCleanups:
    """Toolkit work that outlives the call that started it, held until it finishes so a shutdown can wait for it.

    A failed task logs ``failure_event`` with its error type alone, since its error can name a backend's details.
    ``adopt`` hands each task to whoever else counts it as work still owned, as the MCP gateway's execution lease does.
    """

    failure_event: str
    adopt: Callable[[asyncio.Task[Any]], object] | None = None
    tasks: set[asyncio.Task[None]] = field(default_factory=set, init=False)

    def retain(self, task: asyncio.Task[None]) -> None:
        """Hold ``task`` until it finishes."""
        if self.adopt is not None:
            self.adopt(task)
        self.tasks.add(task)
        task.add_done_callback(self._finished)

    def _finished(self, task: asyncio.Task[None]) -> None:
        self.tasks.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.warning(self.failure_event, error_type=type(error).__name__)

    async def drain(self, timeout_seconds: float | None = None) -> bool:
        """Wait for every retained task, and each one retained meanwhile, without cancelling one.

        Return False once ``timeout_seconds``, None for no bound, ran out first.
        """
        loop = asyncio.get_running_loop()
        deadline = None if timeout_seconds is None else loop.time() + max(0.0, timeout_seconds)
        while tasks := {task for task in self.tasks if not task.done()}:
            remaining = None if deadline is None else deadline - loop.time()
            if remaining is not None and remaining <= 0:
                return False
            await asyncio.wait(tasks, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
        return True


@asynccontextmanager
async def toolkit_call_lifetime(
    toolkit: AnyToolkit,
    cleanups: RetainedToolkitCleanups,
    *,
    executor: Executor | None = None,
) -> AsyncIterator[None]:
    """Connect ``toolkit`` for one call, own every synchronous body the block starts, then close it.

    A failed connect closes the toolkit and raises the connect's error, with a close error as its cause, before the
    block runs; a cancelled connect hands the close to ``cleanups``. When the block is cancelled, or ends while a body
    it started still runs, ``cleanups`` closes the toolkit once those bodies return and the caller does not wait, and a
    body's failure, which already reached the call, is not raised again; otherwise the caller waits for the close and
    sees its error. ``executor`` runs the blocking connect, close, and bodies; None is the loop's default executor.
    """
    lifecycle = partial(run_toolkit_lifecycle, executor=executor)
    connected = toolkit if requires_connect(toolkit) else None
    if connected is not None:
        try:
            await _shielded_lifecycle(
                lifecycle(connected.connect),
                retain=lambda connecting: _retain_close(cleanups, toolkit, after=[connecting], executor=executor),
            )
        except asyncio.CancelledError:
            raise
        except BaseException as connect_error:
            try:
                await _shielded_lifecycle(lifecycle(connected.close), retain=cleanups.retain)
            except asyncio.CancelledError:
                raise
            except BaseException as close_error:
                raise connect_error from close_error
            raise
    tracker = SyncToolCompletionTracker(executor=executor)
    cancelled = False
    try:
        with track_sync_tool_completion(tracker):
            yield
    except asyncio.CancelledError:
        cancelled = True
        raise
    finally:
        # A body the call started that still runs, after a cancel or beside a failure, keeps the toolkit.
        running = [body for body in tracker.settle() if not body.done()]
        if running or (cancelled and connected is not None):
            _retain_close(cleanups, toolkit, after=running, executor=executor)
        elif connected is not None:
            await _shielded_lifecycle(lifecycle(connected.close), retain=cleanups.retain)


def _retain_close(
    cleanups: RetainedToolkitCleanups,
    toolkit: AnyToolkit,
    *,
    after: Collection[asyncio.Future[Any]],
    executor: Executor | None,
) -> None:
    """Have ``cleanups`` close ``toolkit`` once ``after`` finished, however it finished."""

    async def close_after() -> None:
        await asyncio.gather(*after, return_exceptions=True)
        await close_connected_toolkit(toolkit, executor=executor)

    cleanups.retain(asyncio.create_task(close_after(), name="toolkit-cleanup"))
