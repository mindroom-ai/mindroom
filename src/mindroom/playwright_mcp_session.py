"""One-task stdio MCP ownership with serialized calls and cancellable active work."""

from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

if TYPE_CHECKING:
    from collections.abc import Callable

    from anyio.streams.memory import MemoryObjectReceiveStream
    from mcp.shared.message import SessionMessage
    from mcp.types import CallToolResult, Tool

PLAYWRIGHT_MCP_PACKAGE = "@playwright/mcp@0.0.78"

logger = logging.getLogger(__name__)


async def _drain_closed_session(stream: MemoryObjectReceiveStream[SessionMessage | Exception]) -> None:
    """Keep late server output from cancelling the transport's process cleanup."""
    try:
        async for _message in stream:
            pass
    except (anyio.ClosedResourceError, anyio.EndOfStream):
        pass


@dataclass(slots=True)
class _QueuedCall:
    tool_name: str | None
    arguments: dict[str, object]
    future: asyncio.Future[object]


class PlaywrightMCPSession:
    """Own one lazy stdio transport and serialize discovery and tool calls on one task.

    Callers supply launch configuration and interpret results; this owner enters
    and exits the MCP contexts and owns the subprocess lifetime. A request's
    deadline includes startup and queueing. Timeout or caller cancellation,
    even while queued, retires the whole session just like explicit close:
    active work is interrupted and queued work fails without dispatch.

    Retirement waits for context exit and bounded process cleanup before
    settling interrupted calls. The optional cleanup callback for cancelled
    active calls runs after process cleanup, when late output can no longer
    appear. Cleanup exceptions are logged with their traceback, without
    replacing the caller's cancellation/timeout or queued retirement errors.
    They do not make repeated close fail or rerun cleanup. Successful calls
    leave the transport open. A retired instance cannot reopen; its provider
    must create a fresh session.
    """

    def __init__(
        self,
        parameters: StdioServerParameters,
        *,
        call_timeout_seconds: float = 90.0,
        cancelled_call_cleanup: Callable[[str, dict[str, object]], None] | None = None,
    ) -> None:
        # Linux Chromium starts detached process groups. A dedicated subreaper
        # owns those descendants even when the MCP Node server crashes first.
        self._parameters = (
            parameters.model_copy(
                update={
                    "command": sys.executable,
                    "args": ["-I", "-m", "mindroom.playwright_mcp_process", parameters.command, *parameters.args],
                },
            )
            if sys.platform == "linux"
            else parameters
        )
        self._timeout = call_timeout_seconds
        self._cleanup = cancelled_call_cleanup
        self._queue: asyncio.Queue[_QueuedCall] = asyncio.Queue()
        self._actor_task: asyncio.Task[None] | None = None
        self._work_scope: anyio.CancelScope | None = None
        self._closed = False

    @property
    def running(self) -> bool:
        """Whether an actor still owns live stdio resources."""
        return self._actor_task is not None and not self._actor_task.done()

    async def list_tools(self) -> tuple[Tool, ...]:
        """Discover the live surface on the same serialized session."""
        return cast("tuple[Tool, ...]", await self._request(None, {}))

    async def call_tool(self, name: str, arguments: dict[str, object]) -> CallToolResult:
        """Dispatch one native call; timeout or cancellation permanently retires this actor."""
        return cast("CallToolResult", await self._request(name, arguments))

    async def _request(self, name: str | None, arguments: dict[str, object]) -> object:
        if self._closed:
            msg = "Playwright MCP session is closed."
            raise RuntimeError(msg)
        future = asyncio.get_running_loop().create_future()
        self._queue.put_nowait(_QueuedCall(name, arguments, future))
        if self._actor_task is None:
            self._actor_task = asyncio.create_task(self._run_actor(), name="playwright_mcp_session")
        try:
            async with asyncio.timeout(self._timeout):
                return await future
        except (TimeoutError, asyncio.CancelledError) as error:
            future.cancel()
            try:
                await self.close()
            except asyncio.CancelledError:
                if isinstance(error, asyncio.CancelledError):
                    raise error from None
                raise
            raise

    async def close(self) -> None:
        """Permanently retire all requests and wait for owned process cleanup.

        Repeated close is safe. Cancellation of the waiter cannot interrupt
        context exit or the SDK's bounded process termination/reaping, but is
        propagated once cleanup has finished.
        """
        self._closed = True
        if self._work_scope is not None:
            self._work_scope.cancel()
        task = self._actor_task
        if task is not None:
            # The actor exits its work scope before closing MCP contexts, so anyio
            # cancellation cannot interrupt stdio's bounded TERM/KILL cleanup.
            cancellation: asyncio.CancelledError | None = None
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError as exc:
                    cancellation = cancellation or exc
            await task
            if cancellation is not None:
                raise cancellation

    async def _run_actor(self) -> None:  # noqa: C901, PLR0912, PLR0915 - one task owns all MCP contexts
        active: _QueuedCall | None = None
        cancelled: list[_QueuedCall] = []
        drain: asyncio.Task[None] | None = None
        error: BaseException = RuntimeError("Playwright MCP session is closed.")
        try:
            # The request deadline covers startup; failure closes entered contexts.
            async with stdio_client(self._parameters) as (read_stream, write_stream):
                try:
                    # Session closure must not close the transport's last reader:
                    # a late initialize/tool reply otherwise raises BrokenResourceError
                    # in the SDK reader and interrupts its subprocess cleanup.
                    async with ClientSession(read_stream.clone(), write_stream.clone()) as session:
                        with anyio.CancelScope() as scope:
                            self._work_scope = scope
                            if self._closed:
                                scope.cancel()
                            await session.initialize()
                            while True:
                                active = await self._queue.get()
                                if active.future.done():
                                    active = None
                                    continue
                                try:
                                    if active.tool_name is None:
                                        result = tuple((await session.list_tools()).tools)
                                    else:
                                        result = await session.call_tool(active.tool_name, active.arguments)
                                    if not active.future.done():
                                        active.future.set_result(result)
                                finally:
                                    if (
                                        active.future.cancelled()
                                        and self._cleanup is not None
                                        and active.tool_name is not None
                                    ):
                                        cancelled.append(active)
                                active = None
                finally:
                    drain = asyncio.create_task(_drain_closed_session(read_stream))
        except Exception as exc:
            error = exc
        finally:
            if drain is not None:
                drain.cancel()
                await asyncio.gather(drain, return_exceptions=True)
            try:
                # The process tree has stopped before removing late screenshot output.
                if self._cleanup is not None:
                    for call in cancelled:
                        assert call.tool_name is not None
                        try:
                            self._cleanup(call.tool_name, call.arguments)
                        except Exception:
                            logger.exception("Playwright MCP cancelled-call cleanup failed for %s", call.tool_name)
            finally:
                # Fallible caller cleanup must never bypass the owner's settlement.
                self._closed = True
                self._work_scope = None
                if active is not None and not active.future.done():
                    active.future.set_exception(error)
                while not self._queue.empty():
                    queued = self._queue.get_nowait()
                    if not queued.future.done():
                        queued.future.set_exception(error)
