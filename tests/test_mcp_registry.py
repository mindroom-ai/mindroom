"""Tests for the MCP server entries of each config's tool registry."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

import mindroom.tools  # noqa: F401
from mindroom.config.main import Config, ConfigRuntimeValidationError
from mindroom.constants import resolve_runtime_paths
from mindroom.mcp.errors import MCPTimeoutError
from mindroom.mcp.manager import MCPServerManager
from mindroom.mcp.registry import (
    mcp_tool_entries,
    mcp_tool_name,
    validate_mcp_agent_overrides,
    with_catalog_function_names,
)
from mindroom.mcp.toolkit import bind_mcp_server_manager
from mindroom.mcp.types import MCPServerState
from mindroom.tool_system.agent_tools import resolve_agent_tools
from mindroom.tool_system.declarations import SetupType, ToolManagedInitArg, ToolStatus
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tool_system.plugins import config_plugins
from mindroom.tool_system.registry import ToolMetadataValidationError, builtin_tool_registry
from mindroom.tool_system.sandbox_proxy import tool_placement
from mindroom.tool_system.worker_routing import (
    ToolExecutionIdentity,
    resolve_worker_target,
    supports_tool_name_for_worker_scope,
)
from tests.conftest import register_test_tool

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.mcp.config import MCPServerConfig
    from mindroom.tool_system.registry import ToolRegistry


@pytest.fixture(autouse=True)
def _unbind_mcp_server_manager() -> Iterator[None]:
    yield
    bind_mcp_server_manager(None)


def _tools(config: Config, tmp_path: Path) -> ToolRegistry:
    return config_plugins(config, _runtime_paths(tmp_path)).tools


def _runtime_paths(tmp_path: Path) -> RuntimePaths:
    return resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path, process_env={})


def _config(tmp_path: Path) -> Config:
    return Config.validate_with_runtime(
        {
            "models": {"default": {"provider": "openai", "id": "test-model"}},
            "mcp_servers": {
                "demo": {
                    "transport": "stdio",
                    "command": "npx",
                },
            },
            "agents": {
                "code": {
                    "display_name": "Code",
                    "role": "Write code",
                    "tools": ["mcp_demo"],
                },
            },
        },
        _runtime_paths(tmp_path),
    )


def _oauth_config(tmp_path: Path) -> Config:
    return Config.validate_with_runtime(
        {
            "models": {"default": {"provider": "openai", "id": "test-model"}},
            "mcp_servers": {
                "demo": {
                    "transport": "streamable-http",
                    "url": "https://mcp.example.test/mcp",
                    "auth": {
                        "type": "oauth",
                        "discovery": "manual",
                        "authorization_url": "https://auth.example.test/authorize",
                        "token_url": "https://auth.example.test/token",
                    },
                },
            },
            "agents": {
                "code": {
                    "display_name": "Code",
                    "role": "Write code",
                    "worker_scope": "user",
                    "tools": ["mcp_demo"],
                },
            },
        },
        _runtime_paths(tmp_path),
    )


def test_an_enabled_mcp_server_is_a_tool_of_its_config(tmp_path: Path) -> None:
    """Each enabled MCP server is one buildable tool of the config that lists it."""
    tools = _tools(_config(tmp_path), tmp_path)
    tool_name = mcp_tool_name("demo")

    assert tools.get(tool_name).agent_override_fields is not None
    assert tools.factory(tool_name) is tools.get(tool_name).factory
    assert tools.mcp_server_id(tool_name) == "demo"


@pytest.mark.parametrize("worker_tools", [None, ["mcp_demo"]], ids=["mode-all", "explicit-worker-tools"])
def test_mcp_toolkits_stay_in_the_primary_when_every_tool_routes(
    tmp_path: Path,
    worker_tools: list[str] | None,
) -> None:
    """Worker configs carry no MCP servers, so an agent's MCP toolkit runs in the primary however tools route."""
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env={"MINDROOM_SANDBOX_EXECUTION_MODE": "all", "MINDROOM_SANDBOX_PROXY_URL": "http://sandbox.invalid"},
    )
    agent: dict[str, object] = {"display_name": "Code", "role": "Write code", "tools": ["mcp_demo"]}
    if worker_tools is not None:
        agent["worker_tools"] = worker_tools
    config = Config.validate_with_runtime(
        {
            "models": {"default": {"provider": "openai", "id": "test-model"}},
            "mcp_servers": {"demo": {"transport": "stdio", "command": "npx"}},
            "agents": {"code": agent},
        },
        runtime_paths,
    )
    tools = config_plugins(config, runtime_paths).tools

    assert (
        resolve_agent_tools(config, "code", tools).runs_in_worker("mcp_demo", tool_placement(runtime_paths, tools))
        is False
    )


def test_an_mcp_file_access_override_says_no_worker_can_isolate_it(tmp_path: Path) -> None:
    """A stdio MCP server is unconfined and cannot run in a worker, so the refusal does not offer worker_tools."""
    with pytest.raises(ConfigRuntimeValidationError, match="It cannot run in a worker"):
        Config.validate_with_runtime(
            {
                "models": {"default": {"provider": "openai", "id": "test-model"}},
                "mcp_servers": {"demo": {"transport": "stdio", "command": "npx"}},
                "agents": {"code": {"display_name": "Code", "tools": [{"mcp_demo": {"file_access": "workspace"}}]}},
            },
            _runtime_paths(tmp_path),
        )


@pytest.mark.parametrize(
    ("with_oauth", "provider_name", "expected_name"),
    [
        (False, None, "MCP Demo"),
        (True, "  Example Workspace  ", "Example Workspace"),
        (True, "   ", "MCP Demo"),
        (True, None, "MCP Demo"),
    ],
)
def test_mcp_display_metadata_falls_back_when_blank(
    tmp_path: Path,
    with_oauth: bool,
    provider_name: str | None,
    expected_name: str,
) -> None:
    """Empty display overrides preserve useful provider or server names."""
    config = _oauth_config(tmp_path) if with_oauth else _config(tmp_path)
    payload = config.model_dump()
    server = payload["mcp_servers"]["demo"]
    server.update(display_name="  ", summary="  ")
    if with_oauth:
        server["auth"]["display_name"] = provider_name
    config = Config.validate_with_runtime(payload, _runtime_paths(tmp_path))

    metadata = _tools(config, tmp_path).get("mcp_demo")
    assert metadata.display_name == expected_name
    assert metadata.description == "Tools provided by this MCP server"


def test_mcp_entries_never_read_a_catalog_and_listings_read_only_a_synced_one(tmp_path: Path) -> None:
    """An MCP entry names no catalog function; a listing reads them only from a server the manager has."""

    class FakeManager:
        def has_server(self, _server_id: str) -> bool:
            return False

        def get_catalog(self, server_id: str) -> object:
            msg = f"Unknown MCP server '{server_id}'"
            raise KeyError(msg)

    config = _config(tmp_path)
    bind_mcp_server_manager(FakeManager())

    entries = mcp_tool_entries(config.mcp_servers)

    assert entries["mcp_demo"].function_names == ()
    assert with_catalog_function_names(entries)["mcp_demo"].function_names == ()


def test_a_config_without_an_mcp_server_has_no_tool_for_it(tmp_path: Path) -> None:
    """Removing a server from one config leaves another config that keeps it untouched."""
    config = _config(tmp_path)
    without_server = Config.validate_with_runtime(
        {
            "models": {"default": {"provider": "openai", "id": "test-model"}},
            "agents": {"code": {"display_name": "Code", "role": "Write code"}},
        },
        _runtime_paths(tmp_path),
    )

    assert "mcp_demo" not in _tools(without_server, tmp_path)
    assert _tools(config, tmp_path).mcp_server_id("mcp_demo") == "demo"


def test_oauth_mcp_tools_are_oauth_configured_bridge_tools(tmp_path: Path) -> None:
    """Expose OAuth-backed MCP servers as OAuth-configured bridge tools."""
    metadata = _tools(_oauth_config(tmp_path), tmp_path).get("mcp_demo")

    assert metadata.setup_type is SetupType.OAUTH
    assert metadata.status is ToolStatus.REQUIRES_CONFIG
    assert metadata.auth_provider == "mcp_demo"
    assert metadata.function_names == (
        "demo_connection_status",
        "demo_list_tools",
        "demo_call_tool",
    )


def test_an_mcp_tool_may_not_reuse_a_registered_tool_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail instead of silently overwriting an existing built-in tool entry."""
    config = _config(tmp_path)
    register_test_tool(monkeypatch, replace(builtin_tool_registry().entries["shell"], name="mcp_demo"))

    with pytest.raises(ToolMetadataValidationError, match="conflicts with an existing registered tool"):
        _ = _tools(config, tmp_path)


def test_a_non_mcp_tool_whose_name_starts_with_mcp_names_no_mcp_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the entries of configured MCP servers are MCP integrations, whatever another tool's name."""
    register_test_tool(monkeypatch, replace(builtin_tool_registry().entries["shell"], name="mcp_custom_plugin"))
    tools = _tools(_config(tmp_path), tmp_path)

    assert "mcp_custom_plugin" in tools
    assert tools.mcp_server_id("mcp_custom_plugin") is None
    assert supports_tool_name_for_worker_scope("mcp_custom_plugin", "user") is True


def test_config_validation_rejects_runtime_mcp_name_collisions(tmp_path: Path) -> None:
    """Config validation rejects an MCP tool whose name a plugin tool already registers."""
    plugin_root = tmp_path / "plugins" / "demo"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        '{"name":"demo_plugin","tools_module":"tools.py","skills":[]}\n',
        encoding="utf-8",
    )
    (plugin_root / "tools.py").write_text(
        "from agno.tools import Toolkit\n"
        "from mindroom.tool_system.declarations import ToolCategory, ToolFileAccess\nfrom mindroom.tool_system.registration import register_tool_with_metadata\n"
        "\n"
        "class DemoTool(Toolkit):\n"
        "    def __init__(self) -> None:\n"
        "        super().__init__(name='demo', tools=[])\n"
        "\n"
        "@register_tool_with_metadata(\n"
        "    name='mcp_demo',\n"
        "    file_access=ToolFileAccess.NONE,\n"
        "    display_name='Plugin MCP Demo',\n"
        "    description='Should collide',\n"
        "    category=ToolCategory.DEVELOPMENT,\n"
        ")\n"
        "def demo_plugin_tools():\n"
        "    return DemoTool\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigRuntimeValidationError, match="conflicts with an existing registered tool"):
        Config.validate_with_runtime(
            {
                "models": {"default": {"provider": "openai", "id": "test-model"}},
                "plugins": ["./plugins/demo"],
                "mcp_servers": {
                    "demo": {
                        "transport": "stdio",
                        "command": "npx",
                    },
                },
                "agents": {
                    "code": {
                        "display_name": "Code",
                        "role": "Write code",
                        "tools": ["mcp_demo"],
                    },
                },
            },
            _runtime_paths(tmp_path),
        )


def test_config_validation_allows_non_mcp_prefixed_plugin_tools_on_isolating_scope(tmp_path: Path) -> None:
    """Do not reject unrelated plugin tools just because they start with mcp_."""
    plugin_root = tmp_path / "plugins" / "demo"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        '{"name":"demo_plugin","tools_module":"tools.py","skills":[]}\n',
        encoding="utf-8",
    )
    (plugin_root / "tools.py").write_text(
        "from agno.tools import Toolkit\n"
        "from mindroom.tool_system.declarations import ToolCategory, ToolFileAccess\nfrom mindroom.tool_system.registration import register_tool_with_metadata\n"
        "\n"
        "class DemoTool(Toolkit):\n"
        "    def __init__(self) -> None:\n"
        "        super().__init__(name='demo', tools=[])\n"
        "\n"
        "@register_tool_with_metadata(\n"
        "    name='mcp_custom_plugin',\n"
        "    file_access=ToolFileAccess.NONE,\n"
        "    display_name='Plugin MCP Custom',\n"
        "    description='Not an MCP server',\n"
        "    category=ToolCategory.DEVELOPMENT,\n"
        ")\n"
        "def demo_plugin_tools():\n"
        "    return DemoTool\n",
        encoding="utf-8",
    )

    config = Config.validate_with_runtime(
        {
            "models": {"default": {"provider": "openai", "id": "test-model"}},
            "plugins": ["./plugins/demo"],
            "agents": {
                "code": {
                    "display_name": "Code",
                    "role": "Write code",
                    "worker_scope": "user",
                    "tools": ["mcp_custom_plugin"],
                },
            },
        },
        _runtime_paths(tmp_path),
    )

    assert "mcp_custom_plugin" in config.resolve_agent("code").tools.tool_names


def test_mcp_tool_registry_returns_empty_toolkit_without_bound_manager(tmp_path: Path) -> None:
    """Direct agent creation paths should not crash when no orchestrator-bound MCP manager exists."""
    config = _config(tmp_path)

    toolkit = get_tool_by_name("mcp_demo", _runtime_paths(tmp_path), worker_target=None, tools=_tools(config, tmp_path))

    assert toolkit.name == "mcp_demo"
    assert toolkit.functions == {}


def test_non_oauth_mcp_toolkit_builds_for_private_per_user_worker_target(tmp_path: Path) -> None:
    """Tool construction accepts isolating worker targets for non-OAuth MCP tools."""
    config = _config(tmp_path)
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
    worker_target = resolve_worker_target("user_agent", "code", identity)

    toolkit = get_tool_by_name(
        "mcp_demo",
        _runtime_paths(tmp_path),
        worker_target=worker_target,
        tools=_tools(config, tmp_path),
    )

    assert toolkit.name == "mcp_demo"


def _bind_failed_manager(tmp_path: Path, server_config: MCPServerConfig) -> None:
    manager = MCPServerManager(_runtime_paths(tmp_path))
    state = MCPServerState(server_id="demo", config=server_config)
    state.last_error = MCPTimeoutError("demo", "MCP startup timed out after 60.0 seconds")
    manager._states["demo"] = state
    bind_mcp_server_manager(manager)


def test_mcp_tool_registry_degrades_when_optional_server_unavailable(tmp_path: Path) -> None:
    """A failed optional MCP server yields a toolkit without functions instead of an error."""
    config = _config(tmp_path)
    _bind_failed_manager(tmp_path, config.mcp_servers["demo"])

    toolkit = get_tool_by_name("mcp_demo", _runtime_paths(tmp_path), worker_target=None, tools=_tools(config, tmp_path))

    assert toolkit.name == "mcp_demo"
    assert toolkit.functions == {}


def test_mcp_tool_registry_fails_when_required_server_unavailable(tmp_path: Path) -> None:
    """A failed required MCP server keeps the old hard-fail toolkit behavior."""
    config = Config.validate_with_runtime(
        {
            "models": {"default": {"provider": "openai", "id": "test-model"}},
            "mcp_servers": {
                "demo": {
                    "transport": "stdio",
                    "command": "npx",
                    "required": True,
                },
            },
            "agents": {
                "code": {
                    "display_name": "Code",
                    "role": "Write code",
                    "tools": ["mcp_demo"],
                },
            },
        },
        _runtime_paths(tmp_path),
    )
    _bind_failed_manager(tmp_path, config.mcp_servers["demo"])

    with pytest.raises(MCPTimeoutError, match="startup timed out"):
        get_tool_by_name("mcp_demo", _runtime_paths(tmp_path), worker_target=None, tools=_tools(config, tmp_path))


def test_non_oauth_mcp_toolkit_declares_constructor_managed_init_args(tmp_path: Path) -> None:
    """Non-OAuth MCP tools still expose the shared MCP toolkit constructor contract."""
    assert _tools(_config(tmp_path), tmp_path).get("mcp_demo").managed_init_args == (
        ToolManagedInitArg.RUNTIME_PATHS,
        ToolManagedInitArg.CREDENTIALS_MANAGER,
        ToolManagedInitArg.WORKER_TARGET,
    )


def test_non_oauth_mcp_tool_names_are_supported_on_isolating_scope(tmp_path: Path) -> None:
    """Non-OAuth MCP registry tools are scope-agnostic; calls always use the shared server session."""
    assert _tools(_config(tmp_path), tmp_path).mcp_server_id("mcp_demo") == "demo"
    assert supports_tool_name_for_worker_scope("mcp_demo", "user") is True
    assert supports_tool_name_for_worker_scope("mcp_demo", "user_agent") is True


def test_oauth_mcp_tool_names_are_supported_on_isolating_scope(tmp_path: Path) -> None:
    """OAuth-backed MCP registry tools can use requester-scoped credentials."""
    assert _tools(_oauth_config(tmp_path), tmp_path).mcp_server_id("mcp_demo") == "demo"
    assert supports_tool_name_for_worker_scope("mcp_demo", "user") is True


def test_oauth_mcp_toolkit_instantiates_with_managed_runtime_args(tmp_path: Path) -> None:
    """OAuth-backed dynamic MCP tools declare managed init args as runtime enum values."""
    config = _oauth_config(tmp_path)

    toolkit = get_tool_by_name("mcp_demo", _runtime_paths(tmp_path), worker_target=None, tools=_tools(config, tmp_path))

    assert toolkit.name == "mcp_demo"
    assert set(toolkit.functions) == {
        "demo_connection_status",
        "demo_list_tools",
        "demo_call_tool",
    }


def test_validate_mcp_agent_overrides_rejects_overlapping_filters_with_exact_message() -> None:
    """Preserve the public per-agent MCP filter-overlap error."""
    with pytest.raises(ValueError, match="include_tools and exclude_tools overlap") as exc_info:
        validate_mcp_agent_overrides(
            "mcp_demo",
            {
                "include_tools": ["ping", "echo"],
                "exclude_tools": ["echo", "ping"],
            },
        )

    assert (
        str(exc_info.value)
        == "Invalid per-agent override for 'mcp_demo': include_tools and exclude_tools overlap: echo, ping"
    )


def test_validate_mcp_agent_overrides_rejects_invalid_call_timeout_with_exact_message() -> None:
    """Keep per-agent timeout validation behavior independent from filter validation."""
    with pytest.raises(ValueError, match="expected a number greater than 0") as exc_info:
        validate_mcp_agent_overrides("mcp_demo", {"call_timeout_seconds": 0})

    assert (
        str(exc_info.value)
        == "Invalid per-agent override for 'mcp_demo.call_timeout_seconds': expected a number greater than 0"
    )
