"""Tests for the MindRoom MCP toolkit wrapper."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from agno.tools.function import ToolResult

from mindroom.constants import resolve_runtime_paths
from mindroom.credentials import CredentialsManager
from mindroom.mcp.config import MCPServerConfig
from mindroom.mcp.toolkit import MindRoomMCPToolkit
from mindroom.mcp.types import MCPDiscoveredTool, MCPServerCatalog
from mindroom.oauth.providers import OAuthConnectionRequired
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, resolve_worker_target

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget


class _DummyManager:
    def __init__(self) -> None:
        self.calls: list[
            tuple[
                str,
                str,
                dict[str, object],
                CredentialsManager | None,
                ResolvedWorkerTarget | None,
                float | None,
            ]
        ] = []

    async def call_tool(
        self,
        server_id: str,
        remote_tool_name: str,
        arguments: dict[str, object],
        *,
        timeout_seconds: float | None = None,
        credentials_manager: CredentialsManager | None = None,
        worker_target: ResolvedWorkerTarget | None = None,
    ) -> ToolResult:
        """Record the call and return a fixed tool result."""
        self.calls.append((server_id, remote_tool_name, arguments, credentials_manager, worker_target, timeout_seconds))
        return ToolResult(content="ok")


class _OAuthRequiredManager:
    def cached_request_catalog(
        self,
        _server_id: str,
        *,
        worker_target: ResolvedWorkerTarget | None,
    ) -> MCPServerCatalog | None:
        """No requester catalog is cached before the OAuth connection exists."""
        del worker_target
        return None

    async def get_request_catalog(
        self,
        _server_id: str,
        *,
        credentials_manager: CredentialsManager | None,
        worker_target: ResolvedWorkerTarget | None,
    ) -> MCPServerCatalog:
        """Force the bridge path to emit the existing OAuth-required payload."""
        del credentials_manager, worker_target
        message = "Example MCP is not connected for this agent."
        raise OAuthConnectionRequired(
            message,
            provider_id="mcp_demo",
            connect_url="http://localhost:8765/api/oauth/mcp_demo/authorize?connect_token=opaque",
        )


class _RequesterAwareManager:
    def __init__(self, catalog: MCPServerCatalog) -> None:
        self.catalog = catalog
        self.cached_catalog_requests: list[tuple[str, ResolvedWorkerTarget | None]] = []
        self.catalog_requests: list[tuple[str, CredentialsManager | None, ResolvedWorkerTarget | None]] = []
        self.calls: list[
            tuple[
                str,
                str,
                dict[str, object],
                CredentialsManager | None,
                ResolvedWorkerTarget | None,
                float | None,
            ]
        ] = []

    async def get_request_catalog(
        self,
        server_id: str,
        *,
        credentials_manager: CredentialsManager | None,
        worker_target: ResolvedWorkerTarget | None,
    ) -> MCPServerCatalog:
        """Return the requester-specific catalog and record its scope."""
        self.catalog_requests.append((server_id, credentials_manager, worker_target))
        return self.catalog

    async def call_tool(
        self,
        server_id: str,
        remote_tool_name: str,
        arguments: dict[str, object],
        *,
        timeout_seconds: float | None = None,
        credentials_manager: CredentialsManager | None = None,
        worker_target: ResolvedWorkerTarget | None = None,
    ) -> ToolResult:
        """Record the requester-scoped MCP call and return a fixed result."""
        self.calls.append((server_id, remote_tool_name, arguments, credentials_manager, worker_target, timeout_seconds))
        return ToolResult(content="ok")

    def cached_request_catalog(
        self,
        server_id: str,
        *,
        worker_target: ResolvedWorkerTarget | None,
    ) -> MCPServerCatalog | None:
        """Return a cached requester catalog for typed OAuth tool registration."""
        self.cached_catalog_requests.append((server_id, worker_target))
        return self.catalog


def _catalog(*tools: MCPDiscoveredTool) -> MCPServerCatalog:
    return MCPServerCatalog(
        server_id="demo",
        tool_name="mcp_demo",
        tool_prefix="demo",
        tools=tools,
        instructions=None,
        catalog_hash="hash",
    )


def _runtime_paths(tmp_path: Path) -> RuntimePaths:
    return resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path, process_env={})


def _oauth_server_config() -> MCPServerConfig:
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


def _worker_target() -> ResolvedWorkerTarget:
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="code",
        requester_id="@alice:example.test",
        room_id="!room:example.test",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id=None,
        tenant_id="tenant",
        account_id=None,
    )
    return resolve_worker_target("user", "code", identity)


@pytest.mark.asyncio
async def test_mcp_toolkit_registers_async_functions_and_calls_manager() -> None:
    """Expose cached remote tools as async functions backed by the manager."""
    manager = _DummyManager()
    toolkit = MindRoomMCPToolkit(
        server_id="demo",
        manager=manager,
        catalog=_catalog(
            MCPDiscoveredTool(
                remote_name="echo",
                function_name="demo_echo",
                description="Echo",
                input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
                output_schema=None,
            ),
        ),
        call_timeout_seconds=15,
    )
    result = await toolkit.async_functions["demo_echo"].entrypoint(text="hello")
    assert result.content == "ok"
    assert manager.calls == [("demo", "echo", {"text": "hello"}, None, None, 15.0)]


@pytest.mark.asyncio
async def test_oauth_mcp_toolkit_returns_structured_oauth_required_payload(tmp_path: Path) -> None:
    """Bridge functions should return the same structured OAuth prompt as other tools."""
    toolkit = MindRoomMCPToolkit(
        server_id="demo",
        manager=_OAuthRequiredManager(),
        catalog=None,
        server_config=_oauth_server_config(),
        runtime_paths=_runtime_paths(tmp_path),
        credentials_manager=CredentialsManager(tmp_path / "credentials"),
        worker_target=_worker_target(),
    )

    payload = json.loads(await toolkit.async_functions["demo_list_tools"].entrypoint())

    assert payload == {
        "error": "Example MCP is not connected for this agent.",
        "oauth_connection_required": True,
        "provider": "mcp_demo",
        "connect_url": "http://localhost:8765/api/oauth/mcp_demo/authorize?connect_token=opaque",
    }


@pytest.mark.asyncio
async def test_oauth_mcp_toolkit_bridge_passes_requester_scope_to_manager(tmp_path: Path) -> None:
    """Bridge calls must carry the credential manager and worker target to the MCP manager."""
    catalog = _catalog(
        MCPDiscoveredTool(
            remote_name="echo",
            function_name="demo_echo",
            description="Echo",
            input_schema={"type": "object", "properties": {}},
            output_schema=None,
        ),
    )
    manager = _RequesterAwareManager(catalog)
    credentials_manager = CredentialsManager(tmp_path / "credentials")
    worker_target = _worker_target()
    toolkit = MindRoomMCPToolkit(
        server_id="demo",
        manager=manager,
        catalog=None,
        server_config=_oauth_server_config(),
        runtime_paths=_runtime_paths(tmp_path),
        credentials_manager=credentials_manager,
        worker_target=worker_target,
        call_timeout_seconds=30,
    )

    tools_payload = json.loads(await toolkit.async_functions["demo_list_tools"].entrypoint())
    result = await toolkit.async_functions["demo_call_tool"].entrypoint(
        tool_name="echo",
        arguments={"text": "hello"},
    )

    assert tools_payload["tools"][0]["name"] == "echo"
    assert result.content == "ok"
    assert manager.catalog_requests == [
        ("demo", credentials_manager, worker_target),
        ("demo", credentials_manager, worker_target),
    ]
    assert manager.calls == [("demo", "echo", {"text": "hello"}, credentials_manager, worker_target, 30.0)]


@pytest.mark.asyncio
async def test_oauth_mcp_toolkit_registers_typed_tools_from_cached_requester_catalog(tmp_path: Path) -> None:
    """Connected requesters should get typed MCP functions in addition to bridge functions."""
    catalog = _catalog(
        MCPDiscoveredTool(
            remote_name="echo",
            function_name="demo_echo",
            description="Echo",
            input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
            output_schema=None,
        ),
    )
    manager = _RequesterAwareManager(catalog)
    credentials_manager = CredentialsManager(tmp_path / "credentials")
    worker_target = _worker_target()

    toolkit = MindRoomMCPToolkit(
        server_id="demo",
        manager=manager,
        catalog=None,
        server_config=_oauth_server_config(),
        runtime_paths=_runtime_paths(tmp_path),
        credentials_manager=credentials_manager,
        worker_target=worker_target,
        call_timeout_seconds=30,
    )
    result = await toolkit.async_functions["demo_echo"].entrypoint(text="hello")

    assert {
        "demo_connection_status",
        "demo_list_tools",
        "demo_call_tool",
        "demo_echo",
    } <= set(toolkit.async_functions)
    assert result.content == "ok"
    assert manager.cached_catalog_requests == [("demo", worker_target)]
    assert manager.calls == [("demo", "echo", {"text": "hello"}, credentials_manager, worker_target, 30.0)]


@pytest.mark.asyncio
async def test_oauth_mcp_toolkit_bridge_respects_tool_filters(tmp_path: Path) -> None:
    """Bridge list and call operations must enforce the MCP tool allowlist."""
    catalog = _catalog(
        MCPDiscoveredTool(
            remote_name="echo",
            function_name="demo_echo",
            description="Echo",
            input_schema={"type": "object", "properties": {}},
            output_schema=None,
        ),
        MCPDiscoveredTool(
            remote_name="ping",
            function_name="demo_ping",
            description="Ping",
            input_schema={"type": "object", "properties": {}},
            output_schema=None,
        ),
    )
    manager = _RequesterAwareManager(catalog)
    credentials_manager = CredentialsManager(tmp_path / "credentials")
    worker_target = _worker_target()
    toolkit = MindRoomMCPToolkit(
        server_id="demo",
        manager=manager,
        catalog=None,
        server_config=_oauth_server_config(),
        runtime_paths=_runtime_paths(tmp_path),
        credentials_manager=credentials_manager,
        worker_target=worker_target,
        include_tools=["ping"],
    )

    tools_payload = json.loads(await toolkit.async_functions["demo_list_tools"].entrypoint())
    rejected_payload = json.loads(
        await toolkit.async_functions["demo_call_tool"].entrypoint(
            tool_name="echo",
            arguments={},
        ),
    )
    result = await toolkit.async_functions["demo_call_tool"].entrypoint(
        tool_name="ping",
        arguments={},
    )

    assert [tool["name"] for tool in tools_payload["tools"]] == ["ping"]
    assert rejected_payload == {
        "error": "MCP tool 'echo' is not available for server 'demo'",
        "available_tools": ["ping"],
    }
    assert result.content == "ok"
    assert manager.calls == [("demo", "ping", {}, credentials_manager, worker_target, None)]


def test_mcp_toolkit_filters_remote_tools() -> None:
    """Apply include filters to the cached remote catalog."""
    manager = _DummyManager()
    toolkit = MindRoomMCPToolkit(
        server_id="demo",
        manager=manager,
        catalog=_catalog(
            MCPDiscoveredTool(
                remote_name="echo",
                function_name="demo_echo",
                description="Echo",
                input_schema={"type": "object", "properties": {}},
                output_schema=None,
            ),
            MCPDiscoveredTool(
                remote_name="ping",
                function_name="demo_ping",
                description="Ping",
                input_schema={"type": "object", "properties": {}},
                output_schema=None,
            ),
        ),
        include_tools=["ping"],
    )
    assert list(toolkit.async_functions) == ["demo_ping"]


def test_mcp_toolkit_rejects_duplicate_function_names() -> None:
    """Fail fast when two cached tools map to the same function name."""
    manager = _DummyManager()
    with pytest.raises(ValueError, match="Duplicate MCP function name"):
        MindRoomMCPToolkit(
            server_id="demo",
            manager=manager,
            catalog=_catalog(
                MCPDiscoveredTool(
                    remote_name="echo",
                    function_name="demo_echo",
                    description="Echo",
                    input_schema={"type": "object", "properties": {}},
                    output_schema=None,
                ),
                MCPDiscoveredTool(
                    remote_name="ping",
                    function_name="demo_echo",
                    description="Ping",
                    input_schema={"type": "object", "properties": {}},
                    output_schema=None,
                ),
            ),
        )
