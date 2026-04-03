"""Tests for MCP server manager behavior."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, ClassVar, Self

import mcp.types as mcp_types
import pytest
from mcp.types import CallToolResult, Implementation, ListToolsResult, Tool, ToolListChangedNotification

from mindroom.constants import resolve_runtime_paths
from mindroom.mcp.config import MCPServerConfig
from mindroom.mcp.errors import MCPProtocolError, MCPTimeoutError, MCPToolCallError
from mindroom.mcp.manager import MCPServerManager
from mindroom.mcp.transports import MCPTransportHandle

if TYPE_CHECKING:
    from datetime import timedelta
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.mcp.types import MCPServerState


_MessageHandler = Callable[[object], Awaitable[None]]


class _ConfigStub:
    def __init__(self, mcp_servers: dict[str, MCPServerConfig]) -> None:
        self.mcp_servers = mcp_servers


class _FakeClientSession:
    sessions: ClassVar[list[_FakeClientSession]] = []
    planned_tool_results: ClassVar[list[CallToolResult | Exception]] = []
    planned_tool_pages: ClassVar[list[ListToolsResult]] = []
    tool_list: ClassVar[list[Tool]] = []
    listed_cursors: ClassVar[list[str | None]] = []
    initialize_delay_seconds: ClassVar[float] = 0.0
    list_tools_delay_seconds: ClassVar[float] = 0.0
    parallel_call_gate: ClassVar[asyncio.Event | None] = None
    parallel_call_target_count: ClassVar[int] = 0
    call_tool_invocation_count: ClassVar[int] = 0
    call_started_event: ClassVar[asyncio.Event | None] = None
    call_continue_event: ClassVar[asyncio.Event | None] = None

    def __init__(
        self,
        _read_stream: object,
        _write_stream: object,
        *,
        read_timeout_seconds: timedelta | None = None,
        message_handler: _MessageHandler | None = None,
        **_: object,
    ) -> None:
        self.message_handler = message_handler
        self.read_timeout_seconds = read_timeout_seconds
        self.closed = False
        _FakeClientSession.sessions.append(self)

    async def __aenter__(self) -> Self:
        """Return the fake session as an async context manager."""
        return self

    async def __aexit__(self, *_args: object) -> None:
        """Mark the fake session as closed when the context exits."""
        self.closed = True

    async def initialize(self) -> mcp_types.InitializeResult:
        """Return a minimal MCP initialize response."""
        if _FakeClientSession.initialize_delay_seconds > 0:
            await asyncio.sleep(_FakeClientSession.initialize_delay_seconds)
        return mcp_types.InitializeResult(
            protocolVersion="2025-03-26",
            capabilities=mcp_types.ServerCapabilities(),
            serverInfo=Implementation(name="demo", version="1.0"),
            instructions="demo server",
        )

    async def list_tools(self, cursor: str | None = None) -> ListToolsResult:
        """Return the planned tool list, including paginated responses when configured."""
        _FakeClientSession.listed_cursors.append(cursor)
        if _FakeClientSession.list_tools_delay_seconds > 0:
            await asyncio.sleep(_FakeClientSession.list_tools_delay_seconds)
        if _FakeClientSession.planned_tool_pages:
            return _FakeClientSession.planned_tool_pages.pop(0)
        assert cursor is None
        return ListToolsResult(tools=list(_FakeClientSession.tool_list))

    async def call_tool(
        self,
        _name: str,
        arguments: dict[str, object] | None = None,
        read_timeout_seconds: timedelta | None = None,
        progress_callback: object | None = None,
    ) -> CallToolResult:
        """Pop and return the next planned tool result."""
        assert progress_callback is None
        assert arguments is not None
        assert read_timeout_seconds is not None
        _FakeClientSession.call_tool_invocation_count += 1
        if _FakeClientSession.call_started_event is not None:
            _FakeClientSession.call_started_event.set()
        if _FakeClientSession.call_continue_event is not None:
            await _FakeClientSession.call_continue_event.wait()
        next_result = _FakeClientSession.planned_tool_results.pop(0)
        if (
            _FakeClientSession.parallel_call_gate is not None
            and _FakeClientSession.call_tool_invocation_count <= _FakeClientSession.parallel_call_target_count
        ):
            if _FakeClientSession.call_tool_invocation_count == _FakeClientSession.parallel_call_target_count:
                _FakeClientSession.parallel_call_gate.set()
            await _FakeClientSession.parallel_call_gate.wait()
        if isinstance(next_result, Exception):
            raise next_result
        assert isinstance(next_result, CallToolResult)
        return next_result


@pytest.fixture(autouse=True)
def _reset_fake_session_state() -> None:
    _FakeClientSession.sessions = []
    _FakeClientSession.planned_tool_results = []
    _FakeClientSession.planned_tool_pages = []
    _FakeClientSession.tool_list = []
    _FakeClientSession.listed_cursors = []
    _FakeClientSession.initialize_delay_seconds = 0.0
    _FakeClientSession.list_tools_delay_seconds = 0.0
    _FakeClientSession.parallel_call_gate = None
    _FakeClientSession.parallel_call_target_count = 0
    _FakeClientSession.call_tool_invocation_count = 0
    _FakeClientSession.call_started_event = None
    _FakeClientSession.call_continue_event = None


def _runtime_paths(tmp_path: Path) -> RuntimePaths:
    return resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path, process_env={})


def _tool(name: str) -> Tool:
    return Tool(name=name, description=f"{name} tool", inputSchema={"type": "object", "properties": {}})


@asynccontextmanager
async def _fake_transport() -> AsyncIterator[tuple[object, object]]:
    yield object(), object()


def _patch_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    def _build_fake_handle(
        _server_id: str,
        server_config: MCPServerConfig,
        _runtime_paths: RuntimePaths,
    ) -> MCPTransportHandle:
        return MCPTransportHandle(
            transport=server_config.transport,
            opener=lambda: _fake_transport(),
        )

    monkeypatch.setattr("mindroom.mcp.manager.ClientSession", _FakeClientSession)
    monkeypatch.setattr(
        "mindroom.mcp.manager.build_transport_handle",
        _build_fake_handle,
    )


@pytest.mark.asyncio
async def test_mcp_manager_syncs_catalog_and_calls_tool(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Discover a catalog and forward tool calls through the cached session."""
    _patch_manager(monkeypatch)
    _FakeClientSession.tool_list = [_tool("echo")]
    _FakeClientSession.planned_tool_results = [
        CallToolResult(content=[mcp_types.TextContent(type="text", text="pong")]),
    ]
    manager = MCPServerManager(_runtime_paths(tmp_path))
    config = _ConfigStub({"demo": MCPServerConfig(transport="stdio", command="npx")})
    changed = await manager.sync_servers(config)
    assert changed == {"demo"}
    result = await manager.call_tool("demo", "echo", {"value": "ping"})
    assert result.content == "pong"


@pytest.mark.asyncio
async def test_mcp_manager_reconnects_after_call_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Reconnect once when a tool call fails on a stale transport."""
    _patch_manager(monkeypatch)
    _FakeClientSession.tool_list = [_tool("echo")]
    _FakeClientSession.planned_tool_results = [
        BrokenPipeError("transport closed"),
        CallToolResult(content=[mcp_types.TextContent(type="text", text="pong")]),
    ]
    manager = MCPServerManager(_runtime_paths(tmp_path))
    config = _ConfigStub({"demo": MCPServerConfig(transport="stdio", command="npx")})
    await manager.sync_servers(config)
    result = await manager.call_tool("demo", "echo", {"value": "ping"})
    assert result.content == "pong"
    assert len(_FakeClientSession.sessions) == 2


@pytest.mark.asyncio
async def test_mcp_manager_does_not_retry_explicit_tool_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Do not replay non-idempotent MCP tool failures as reconnect retries."""
    _patch_manager(monkeypatch)
    _FakeClientSession.tool_list = [_tool("echo")]
    _FakeClientSession.planned_tool_results = [
        CallToolResult(
            content=[mcp_types.TextContent(type="text", text="tool exploded")],
            isError=True,
        ),
    ]
    manager = MCPServerManager(_runtime_paths(tmp_path))
    config = _ConfigStub({"demo": MCPServerConfig(transport="stdio", command="npx")})
    await manager.sync_servers(config)
    with pytest.raises(MCPToolCallError, match="tool exploded"):
        await manager.call_tool("demo", "echo", {"value": "ping"})
    assert len(_FakeClientSession.sessions) == 1


@pytest.mark.asyncio
async def test_mcp_manager_enforces_startup_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Bound transport open, initialize, and discovery under startup_timeout_seconds."""
    _patch_manager(monkeypatch)
    _FakeClientSession.tool_list = [_tool("echo")]
    _FakeClientSession.initialize_delay_seconds = 0.05
    manager = MCPServerManager(_runtime_paths(tmp_path))
    config = _ConfigStub(
        {
            "demo": MCPServerConfig(
                transport="stdio",
                command="npx",
                startup_timeout_seconds=0.01,
                call_timeout_seconds=5.0,
            ),
        },
    )
    changed = await manager.sync_servers(config)
    assert changed == set()
    state = manager._states["demo"]
    assert isinstance(state.last_error, MCPTimeoutError)
    assert "startup timed out" in str(state.last_error)


@pytest.mark.asyncio
async def test_mcp_manager_paginates_catalog_discovery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Follow MCP pagination cursors until the full tool catalog is collected."""
    _patch_manager(monkeypatch)
    _FakeClientSession.planned_tool_pages = [
        ListToolsResult(tools=[_tool("echo")], nextCursor="page-2"),
        ListToolsResult(tools=[_tool("ping")]),
    ]
    manager = MCPServerManager(_runtime_paths(tmp_path))
    config = _ConfigStub({"demo": MCPServerConfig(transport="stdio", command="npx")})
    changed = await manager.sync_servers(config)
    assert changed == {"demo"}
    catalog = manager.get_catalog("demo")
    assert [tool.remote_name for tool in catalog.tools] == ["echo", "ping"]
    assert _FakeClientSession.listed_cursors == [None, "page-2"]


@pytest.mark.asyncio
async def test_mcp_manager_deduplicates_concurrent_reconnects(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Reconnect only once when multiple in-flight callers hit the same stale transport."""
    _patch_manager(monkeypatch)
    _FakeClientSession.tool_list = [_tool("echo")]
    _FakeClientSession.parallel_call_gate = asyncio.Event()
    _FakeClientSession.parallel_call_target_count = 2
    _FakeClientSession.planned_tool_results = [
        BrokenPipeError("transport closed"),
        BrokenPipeError("transport closed"),
        CallToolResult(content=[mcp_types.TextContent(type="text", text="pong")]),
        CallToolResult(content=[mcp_types.TextContent(type="text", text="pong")]),
    ]
    manager = MCPServerManager(_runtime_paths(tmp_path))
    config = _ConfigStub(
        {"demo": MCPServerConfig(transport="stdio", command="npx", max_concurrent_calls=2)},
    )
    await manager.sync_servers(config)
    first_result, second_result = await asyncio.gather(
        manager.call_tool("demo", "echo", {"value": "ping-1"}),
        manager.call_tool("demo", "echo", {"value": "ping-2"}),
    )
    assert first_result.content == "pong"
    assert second_result.content == "pong"
    assert len(_FakeClientSession.sessions) == 2


@pytest.mark.asyncio
async def test_mcp_manager_refresh_waits_for_in_flight_calls(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Do not disconnect one catalog while an in-flight tool call still holds the transport."""
    _patch_manager(monkeypatch)
    _FakeClientSession.tool_list = [_tool("echo")]
    _FakeClientSession.planned_tool_results = [
        CallToolResult(content=[mcp_types.TextContent(type="text", text="pong")]),
    ]
    _FakeClientSession.call_started_event = asyncio.Event()
    _FakeClientSession.call_continue_event = asyncio.Event()
    manager = MCPServerManager(_runtime_paths(tmp_path))
    config = _ConfigStub({"demo": MCPServerConfig(transport="stdio", command="npx")})
    await manager.sync_servers(config)
    initial_session = _FakeClientSession.sessions[0]

    call_task = asyncio.create_task(manager.call_tool("demo", "echo", {"value": "ping"}))
    await _FakeClientSession.call_started_event.wait()

    message_handler = initial_session.message_handler
    assert message_handler is not None
    await message_handler(
        mcp_types.ServerNotification(
            ToolListChangedNotification(method="notifications/tools/list_changed"),
        ),
    )
    refresh_task = manager._states["demo"].refresh_task
    assert refresh_task is not None
    await asyncio.sleep(0)
    assert not initial_session.closed
    assert not refresh_task.done()

    _FakeClientSession.call_continue_event.set()
    result = await call_task
    assert result.content == "pong"

    await refresh_task
    assert initial_session.closed
    assert len(_FakeClientSession.sessions) == 2


@pytest.mark.asyncio
async def test_mcp_manager_handles_tools_list_changed_notifications(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Schedule a catalog refresh when the server sends a tools-changed notification."""
    _patch_manager(monkeypatch)
    _FakeClientSession.tool_list = [_tool("echo")]
    manager = MCPServerManager(_runtime_paths(tmp_path))
    config = _ConfigStub({"demo": MCPServerConfig(transport="stdio", command="npx")})
    await manager.sync_servers(config)
    refreshed: list[str] = []

    async def fake_refresh(state: MCPServerState, *, notify: bool) -> bool:
        assert notify is True
        refreshed.append(state.server_id)
        return False

    monkeypatch.setattr(manager, "_refresh_server_catalog", fake_refresh)
    message_handler = _FakeClientSession.sessions[0].message_handler
    assert message_handler is not None
    await message_handler(
        mcp_types.ServerNotification(
            ToolListChangedNotification(method="notifications/tools/list_changed"),
        ),
    )
    refresh_task = manager._states["demo"].refresh_task
    assert refresh_task is not None
    await refresh_task
    assert refreshed == ["demo"]


@pytest.mark.asyncio
async def test_mcp_manager_marks_colliding_catalogs_as_failed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Record discovery failures when remote tool names collide after prefixing."""
    _patch_manager(monkeypatch)
    _FakeClientSession.tool_list = [_tool("echo"), _tool("echo")]
    manager = MCPServerManager(_runtime_paths(tmp_path))
    config = _ConfigStub({"demo": MCPServerConfig(transport="stdio", command="npx")})
    changed = await manager.sync_servers(config)
    assert changed == set()
    assert manager.failed_server_ids() == {"demo"}


@pytest.mark.asyncio
async def test_mcp_manager_marks_cross_server_function_name_collisions_as_failed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Reject colliding function names after combining all discovered server catalogs."""
    _patch_manager(monkeypatch)
    _FakeClientSession.tool_list = [_tool("echo")]
    manager = MCPServerManager(_runtime_paths(tmp_path))
    config = _ConfigStub(
        {
            "demo": MCPServerConfig(transport="stdio", command="npx", tool_prefix="shared"),
            "other": MCPServerConfig(transport="stdio", command="npx", tool_prefix="shared"),
        },
    )
    changed = await manager.sync_servers(config)
    assert changed == set()
    assert manager.failed_server_ids() == {"demo", "other"}
    demo_error = manager._states["demo"].last_error
    other_error = manager._states["other"].last_error
    assert isinstance(demo_error, MCPProtocolError)
    assert isinstance(other_error, MCPProtocolError)
    assert "shared_echo" in str(demo_error)
    assert "demo, other" in str(demo_error)


@pytest.mark.asyncio
async def test_mcp_manager_rejects_overlong_function_names(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Fail discovery when one provider-visible function name exceeds the model limit."""
    _patch_manager(monkeypatch)
    _FakeClientSession.tool_list = [_tool("x" * 60)]
    manager = MCPServerManager(_runtime_paths(tmp_path))
    config = _ConfigStub({"demo": MCPServerConfig(transport="stdio", command="npx", tool_prefix="demo")})
    changed = await manager.sync_servers(config)
    assert changed == set()
    assert manager.failed_server_ids() == {"demo"}
    error = manager._states["demo"].last_error
    assert isinstance(error, MCPProtocolError)
    assert "at most 64 characters" in str(error)
