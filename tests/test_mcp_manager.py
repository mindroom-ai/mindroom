"""Tests for MCP server manager behavior."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Generator, Mapping
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, ClassVar, Self
from unittest.mock import patch

import mcp.types as mcp_types
import pytest
from agno.models.openai import OpenAIChat
from mcp.types import CallToolResult, Implementation, ListToolsResult, Tool, ToolListChangedNotification

from mindroom.agents import create_agent
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.credentials import get_runtime_credentials_manager, save_scoped_credentials
from mindroom.custom_tools.dynamic_tools import DynamicToolsToolkit
from mindroom.mcp.config import MCPServerConfig
from mindroom.mcp.errors import MCPProtocolError, MCPTimeoutError, MCPToolCallError
from mindroom.mcp.manager import MCPServerManager
from mindroom.mcp.toolkit import bind_mcp_server_manager
from mindroom.mcp.transports import _MCPTransportHandle
from mindroom.tool_system import dynamic_toolkits as dynamic_toolkits_module
from mindroom.tool_system.dynamic_toolkits import get_loaded_tools_for_session
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, resolve_worker_target
from tests.identity_helpers import persist_entity_accounts

if TYPE_CHECKING:
    from datetime import timedelta
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.mcp.types import MCPServerState
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget


_MessageHandler = Callable[[object], Awaitable[None]]


class _ConfigStub:
    def __init__(self, mcp_servers: dict[str, MCPServerConfig]) -> None:
        self.mcp_servers = mcp_servers
        self.plugins: list[object] = []
        self.agents: dict[str, object] = {}
        self.defaults = type("_DefaultsStub", (), {"allow_self_config": False})()

    def get_agent_tools(self, _agent_name: str) -> list[str]:
        return []

    def get_agent_available_tools(self, _agent_name: str) -> list[str]:
        return []


class _FakeClientSession:
    sessions: ClassVar[list[_FakeClientSession]] = []
    planned_tool_results: ClassVar[list[CallToolResult | Exception]] = []
    planned_tool_pages: ClassVar[list[ListToolsResult]] = []
    tool_list: ClassVar[list[Tool]] = []
    listed_cursors: ClassVar[list[str | None]] = []
    call_tool_arguments: ClassVar[list[dict[str, object] | None]] = []
    initialize_delay_seconds: ClassVar[float] = 0.0
    list_tools_delay_seconds: ClassVar[float] = 0.0
    parallel_call_gate: ClassVar[asyncio.Event | None] = None
    parallel_call_target_count: ClassVar[int] = 0
    call_tool_invocation_count: ClassVar[int] = 0
    call_started_event: ClassVar[asyncio.Event | None] = None
    call_continue_event: ClassVar[asyncio.Event | None] = None
    transport_extra_headers: ClassVar[list[dict[str, str]]] = []

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
        _FakeClientSession.call_tool_arguments.append(arguments)
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
def _reset_fake_session_state() -> Generator[None, None, None]:
    dynamic_toolkits_module._loaded_tools.clear()
    _FakeClientSession.sessions = []
    _FakeClientSession.planned_tool_results = []
    _FakeClientSession.planned_tool_pages = []
    _FakeClientSession.tool_list = []
    _FakeClientSession.listed_cursors = []
    _FakeClientSession.call_tool_arguments = []
    _FakeClientSession.initialize_delay_seconds = 0.0
    _FakeClientSession.list_tools_delay_seconds = 0.0
    _FakeClientSession.parallel_call_gate = None
    _FakeClientSession.parallel_call_target_count = 0
    _FakeClientSession.call_tool_invocation_count = 0
    _FakeClientSession.call_started_event = None
    _FakeClientSession.call_continue_event = None
    _FakeClientSession.transport_extra_headers = []
    yield
    dynamic_toolkits_module._loaded_tools.clear()


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
        *,
        extra_headers: Mapping[str, str] | None = None,
    ) -> _MCPTransportHandle:
        _FakeClientSession.transport_extra_headers.append(dict(extra_headers or {}))
        return _MCPTransportHandle(
            transport=server_config.transport,
            opener=lambda: _fake_transport(),
        )

    monkeypatch.setattr("mindroom.mcp.manager.ClientSession", _FakeClientSession)
    monkeypatch.setattr(
        "mindroom.mcp.manager.build_transport_handle",
        _build_fake_handle,
    )


def _oauth_mcp_config() -> MCPServerConfig:
    return MCPServerConfig(
        transport="streamable-http",
        url="https://mcp.example.test/mcp",
        auth={
            "type": "oauth",
            "discovery": "manual",
            "authorization_url": "https://auth.example.test/authorize",
            "token_url": "https://auth.example.test/token",
        },
    )


def _worker_target(requester_id: str) -> ResolvedWorkerTarget:
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="code",
        requester_id=requester_id,
        room_id="!room:example.test",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id=None,
        tenant_id="tenant",
        account_id=None,
    )
    return resolve_worker_target("user", "code", identity)


def _save_mcp_oauth_credentials(runtime_paths: RuntimePaths, worker_target: ResolvedWorkerTarget, token: str) -> None:
    credentials_manager = get_runtime_credentials_manager(runtime_paths)
    credentials_manager.save_credentials("mcp_demo_oauth_client", {"client_id": "public-client"})
    save_scoped_credentials(
        "mcp_demo_oauth",
        {
            "token": token,
            "client_id": "public-client",
            "scopes": [],
            "_source": "oauth",
            "_oauth_provider": "mcp_demo",
        },
        credentials_manager=credentials_manager,
        worker_target=worker_target,
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
async def test_mcp_manager_uses_requester_oauth_bearer_token(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """OAuth-backed MCP sessions send the current requester's bearer token."""
    _patch_manager(monkeypatch)
    _FakeClientSession.tool_list = [_tool("echo")]
    _FakeClientSession.planned_tool_results = [
        CallToolResult(content=[mcp_types.TextContent(type="text", text="pong")]),
    ]
    runtime_paths = _runtime_paths(tmp_path)
    worker_target = _worker_target("@alice:example.test")
    _save_mcp_oauth_credentials(runtime_paths, worker_target, "alice-token")
    credentials_manager = get_runtime_credentials_manager(runtime_paths)
    manager = MCPServerManager(runtime_paths)
    config = _ConfigStub({"demo": _oauth_mcp_config()})

    changed = await manager.sync_servers(config)
    catalog = await manager.get_request_catalog(
        "demo",
        credentials_manager=credentials_manager,
        worker_target=worker_target,
    )
    result = await manager.call_tool(
        "demo",
        "echo",
        {"value": "ping"},
        credentials_manager=credentials_manager,
        worker_target=worker_target,
    )

    assert changed == set()
    assert [tool.remote_name for tool in catalog.tools] == ["echo"]
    assert result.content == "pong"
    assert _FakeClientSession.transport_extra_headers == [{"Authorization": "Bearer alice-token"}]


@pytest.mark.asyncio
async def test_mcp_manager_serializes_requester_oauth_token_resolution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Concurrent calls for one requester should share one locked OAuth state."""
    runtime_paths = _runtime_paths(tmp_path)
    worker_target = _worker_target("@alice:example.test")
    manager = MCPServerManager(runtime_paths)
    await manager.sync_servers(_ConfigStub({"demo": _oauth_mcp_config()}))
    first_token_started = asyncio.Event()
    allow_first_token = asyncio.Event()
    active_token_resolutions = 0
    max_active_token_resolutions = 0

    async def fake_oauth_access_token(
        _state: MCPServerState,
        *,
        credentials_manager: object,
        worker_target: object,
    ) -> str:
        del credentials_manager, worker_target
        nonlocal active_token_resolutions, max_active_token_resolutions
        active_token_resolutions += 1
        max_active_token_resolutions = max(max_active_token_resolutions, active_token_resolutions)
        if active_token_resolutions == 1:
            first_token_started.set()
            await allow_first_token.wait()
        await asyncio.sleep(0)
        active_token_resolutions -= 1
        return "alice-token"

    monkeypatch.setattr(manager, "_oauth_access_token", fake_oauth_access_token)

    first_call = asyncio.create_task(
        manager._request_state_and_headers(
            "demo",
            credentials_manager=None,
            worker_target=worker_target,
        ),
    )
    await first_token_started.wait()
    second_call = asyncio.create_task(
        manager._request_state_and_headers(
            "demo",
            credentials_manager=None,
            worker_target=worker_target,
        ),
    )
    await asyncio.sleep(0)
    assert len(manager._scoped_states) == 1
    assert max_active_token_resolutions == 1

    allow_first_token.set()
    first_result, second_result = await asyncio.gather(first_call, second_call)

    assert first_result[0] is second_result[0]
    assert first_result[1] == {"Authorization": "Bearer alice-token"}
    assert second_result[1] == {"Authorization": "Bearer alice-token"}
    assert max_active_token_resolutions == 1


@pytest.mark.asyncio
async def test_mcp_manager_refreshes_stale_requester_oauth_session_before_tool_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """OAuth tool calls should refresh requester catalogs marked stale by server notifications."""
    _patch_manager(monkeypatch)
    _FakeClientSession.tool_list = [_tool("echo")]
    _FakeClientSession.planned_tool_results = [
        CallToolResult(content=[mcp_types.TextContent(type="text", text="pong")]),
    ]
    runtime_paths = _runtime_paths(tmp_path)
    worker_target = _worker_target("@alice:example.test")
    _save_mcp_oauth_credentials(runtime_paths, worker_target, "alice-token")
    credentials_manager = get_runtime_credentials_manager(runtime_paths)
    manager = MCPServerManager(runtime_paths)
    await manager.sync_servers(_ConfigStub({"demo": _oauth_mcp_config()}))
    await manager.get_request_catalog(
        "demo",
        credentials_manager=credentials_manager,
        worker_target=worker_target,
    )
    request_state = next(iter(manager._scoped_states.values()))
    request_state.stale = True

    result = await manager.call_tool(
        "demo",
        "echo",
        {},
        credentials_manager=credentials_manager,
        worker_target=worker_target,
    )

    assert result.content == "pong"
    assert request_state.stale is False
    assert len(_FakeClientSession.sessions) == 2
    assert _FakeClientSession.transport_extra_headers == [
        {"Authorization": "Bearer alice-token"},
        {"Authorization": "Bearer alice-token"},
    ]


@pytest.mark.asyncio
async def test_mcp_manager_separates_oauth_sessions_by_requester(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Two requesters should get separate OAuth MCP sessions and bearer tokens."""
    _patch_manager(monkeypatch)
    _FakeClientSession.tool_list = [_tool("echo")]
    runtime_paths = _runtime_paths(tmp_path)
    alice_target = _worker_target("@alice:example.test")
    bob_target = _worker_target("@bob:example.test")
    _save_mcp_oauth_credentials(runtime_paths, alice_target, "alice-token")
    _save_mcp_oauth_credentials(runtime_paths, bob_target, "bob-token")
    credentials_manager = get_runtime_credentials_manager(runtime_paths)
    manager = MCPServerManager(runtime_paths)
    await manager.sync_servers(_ConfigStub({"demo": _oauth_mcp_config()}))

    await manager.get_request_catalog("demo", credentials_manager=credentials_manager, worker_target=alice_target)
    await manager.get_request_catalog("demo", credentials_manager=credentials_manager, worker_target=bob_target)

    assert _FakeClientSession.transport_extra_headers == [
        {"Authorization": "Bearer alice-token"},
        {"Authorization": "Bearer bob-token"},
    ]
    assert len(manager._scoped_states) == 2


@pytest.mark.asyncio
async def test_mcp_manager_allows_same_oauth_typed_tools_for_multiple_requesters(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Equivalent requester-scoped catalogs should not collide within one OAuth server."""
    _patch_manager(monkeypatch)
    _FakeClientSession.tool_list = [_tool("echo")]
    runtime_paths = _runtime_paths(tmp_path)
    alice_target = _worker_target("@alice:example.test")
    bob_target = _worker_target("@bob:example.test")
    _save_mcp_oauth_credentials(runtime_paths, alice_target, "alice-token")
    _save_mcp_oauth_credentials(runtime_paths, bob_target, "bob-token")
    credentials_manager = get_runtime_credentials_manager(runtime_paths)
    manager = MCPServerManager(runtime_paths)
    config = Config.validate_with_runtime(
        {
            "mcp_servers": {
                "demo": _oauth_mcp_config().model_dump(exclude_none=True),
            },
            "agents": {
                "code": {
                    "display_name": "Code",
                    "role": "Use MCP",
                    "tools": ["mcp_demo"],
                    "worker_scope": "user",
                },
            },
        },
        runtime_paths,
    )
    await manager.sync_servers(config)

    alice_catalog = await manager.get_request_catalog(
        "demo",
        credentials_manager=credentials_manager,
        worker_target=alice_target,
    )
    bob_catalog = await manager.get_request_catalog(
        "demo",
        credentials_manager=credentials_manager,
        worker_target=bob_target,
    )

    assert [tool.function_name for tool in alice_catalog.tools] == ["demo_echo"]
    assert [tool.function_name for tool in bob_catalog.tools] == ["demo_echo"]
    assert manager.failed_server_ids() == set()
    assert len(manager._scoped_states) == 2


@pytest.mark.asyncio
async def test_mcp_manager_disconnects_requester_oauth_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Disconnect should close only the current requester's OAuth-backed MCP session."""
    _patch_manager(monkeypatch)
    _FakeClientSession.tool_list = [_tool("echo")]
    runtime_paths = _runtime_paths(tmp_path)
    alice_target = _worker_target("@alice:example.test")
    bob_target = _worker_target("@bob:example.test")
    _save_mcp_oauth_credentials(runtime_paths, alice_target, "alice-token")
    _save_mcp_oauth_credentials(runtime_paths, bob_target, "bob-token")
    credentials_manager = get_runtime_credentials_manager(runtime_paths)
    manager = MCPServerManager(runtime_paths)
    await manager.sync_servers(_ConfigStub({"demo": _oauth_mcp_config()}))
    await manager.get_request_catalog("demo", credentials_manager=credentials_manager, worker_target=alice_target)
    await manager.get_request_catalog("demo", credentials_manager=credentials_manager, worker_target=bob_target)
    alice_session = _FakeClientSession.sessions[0]
    bob_session = _FakeClientSession.sessions[1]

    await manager.disconnect_request_session("demo", worker_target=alice_target)

    assert alice_session.closed is True
    assert bob_session.closed is False
    assert len(manager._scoped_states) == 1


@pytest.mark.asyncio
async def test_mcp_manager_preserves_empty_tool_arguments(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Forward zero-argument MCP calls as {} instead of omitting arguments entirely."""
    _patch_manager(monkeypatch)
    _FakeClientSession.tool_list = [_tool("echo")]
    _FakeClientSession.planned_tool_results = [
        CallToolResult(content=[mcp_types.TextContent(type="text", text="pong")]),
    ]
    manager = MCPServerManager(_runtime_paths(tmp_path))
    config = _ConfigStub({"demo": MCPServerConfig(transport="stdio", command="npx")})
    await manager.sync_servers(config)

    result = await manager.call_tool("demo", "echo", {})

    assert result.content == "pong"
    assert _FakeClientSession.call_tool_arguments == [{}]


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
async def test_mcp_manager_reconnect_notifies_when_catalog_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Propagate reconnect-time catalog changes through the configured callback."""
    _patch_manager(monkeypatch)
    _FakeClientSession.tool_list = [_tool("echo")]
    catalog_changes: list[str] = []

    async def on_catalog_change(server_id: str) -> None:
        catalog_changes.append(server_id)

    manager = MCPServerManager(_runtime_paths(tmp_path), on_catalog_change=on_catalog_change)
    config = _ConfigStub({"demo": MCPServerConfig(transport="stdio", command="npx")})
    await manager.sync_servers(config)

    _FakeClientSession.tool_list = [_tool("echo"), _tool("ping")]
    _FakeClientSession.planned_tool_results = [
        BrokenPipeError("transport closed"),
        CallToolResult(content=[mcp_types.TextContent(type="text", text="pong")]),
    ]

    result = await manager.call_tool("demo", "echo", {"value": "ping"})

    assert result.content == "pong"
    assert catalog_changes == ["demo"]
    assert [tool.remote_name for tool in manager.get_catalog("demo").tools] == ["echo", "ping"]


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
        state.stale = False
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
async def test_mcp_manager_reschedules_refresh_when_catalog_goes_stale_mid_refresh(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A second tools-changed notification during refresh should schedule a follow-up refresh."""
    _patch_manager(monkeypatch)
    _FakeClientSession.tool_list = [_tool("echo")]
    manager = MCPServerManager(_runtime_paths(tmp_path))
    config = _ConfigStub({"demo": MCPServerConfig(transport="stdio", command="npx")})
    await manager.sync_servers(config)

    refresh_started = asyncio.Event()
    second_refresh_started = asyncio.Event()
    allow_first_refresh_to_finish = asyncio.Event()
    allow_second_refresh_to_finish = asyncio.Event()
    refresh_calls: list[bool] = []

    async def fake_refresh(
        state: MCPServerState,
        *,
        notify: bool,
        expected_refresh_revision: int | None = None,
    ) -> bool:
        del expected_refresh_revision
        refresh_calls.append(notify)
        state.stale = False
        if len(refresh_calls) == 1:
            refresh_started.set()
            await allow_first_refresh_to_finish.wait()
        if len(refresh_calls) == 2:
            second_refresh_started.set()
            await allow_second_refresh_to_finish.wait()
        return False

    monkeypatch.setattr(manager, "_refresh_server_catalog", fake_refresh)
    message_handler = _FakeClientSession.sessions[0].message_handler
    assert message_handler is not None

    await message_handler(
        mcp_types.ServerNotification(
            ToolListChangedNotification(method="notifications/tools/list_changed"),
        ),
    )
    first_refresh_task = manager._states["demo"].refresh_task
    assert first_refresh_task is not None
    await refresh_started.wait()

    await message_handler(
        mcp_types.ServerNotification(
            ToolListChangedNotification(method="notifications/tools/list_changed"),
        ),
    )

    allow_first_refresh_to_finish.set()
    await asyncio.wait_for(second_refresh_started.wait(), timeout=1)
    second_refresh_task = manager._states["demo"].refresh_task
    assert second_refresh_task is not None
    assert second_refresh_task is not first_refresh_task
    allow_second_refresh_to_finish.set()
    await asyncio.wait_for(first_refresh_task, timeout=1)
    await asyncio.wait_for(second_refresh_task, timeout=1)

    assert refresh_calls == [True, True]


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
    """Reject colliding function names when the same agent can see both MCP servers."""
    _patch_manager(monkeypatch)
    _FakeClientSession.tool_list = [_tool("echo")]
    runtime_paths = _runtime_paths(tmp_path)
    manager = MCPServerManager(runtime_paths)
    config = Config.validate_with_runtime(
        {
            "defaults": {"tools": []},
            "mcp_servers": {
                "demo": {
                    "transport": "stdio",
                    "command": "npx",
                    "tool_prefix": "shared",
                },
                "other": {
                    "transport": "stdio",
                    "command": "npx",
                    "tool_prefix": "shared",
                },
            },
            "agents": {
                "code": {
                    "display_name": "Code",
                    "role": "Write code",
                    "tools": ["mcp_demo", "mcp_other"],
                },
            },
        },
        runtime_paths,
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
async def test_mcp_manager_marks_oauth_bridge_function_name_collisions_as_failed(
    tmp_path: Path,
) -> None:
    """OAuth bridge functions should collide like discovered MCP catalog functions."""
    runtime_paths = _runtime_paths(tmp_path)
    manager = MCPServerManager(runtime_paths)
    oauth_server = _oauth_mcp_config().model_copy(update={"tool_prefix": "shared"})
    config = Config.validate_with_runtime(
        {
            "mcp_servers": {
                "demo": oauth_server.model_dump(exclude_none=True),
                "other": oauth_server.model_dump(exclude_none=True),
            },
            "agents": {
                "code": {
                    "display_name": "Code",
                    "role": "Use MCP",
                    "tools": ["mcp_demo", "mcp_other"],
                    "worker_scope": "user",
                },
            },
        },
        runtime_paths,
    )

    changed = await manager.sync_servers(config)

    assert changed == set()
    assert manager.failed_server_ids() == {"demo", "other"}
    demo_error = manager._states["demo"].last_error
    other_error = manager._states["other"].last_error
    assert isinstance(demo_error, MCPProtocolError)
    assert isinstance(other_error, MCPProtocolError)
    assert "shared_list_tools" in str(demo_error)
    assert "demo, other" in str(demo_error)


@pytest.mark.asyncio
async def test_mcp_manager_marks_oauth_bridge_local_function_name_collisions_as_failed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """OAuth bridge functions should not collide with local tool functions on the same agent."""
    runtime_paths = _runtime_paths(tmp_path)
    manager = MCPServerManager(runtime_paths)

    class _FakeToolkit:
        def __init__(self) -> None:
            self.functions = {"demo_list_tools": object()}
            self.async_functions = {}
            self.tools = ()

    monkeypatch.setattr("mindroom.mcp.manager.get_tool_by_name", lambda *_args, **_kwargs: _FakeToolkit())
    config = Config.validate_with_runtime(
        {
            "mcp_servers": {
                "demo": _oauth_mcp_config().model_dump(exclude_none=True),
            },
            "agents": {
                "code": {
                    "display_name": "Code",
                    "role": "Use MCP",
                    "tools": ["shell", "mcp_demo"],
                    "worker_scope": "user",
                },
            },
        },
        runtime_paths,
    )

    changed = await manager.sync_servers(config)

    assert changed == set()
    assert manager.failed_server_ids() == {"demo"}
    error = manager._states["demo"].last_error
    assert isinstance(error, MCPProtocolError)
    assert "demo_list_tools" in str(error)
    assert "existing MindRoom tool function" in str(error)


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


@pytest.mark.asyncio
async def test_mcp_manager_marks_local_function_name_collisions_as_failed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Reject MCP functions that collide with configured non-MCP tool functions."""
    _patch_manager(monkeypatch)
    _FakeClientSession.tool_list = [_tool("shell_command")]
    runtime_paths = _runtime_paths(tmp_path)
    config = Config.validate_with_runtime(
        {
            "mcp_servers": {
                "demo": {
                    "transport": "stdio",
                    "command": "npx",
                    "tool_prefix": "run",
                },
            },
            "agents": {
                "code": {
                    "display_name": "Code",
                    "role": "Write code",
                    "tools": ["shell", "mcp_demo"],
                },
            },
        },
        runtime_paths,
    )
    manager = MCPServerManager(runtime_paths)

    changed = await manager.sync_servers(config)

    assert changed == set()
    assert manager.failed_server_ids() == {"demo"}
    error = manager._states["demo"].last_error
    assert isinstance(error, MCPProtocolError)
    assert "run_shell_command" in str(error)
    assert "existing MindRoom tool function" in str(error)


@pytest.mark.asyncio
async def test_mcp_manager_ignores_deferred_unloaded_local_function_collisions_at_startup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Deferred-unloaded local tools should not collide with MCP functions until load time."""
    _patch_manager(monkeypatch)
    _FakeClientSession.tool_list = [_tool("shell_command")]
    runtime_paths = _runtime_paths(tmp_path)
    config = Config.validate_with_runtime(
        {
            "mcp_servers": {
                "demo": {
                    "transport": "stdio",
                    "command": "npx",
                    "tool_prefix": "run",
                },
            },
            "agents": {
                "code": {
                    "display_name": "Code",
                    "role": "Write code",
                    "tools": ["mcp_demo", {"shell": {"defer": True}}],
                },
            },
        },
        runtime_paths,
    )
    manager = MCPServerManager(runtime_paths)

    changed = await manager.sync_servers(config)

    assert changed == {"demo"}
    assert manager.failed_server_ids() == set()
    assert manager.function_name_collision_messages_for_loaded_tools("code", ["shell"]) == [
        "MCP function name 'run_shell_command' collides with an existing MindRoom tool function",
    ]

    bind_mcp_server_manager(manager)
    try:
        dynamic_manager = DynamicToolsToolkit(agent_name="code", config=config, session_id="thread-a")
        payload = json.loads(dynamic_manager.load_tool("shell"))
    finally:
        bind_mcp_server_manager(None)

    assert payload["status"] == "function_name_collision"
    assert payload["collision_messages"] == [
        "MCP function name 'run_shell_command' collides with an existing MindRoom tool function",
    ]


@pytest.mark.asyncio
async def test_mcp_manager_uses_deferred_tool_overrides_for_load_time_collision_validation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Deferred local tool overrides should shape the load-time function collision surface."""
    _patch_manager(monkeypatch)
    _FakeClientSession.tool_list = [_tool("shell_command")]
    runtime_paths = _runtime_paths(tmp_path)
    config = Config.validate_with_runtime(
        {
            "mcp_servers": {
                "demo": {
                    "transport": "stdio",
                    "command": "npx",
                    "tool_prefix": "run",
                },
            },
            "agents": {
                "code": {
                    "display_name": "Code",
                    "role": "Write code",
                    "tools": ["mcp_demo", {"shell": {"defer": True, "enable_run_shell_command": False}}],
                },
            },
        },
        runtime_paths,
    )
    manager = MCPServerManager(runtime_paths)

    changed = await manager.sync_servers(config)

    assert changed == {"demo"}
    assert manager.failed_server_ids() == set()
    assert manager.function_name_collision_messages_for_loaded_tools("code", ["shell"]) == []

    bind_mcp_server_manager(manager)
    try:
        dynamic_manager = DynamicToolsToolkit(agent_name="code", config=config, session_id="thread-a")
        payload = json.loads(dynamic_manager.load_tool("shell"))
    finally:
        bind_mcp_server_manager(None)

    assert payload["status"] == "loaded"
    assert payload["loaded_tools"] == ["shell"]


@pytest.mark.asyncio
async def test_mcp_manager_uses_deferred_mcp_filters_for_load_time_collision_validation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Deferred MCP filters should shape the load-time remote function collision surface."""
    _patch_manager(monkeypatch)
    _FakeClientSession.tool_list = [_tool("shell_command"), _tool("safe")]
    runtime_paths = _runtime_paths(tmp_path)
    config = Config.validate_with_runtime(
        {
            "mcp_servers": {
                "demo": {
                    "transport": "stdio",
                    "command": "npx",
                    "tool_prefix": "run",
                },
            },
            "agents": {
                "code": {
                    "display_name": "Code",
                    "role": "Write code",
                    "tools": ["shell", {"mcp_demo": {"defer": True, "include_tools": ["safe"]}}],
                },
            },
        },
        runtime_paths,
    )
    manager = MCPServerManager(runtime_paths)

    changed = await manager.sync_servers(config)

    assert changed == {"demo"}
    assert manager.failed_server_ids() == set()
    assert manager.function_name_collision_messages_for_loaded_tools("code", ["mcp_demo"]) == []

    bind_mcp_server_manager(manager)
    try:
        dynamic_manager = DynamicToolsToolkit(agent_name="code", config=config, session_id="thread-a")
        payload = json.loads(dynamic_manager.load_tool("mcp_demo"))
    finally:
        bind_mcp_server_manager(None)

    assert payload["status"] == "loaded"
    assert payload["loaded_tools"] == ["mcp_demo"]


@pytest.mark.asyncio
async def test_dynamic_load_rejects_failed_deferred_non_oauth_mcp_server_before_agent_construction(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Failed deferred-only non-OAuth MCP servers should not be persisted into the next runtime surface."""
    _patch_manager(monkeypatch)
    _FakeClientSession.tool_list = [_tool("x" * 60)]
    runtime_paths = _runtime_paths(tmp_path)
    config = Config.validate_with_runtime(
        {
            "mcp_servers": {
                "demo": {
                    "transport": "stdio",
                    "command": "npx",
                },
            },
            "models": {
                "default": {
                    "provider": "openai",
                    "id": "gpt-4o-mini",
                },
            },
            "agents": {
                "code": {
                    "display_name": "Code",
                    "role": "Write code",
                    "tools": [{"mcp_demo": {"defer": True}}],
                },
            },
        },
        runtime_paths,
    )
    persist_entity_accounts(config, runtime_paths)
    manager = MCPServerManager(runtime_paths)

    changed = await manager.sync_servers(config)

    assert changed == set()
    assert manager.failed_server_ids() == {"demo"}

    bind_mcp_server_manager(manager)
    try:
        session_id = "failed-mcp-thread"
        dynamic_manager = DynamicToolsToolkit(agent_name="code", config=config, session_id=session_id)
        payload = json.loads(dynamic_manager.load_tool("mcp_demo"))

        assert payload["status"] == "tool_unavailable"
        assert payload["loaded_tools"] == []
        assert "MCP server 'demo' is unavailable" in payload["unavailable_messages"][0]
        assert get_loaded_tools_for_session(agent_name="code", config=config, session_id=session_id) == []

        model = OpenAIChat(id="gpt-4o-mini", api_key="sk-test")
        with patch("mindroom.model_loading.get_model_instance", return_value=model):
            agent = create_agent(
                "code",
                config,
                runtime_paths,
                execution_identity=None,
                session_id=session_id,
                include_interactive_questions=False,
            )
    finally:
        bind_mcp_server_manager(None)

    tool_names = [tool.name for tool in agent.tools]
    assert "mcp_demo" not in tool_names
    assert "dynamic_tools" in tool_names


@pytest.mark.asyncio
async def test_mcp_manager_marks_oauth_typed_function_name_collisions_as_failed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Requester-scoped OAuth catalogs should also participate in function-name validation."""
    _patch_manager(monkeypatch)
    _FakeClientSession.tool_list = [_tool("echo")]
    runtime_paths = _runtime_paths(tmp_path)
    worker_target = _worker_target("@alice:example.test")
    _save_mcp_oauth_credentials(runtime_paths, worker_target, "alice-token")
    credentials_manager = get_runtime_credentials_manager(runtime_paths)
    manager = MCPServerManager(runtime_paths)

    class _FakeToolkit:
        def __init__(self) -> None:
            self.functions = {"demo_echo": object()}
            self.async_functions = {}
            self.tools = ()

    monkeypatch.setattr("mindroom.mcp.manager.get_tool_by_name", lambda *_args, **_kwargs: _FakeToolkit())
    config = Config.validate_with_runtime(
        {
            "mcp_servers": {
                "demo": _oauth_mcp_config().model_dump(exclude_none=True),
            },
            "agents": {
                "code": {
                    "display_name": "Code",
                    "role": "Use MCP",
                    "tools": ["shell", "mcp_demo"],
                    "worker_scope": "user",
                },
            },
        },
        runtime_paths,
    )
    await manager.sync_servers(config)

    with pytest.raises(MCPProtocolError, match="demo_echo"):
        await manager.get_request_catalog(
            "demo",
            credentials_manager=credentials_manager,
            worker_target=worker_target,
        )

    assert manager.failed_server_ids() == {"demo"}
    assert manager._states["demo"].last_error is not None


@pytest.mark.asyncio
async def test_mcp_manager_marks_oauth_typed_bridge_function_name_collisions_as_failed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Requester-scoped typed tools should not overwrite OAuth bridge functions."""
    _patch_manager(monkeypatch)
    _FakeClientSession.tool_list = [_tool("list_tools")]
    runtime_paths = _runtime_paths(tmp_path)
    worker_target = _worker_target("@alice:example.test")
    _save_mcp_oauth_credentials(runtime_paths, worker_target, "alice-token")
    credentials_manager = get_runtime_credentials_manager(runtime_paths)
    manager = MCPServerManager(runtime_paths)
    config = Config.validate_with_runtime(
        {
            "mcp_servers": {
                "demo": _oauth_mcp_config().model_dump(exclude_none=True),
            },
            "agents": {
                "code": {
                    "display_name": "Code",
                    "role": "Use MCP",
                    "tools": ["mcp_demo"],
                    "worker_scope": "user",
                },
            },
        },
        runtime_paths,
    )
    await manager.sync_servers(config)

    with pytest.raises(MCPProtocolError, match="demo_list_tools"):
        await manager.get_request_catalog(
            "demo",
            credentials_manager=credentials_manager,
            worker_target=worker_target,
        )

    assert manager.failed_server_ids() == {"demo"}
    error = manager._states["demo"].last_error
    assert isinstance(error, MCPProtocolError)
    assert "collides within server 'demo'" in str(error)


@pytest.mark.asyncio
async def test_mcp_manager_allows_local_function_name_collisions_on_other_agents(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Only reject local collisions when the same agent can see both tool surfaces."""
    _patch_manager(monkeypatch)
    _FakeClientSession.tool_list = [_tool("shell_command")]
    runtime_paths = _runtime_paths(tmp_path)
    config = Config.validate_with_runtime(
        {
            "mcp_servers": {
                "demo": {
                    "transport": "stdio",
                    "command": "npx",
                    "tool_prefix": "run",
                },
            },
            "agents": {
                "shell_only": {
                    "display_name": "Shell Only",
                    "role": "Run shell commands",
                    "tools": ["shell"],
                },
                "mcp_only": {
                    "display_name": "MCP Only",
                    "role": "Use MCP tools",
                    "tools": ["mcp_demo"],
                },
            },
        },
        runtime_paths,
    )
    manager = MCPServerManager(runtime_paths)

    changed = await manager.sync_servers(config)

    assert changed == {"demo"}
    assert manager.failed_server_ids() == set()


@pytest.mark.asyncio
async def test_mcp_manager_cancellation_closes_transport_during_discovery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Cancelling discovery should still close the in-flight session transport."""
    _patch_manager(monkeypatch)
    _FakeClientSession.tool_list = [_tool("echo")]
    _FakeClientSession.initialize_delay_seconds = 0.1
    manager = MCPServerManager(_runtime_paths(tmp_path))
    config = _ConfigStub({"demo": MCPServerConfig(transport="stdio", command="npx", startup_timeout_seconds=5.0)})

    sync_task = asyncio.create_task(manager.sync_servers(config))
    await asyncio.sleep(0.01)
    sync_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await sync_task

    assert _FakeClientSession.sessions
    assert _FakeClientSession.sessions[0].closed is True


@pytest.mark.asyncio
async def test_mcp_manager_disconnect_clears_state_even_when_close_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A close failure should not leave the state holding a poisoned exit stack."""
    _patch_manager(monkeypatch)
    _FakeClientSession.tool_list = [_tool("echo")]
    manager = MCPServerManager(_runtime_paths(tmp_path))
    config = _ConfigStub({"demo": MCPServerConfig(transport="stdio", command="npx")})
    await manager.sync_servers(config)
    state = manager._states["demo"]
    close_failed_message = "close failed"

    class _BrokenExitStack:
        async def aclose(self) -> None:
            raise RuntimeError(close_failed_message)

    state.exit_stack = _BrokenExitStack()

    with pytest.raises(RuntimeError, match=close_failed_message):
        await manager._disconnect_state(state)

    assert state.exit_stack is None
    assert state.session is None
    assert state.connected is False
