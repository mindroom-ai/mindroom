"""Typed MCP runtime structures."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from contextlib import AsyncExitStack

    from mcp import ClientSession

    from mindroom.mcp.config import MCPServerConfig
    from mindroom.mcp.errors import MCPError


class _AsyncReadWriteLock:
    """Coordinate concurrent tool calls against exclusive catalog refreshes."""

    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._active_readers = 0
        self._writer_active = False
        self._waiting_writers = 0

    @asynccontextmanager
    async def read(self) -> AsyncIterator[None]:
        """Allow concurrent readers unless one writer is pending or active."""
        async with self._condition:
            while self._writer_active or self._waiting_writers > 0:
                await self._condition.wait()
            self._active_readers += 1
        try:
            yield
        finally:
            async with self._condition:
                self._active_readers -= 1
                if self._active_readers == 0:
                    self._condition.notify_all()

    @asynccontextmanager
    async def write(self) -> AsyncIterator[None]:
        """Wait until all readers complete, then block new readers."""
        async with self._condition:
            self._waiting_writers += 1
            try:
                while self._writer_active or self._active_readers > 0:
                    await self._condition.wait()
                self._writer_active = True
            finally:
                self._waiting_writers -= 1
        try:
            yield
        finally:
            async with self._condition:
                self._writer_active = False
                self._condition.notify_all()


@dataclass(frozen=True)
class MCPDiscoveredTool:
    """One discovered remote MCP tool."""

    remote_name: str
    function_name: str
    description: str | None
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None
    title: str | None = None
    ui_resource_uri: str | None = None


@dataclass(frozen=True)
class MCPServerCatalog:
    """Cached discovery result for one server."""

    server_id: str
    tool_name: str
    tool_prefix: str
    tools: tuple[MCPDiscoveredTool, ...]
    instructions: str | None
    catalog_hash: str


@dataclass(frozen=True)
class MCPAppResource:
    """One fetched MCP Apps HTML resource ready for Matrix clients to render."""

    uri: str
    mime_type: str
    html: str
    meta: dict[str, Any] | None = None


@dataclass
class MCPServerState:
    """Live connection state for one configured server."""

    server_id: str
    config: MCPServerConfig
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    call_lock: _AsyncReadWriteLock = field(default_factory=_AsyncReadWriteLock)
    catalog: MCPServerCatalog | None = None
    session: ClientSession | None = None
    exit_stack: AsyncExitStack | None = None
    session_owner_task: asyncio.Task[None] | None = None
    session_close_event: asyncio.Event | None = None
    semaphore: asyncio.Semaphore = field(init=False)
    connected: bool = False
    stale: bool = False
    last_error: MCPError | None = None
    consecutive_failures: int = 0
    refresh_task: asyncio.Task[None] | None = None
    refresh_revision: int = 0
    oauth_access_token_hash: str | None = None

    def __post_init__(self) -> None:
        """Initialize the per-server concurrency limiter."""
        self.semaphore = asyncio.Semaphore(self.config.max_concurrent_calls)
