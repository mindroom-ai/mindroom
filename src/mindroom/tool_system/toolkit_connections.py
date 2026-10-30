# Adapted from Agno (https://github.com/agno-agi/agno), Copyright 2025-2026 Agno Inc., Apache License 2.0; modified by MindRoom.
"""Connect the Agno toolkits that hold a connection for one run around each run that offers them.

A toolkit that sets ``requires_connect``, such as Postgres or Redshift, holds a connection its run must close. A run
connects every such toolkit first and closes what it connected once it ends; a failure either way is logged as a
warning and the run goes on.
"""

from __future__ import annotations

import asyncio
import inspect
import weakref
from functools import partial
from typing import TYPE_CHECKING, Any, Protocol

from mindroom.background_tasks import create_background_task
from mindroom.logging_config import get_logger
from mindroom.tool_system.agno_toolkits import requires_connect

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

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

    await shielded_lifecycle(run_toolkit_lifecycle(toolkit.connect), retain=close_once_connected)


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


async def shielded_lifecycle[T](lifecycle: Coroutine[Any, Any, T], *, retain: Callable[[asyncio.Task[T]], object]) -> T:
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


async def run_toolkit_lifecycle(operation: Callable[[], object]) -> None:
    """Run a toolkit's ``connect`` or ``close``: a coroutine function on the loop, a blocking one on a thread."""
    if inspect.iscoroutinefunction(operation):
        await operation()
        return
    result = await asyncio.to_thread(operation)
    if inspect.isawaitable(result):
        await result
