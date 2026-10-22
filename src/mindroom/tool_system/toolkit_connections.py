# Adapted from Agno (https://github.com/agno-agi/agno), Copyright 2025-2026 Agno Inc., Apache License 2.0; modified by MindRoom.
"""Connect the Agno toolkits that hold a connection for one run, as Agno 3.0.9's agent did around each of its runs.

An Agno ``MCPTools`` registers its functions only once its session is connected, and a toolkit that sets
``requires_connect``, such as Postgres or Redshift, holds a connection its run must close. Before each run Agno's
agent connected every such toolkit, an MCP session only when it was not connected already, and after the run it
closed what that run had connected; a failure either way was logged as a warning and the run went on.
"""

from __future__ import annotations

import asyncio
import inspect
from functools import partial
from typing import TYPE_CHECKING, Any, cast

from mindroom.background_tasks import create_background_task
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from agno.tools import Toolkit
    from agno.tools.mcp import MCPTools

    from mindroom.tool_system.tool_function import ToolkitClose, ToolkitConnect

logger = get_logger(__name__)


def _mcp_toolkit(toolkit: Toolkit) -> MCPTools | None:
    """Return ``toolkit`` as an Agno MCP toolkit, or None for any other toolkit."""
    # Agno recognizes its MCP toolkits by class name so the optional MCP SDK is never imported.
    if any(base.__name__ == "MCPTools" for base in type(toolkit).__mro__):
        return cast("MCPTools", toolkit)
    return None


def connects_per_run(toolkit: Toolkit) -> bool:
    """Whether ``toolkit`` holds a connection for one run, so each of its calls must finish inside that run."""
    return toolkit.requires_connect or _mcp_toolkit(toolkit) is not None


def is_unconnected_mcp_toolkit(toolkit: Toolkit) -> bool:
    """Whether ``toolkit`` is an Agno MCP toolkit that is not connected yet, so it offers no functions until it is."""
    mcp = _mcp_toolkit(toolkit)
    return mcp is not None and not mcp.initialized


def register_mcp_functions(toolkit: Toolkit) -> None:
    """Register the functions of an Agno MCP toolkit that is not connected yet, which it lists only once connected.

    An agent is built off the event loop its runs use and before any of them, and its approval rules, tool owners,
    and prompt are derived from the functions it offers, so the build connects the toolkit through a session and a
    loop of its own and closes that session once the functions are registered. Each run connects it again.
    Building such a toolkit on an event loop's thread raises ``RuntimeError``, since it cannot list there.
    """
    if not is_unconnected_mcp_toolkit(toolkit):
        return
    if _on_event_loop():
        msg = f"MCP toolkit {toolkit.name!r} must be built off the event loop, since it lists on a loop of its own"
        raise RuntimeError(msg)
    mcp = cast("MCPTools", toolkit)

    async def list_functions() -> None:
        await mcp.connect()
        await mcp.close()

    try:
        asyncio.run(list_functions())
    except Exception as exc:
        logger.warning("mcp_toolkit_listing_failed", toolkit=toolkit.name, error=str(exc))


def _on_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def run_connection(toolkit: Toolkit) -> ToolkitConnect | None:
    """Return how a run connects ``toolkit`` before its first request, or None for a toolkit that holds no connection."""
    mcp = _mcp_toolkit(toolkit)
    if mcp is not None:
        return partial(_connect_mcp, mcp)
    if toolkit.requires_connect:
        return partial(_connect, toolkit)
    return None


async def _connect_mcp(toolkit: MCPTools) -> ToolkitClose | None:
    # A session connected before the run belongs to whoever connected it, so the run leaves it open.
    if toolkit.initialized:
        return None
    return await _connect(toolkit)


async def _connect(toolkit: Toolkit) -> ToolkitClose | None:
    try:
        if inspect.iscoroutinefunction(toolkit.connect):
            # An async session may have to close in the task that opened it, so it connects in the run's own task.
            await toolkit.connect()
        else:
            await _connect_on_thread(toolkit)
    except Exception as exc:
        logger.warning("toolkit_connect_failed", toolkit=toolkit.name, error=str(exc))
        return None
    return partial(_close, toolkit)


async def _connect_on_thread(toolkit: Toolkit) -> None:
    """Connect ``toolkit`` on a thread; a caller cancelled meanwhile stops waiting, and the connection closes once made."""

    def close_once_connected(connecting: asyncio.Task[None]) -> None:
        create_background_task(_close_once_connected(toolkit, connecting), name="toolkit-connect-cleanup")

    await shielded_lifecycle(run_toolkit_lifecycle(toolkit.connect), retain=close_once_connected)


async def _close_once_connected(toolkit: Toolkit, connecting: asyncio.Task[None]) -> None:
    try:
        await connecting
    except Exception as exc:
        logger.warning("toolkit_connect_failed", toolkit=toolkit.name, error=str(exc))
        return
    await _close(toolkit)


async def _close(toolkit: Toolkit) -> None:
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
