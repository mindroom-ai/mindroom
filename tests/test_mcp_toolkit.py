"""Tests for the MindRoom MCP toolkit wrapper."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from agno.tools.function import ToolResult

from mindroom.constants import resolve_runtime_paths
from mindroom.credentials.manager import CredentialsManager
from mindroom.mcp.config import MCPServerConfig
from mindroom.mcp.errors import MCPToolCallError, MCPToolUnavailableError
from mindroom.mcp.toolkit import MindRoomMCPToolkit
from mindroom.mcp.types import MCPDiscoveredTool, MCPServerCatalog
from mindroom.oauth.providers import OAuthConnectionRequired
from mindroom.tool_system.agno_toolkits import toolkit_function
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, resolve_worker_target

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.config.auth import AuthorizationConfig
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.tool_function import ToolFunction
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
        authorization: AuthorizationConfig | None = None,
        include_tools: list[str] | None = None,
        exclude_tools: list[str] | None = None,
    ) -> ToolResult:
        """Record the call and return a fixed tool result."""
        del authorization, include_tools, exclude_tools
        self.calls.append((server_id, remote_tool_name, arguments, credentials_manager, worker_target, timeout_seconds))
        return ToolResult(content="ok")


class _OAuthRequiredManager:
    def __init__(self, cached_catalog: MCPServerCatalog | None = None) -> None:
        self.cached_catalog = cached_catalog

    @staticmethod
    def _connection_required() -> OAuthConnectionRequired:
        message = "Example MCP is not connected for this agent."
        return OAuthConnectionRequired(
            message,
            provider_id="mcp_demo",
            connect_url="http://localhost:8765/api/oauth/mcp_demo/authorize?connect_token=opaque",
        )

    def cached_request_catalog(
        self,
        _server_id: str,
        *,
        worker_target: ResolvedWorkerTarget | None,
        authorization: AuthorizationConfig | None = None,
    ) -> MCPServerCatalog | None:
        """Return the requester catalog captured when this fake manager was built."""
        del worker_target, authorization
        return self.cached_catalog

    async def get_request_catalog(
        self,
        _server_id: str,
        *,
        credentials_manager: CredentialsManager | None,
        worker_target: ResolvedWorkerTarget | None,
        authorization: AuthorizationConfig | None = None,
    ) -> MCPServerCatalog:
        """Force the bridge path to emit the existing OAuth-required payload."""
        del credentials_manager, worker_target, authorization
        raise self._connection_required()

    async def call_tool(self, *_args: object, **_kwargs: object) -> ToolResult:
        """Force bridge calls to emit the same OAuth-required payload as discovery."""
        raise self._connection_required()


class _RequesterAwareManager:
    def __init__(self, catalog: MCPServerCatalog) -> None:
        self.catalog = catalog
        self.cached_catalog_requests: list[tuple[str, ResolvedWorkerTarget | None]] = []
        self.catalog_requests: list[tuple[str, CredentialsManager | None, ResolvedWorkerTarget | None]] = []
        self.call_filters: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
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
        authorization: AuthorizationConfig | None = None,
    ) -> MCPServerCatalog:
        """Return the requester-specific catalog and record its scope."""
        del authorization
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
        authorization: AuthorizationConfig | None = None,
        include_tools: list[str] | None = None,
        exclude_tools: list[str] | None = None,
    ) -> ToolResult:
        """Record the requester-scoped MCP call and return a fixed result."""
        del authorization
        included = tuple(include_tools or ())
        excluded = tuple(exclude_tools or ())
        self.call_filters.append((included, excluded))
        available_tools = tuple(
            sorted(
                tool.remote_name
                for tool in self.catalog.tools
                if (not included or tool.remote_name in included) and (not excluded or tool.remote_name not in excluded)
            ),
        )
        if remote_tool_name not in available_tools:
            raise MCPToolUnavailableError(server_id, remote_tool_name, available_tools)
        self.calls.append((server_id, remote_tool_name, arguments, credentials_manager, worker_target, timeout_seconds))
        return ToolResult(content="ok")

    def cached_request_catalog(
        self,
        server_id: str,
        *,
        worker_target: ResolvedWorkerTarget | None,
        authorization: AuthorizationConfig | None = None,
    ) -> MCPServerCatalog | None:
        """Return a cached requester catalog for typed OAuth tool registration."""
        del authorization
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


def _oauth_server_config(description: str | None = None) -> MCPServerConfig:
    return MCPServerConfig(
        transport="streamable-http",
        url="https://mcp.example.test/mcp",
        description=description,
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


def _offered(toolkit: MindRoomMCPToolkit, name: str) -> ToolFunction:
    """Return the function ``toolkit`` offers under ``name``, as an agent's run offers it."""
    function = toolkit_function(toolkit, name)
    assert function is not None
    return function


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
    result = await toolkit.functions["demo_echo"](text="hello")
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

    payload = json.loads(await toolkit.functions["demo_list_tools"]())

    assert payload == {
        "error": "Example MCP is not connected for this agent.",
        "oauth_connection_required": True,
        "provider": "mcp_demo",
        "connect_url": "http://localhost:8765/api/oauth/mcp_demo/authorize?connect_token=opaque",
        "requires_host_browser": True,
    }


@pytest.mark.asyncio
async def test_oauth_mcp_toolkit_bridge_descriptions_include_server_description(tmp_path: Path) -> None:
    """Configured server descriptions must reach the model before the requester signs in."""
    description = "Company workspace gateway: email, calendar, documents, and issue tracking."
    toolkit = MindRoomMCPToolkit(
        server_id="demo",
        manager=_OAuthRequiredManager(),
        catalog=None,
        server_config=_oauth_server_config(description),
        runtime_paths=_runtime_paths(tmp_path),
        credentials_manager=CredentialsManager(tmp_path / "credentials"),
        worker_target=_worker_target(),
    )

    for function_name in ("demo_connection_status", "demo_list_tools", "demo_call_tool"):
        assert _offered(toolkit, function_name).description.endswith(f" {description}")


@pytest.mark.asyncio
async def test_oauth_mcp_toolkit_bridge_descriptions_without_server_description(tmp_path: Path) -> None:
    """Bridge descriptions keep their original shape when no description is configured."""
    toolkit = MindRoomMCPToolkit(
        server_id="demo",
        manager=_OAuthRequiredManager(),
        catalog=None,
        server_config=_oauth_server_config(),
        runtime_paths=_runtime_paths(tmp_path),
        credentials_manager=CredentialsManager(tmp_path / "credentials"),
        worker_target=_worker_target(),
    )

    assert _offered(toolkit, "demo_connection_status").description == (
        "Check whether MCP server 'demo' is connected for this agent's credential scope."
    )
    assert _offered(toolkit, "demo_list_tools").description == (
        "List remote tools exposed by MCP server 'demo' for this agent's credential scope."
    )
    assert _offered(toolkit, "demo_call_tool").description == (
        "Call one remote tool on MCP server 'demo' for this agent's credential scope."
    )


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

    tools_payload = json.loads(await toolkit.functions["demo_list_tools"]())
    result = await toolkit.functions["demo_call_tool"](
        tool_name="echo",
        arguments={"text": "hello"},
    )

    assert tools_payload["tools"][0]["name"] == "echo"
    assert result.content == "ok"
    assert manager.catalog_requests == [
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
    result = await toolkit.functions["demo_echo"](text="hello")

    assert {
        "demo_connection_status",
        "demo_list_tools",
        "demo_call_tool",
        "demo_echo",
    } <= set(toolkit.functions)
    assert result.content == "ok"
    assert manager.cached_catalog_requests == [("demo", worker_target)]
    assert manager.calls == [("demo", "echo", {"text": "hello"}, credentials_manager, worker_target, 30.0)]


@pytest.mark.asyncio
async def test_oauth_mcp_typed_tool_returns_structured_reconnect_payload(tmp_path: Path) -> None:
    """A cached typed tool must preserve the bridge's reconnect contract after revocation."""
    catalog = _catalog(
        MCPDiscoveredTool(
            remote_name="echo",
            function_name="demo_echo",
            description="Echo",
            input_schema={"type": "object", "properties": {}},
            output_schema=None,
        ),
    )
    toolkit = MindRoomMCPToolkit(
        server_id="demo",
        manager=_OAuthRequiredManager(catalog),
        catalog=None,
        server_config=_oauth_server_config(),
        runtime_paths=_runtime_paths(tmp_path),
        credentials_manager=CredentialsManager(tmp_path / "credentials"),
        worker_target=_worker_target(),
    )

    payload = json.loads(await toolkit.functions["demo_echo"]())

    assert payload["oauth_connection_required"] is True
    assert payload["provider"] == "mcp_demo"
    assert "connect_token=opaque" in payload["connect_url"]


@pytest.mark.asyncio
async def test_oauth_mcp_typed_tool_returns_current_catalog_after_tool_removal(tmp_path: Path) -> None:
    """A typed tool cached before catalog drift must return the manager's current surface."""
    echo = MCPDiscoveredTool(
        remote_name="echo",
        function_name="demo_echo",
        description="Echo",
        input_schema={"type": "object", "properties": {}},
        output_schema=None,
    )
    ping = MCPDiscoveredTool(
        remote_name="ping",
        function_name="demo_ping",
        description="Ping",
        input_schema={"type": "object", "properties": {}},
        output_schema=None,
    )
    manager = _RequesterAwareManager(_catalog(echo))
    toolkit = MindRoomMCPToolkit(
        server_id="demo",
        manager=manager,
        catalog=None,
        server_config=_oauth_server_config(),
        runtime_paths=_runtime_paths(tmp_path),
        credentials_manager=CredentialsManager(tmp_path / "credentials"),
        worker_target=_worker_target(),
    )
    manager.catalog = _catalog(ping)

    payload = json.loads(await toolkit.functions["demo_echo"]())

    assert payload == {
        "error": "MCP tool 'echo' is not available for server 'demo'",
        "available_tools": ["ping"],
    }


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

    tools_payload = json.loads(await toolkit.functions["demo_list_tools"]())
    rejected_payload = json.loads(
        await toolkit.functions["demo_call_tool"](
            tool_name="echo",
            arguments={},
        ),
    )
    result = await toolkit.functions["demo_call_tool"](
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
    assert manager.call_filters == [(("ping",), ()), (("ping",), ())]


_GENERIC_FAILURE = "An unexpected error occurred. Please try again."


class _FailingManager(_DummyManager):
    def __init__(self, input_schema: dict[str, Any] | None) -> None:
        super().__init__()
        self.input_schema = input_schema

    async def call_tool(self, server_id: str, *_args: object, **_kwargs: object) -> ToolResult:
        """Fail every call the way a server that hides the cause fails it, with the schema the call was sent against."""
        raise MCPToolCallError(server_id, _GENERIC_FAILURE, input_schema=self.input_schema)


def _search_toolkit(schema: dict[str, Any], *, known_schema: bool = True) -> MindRoomMCPToolkit:
    """Return a toolkit offering ``search``, whose calls fail and carry ``schema`` when the manager knows it."""
    tool = MCPDiscoveredTool(
        remote_name="search",
        function_name="demo_search",
        description="Search",
        input_schema=schema,
        output_schema=None,
    )
    manager = _FailingManager(schema if known_schema else None)
    return MindRoomMCPToolkit(server_id="demo", manager=manager, catalog=_catalog(tool))


@pytest.mark.asyncio
async def test_a_typed_mcp_tool_names_arguments_its_schema_does_not_list_when_a_call_fails() -> None:
    """A generic failure names the unlisted arguments and gives the schema; with listed names it stays unchanged."""
    schema = {"type": "object", "properties": {"query": {"type": "string"}}, "additionalProperties": True}
    search = _search_toolkit(schema).functions["demo_search"]

    with pytest.raises(MCPToolCallError) as unlisted:
        await search(query="x", limit=5)
    with pytest.raises(MCPToolCallError) as listed:
        await search(query="x")

    assert str(unlisted.value) == (
        f"{_GENERIC_FAILURE}\n\n"
        "Arguments not listed in the properties of the input schema of 'search': 'limit'.\n"
        f"Input schema of 'search': {json.dumps(schema)}"
    )
    assert str(listed.value) == _GENERIC_FAILURE


@pytest.mark.asyncio
async def test_an_mcp_call_failure_names_at_most_twenty_unlisted_arguments() -> None:
    """Past twenty unlisted names, the note counts the rest."""
    search = _search_toolkit({"type": "object", "properties": {}}).functions["demo_search"]

    with pytest.raises(MCPToolCallError) as failed:
        await search(**{f"name{index}": index for index in range(23)})

    named = ", ".join(f"'name{index}'" for index in range(20))
    assert f"'search': {named}, and 3 more.\n" in str(failed.value)


@pytest.mark.asyncio
async def test_an_mcp_call_failure_stays_unchanged_when_the_called_tools_schema_is_unknown() -> None:
    """A failure that does not carry the schema the call was sent against keeps the server's error."""
    search = _search_toolkit({"type": "object", "properties": {}}, known_schema=False).functions["demo_search"]

    with pytest.raises(MCPToolCallError) as failed:
        await search(limit=5)

    assert str(failed.value) == _GENERIC_FAILURE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "schema",
    [
        {"type": "object", "properties": {"query": {"type": "string", "description": "wörd " * 50_000}}},
        {"type": "object", "properties": {"kind": {"enum": [f"kind-{index}" for index in range(50_000)]}}},
    ],
    ids=["long-description", "long-enum"],
)
async def test_an_mcp_call_failure_repeats_at_most_a_bounded_part_of_a_large_schema(schema: dict[str, Any]) -> None:
    """A schema past the bound is cut, unescaped, and the error says it was cut and how long the whole schema is."""
    whole = json.dumps(schema, ensure_ascii=False)

    with pytest.raises(MCPToolCallError) as failed:
        await _search_toolkit(schema).functions["demo_search"](unlisted=1)

    hint = str(failed.value).split("Input schema of 'search': ", 1)[1]
    assert hint == f"{whole[:4000]}... [truncated, {len(whole)} characters in all]"


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
    assert list(toolkit.functions) == ["demo_ping"]


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
