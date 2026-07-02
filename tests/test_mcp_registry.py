"""Tests for dynamic MCP tool registry integration."""

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
    _MCP_TOOL_NAMES,
    mcp_server_id_from_tool_name,
    mcp_tool_name,
    resolved_mcp_tool_state,
    sync_mcp_tool_registry,
    validate_mcp_agent_overrides,
)
from mindroom.mcp.toolkit import bind_mcp_server_manager
from mindroom.mcp.types import MCPServerState
from mindroom.tool_system.metadata import (
    TOOL_METADATA,
    TOOL_REGISTRY,
    SetupType,
    ToolManagedInitArg,
    ToolStatus,
    get_tool_by_name,
)
from mindroom.tool_system.worker_routing import (
    ToolExecutionIdentity,
    resolve_worker_target,
    supports_tool_name_for_worker_scope,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.mcp.config import MCPServerConfig

_BASE_TOOL_REGISTRY = {
    tool_name: factory for tool_name, factory in TOOL_REGISTRY.items() if not tool_name.startswith("mcp_")
}
_BASE_TOOL_METADATA = {
    tool_name: metadata for tool_name, metadata in TOOL_METADATA.items() if not tool_name.startswith("mcp_")
}


@pytest.fixture(autouse=True)
def _restore_tool_registry() -> Iterator[None]:
    _MCP_TOOL_NAMES.clear()
    TOOL_REGISTRY.clear()
    TOOL_REGISTRY.update(_BASE_TOOL_REGISTRY)
    TOOL_METADATA.clear()
    TOOL_METADATA.update(_BASE_TOOL_METADATA)
    bind_mcp_server_manager(None)
    sync_mcp_tool_registry(None)
    yield
    _MCP_TOOL_NAMES.clear()
    TOOL_REGISTRY.clear()
    TOOL_REGISTRY.update(_BASE_TOOL_REGISTRY)
    TOOL_METADATA.clear()
    TOOL_METADATA.update(_BASE_TOOL_METADATA)
    bind_mcp_server_manager(None)
    sync_mcp_tool_registry(None)


def _runtime_paths(tmp_path: Path) -> RuntimePaths:
    return resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path, process_env={})


def _config(tmp_path: Path) -> Config:
    return Config.validate_with_runtime(
        {
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


def test_sync_mcp_tool_registry_registers_dynamic_tool(tmp_path: Path) -> None:
    """Register a dynamic tool entry for each enabled MCP server."""
    config = _config(tmp_path)
    sync_mcp_tool_registry(config)
    tool_name = mcp_tool_name("demo")
    assert tool_name in TOOL_METADATA
    assert tool_name in TOOL_REGISTRY
    assert TOOL_METADATA[tool_name].agent_override_fields is not None


def test_resolved_mcp_tool_state_ignores_unsynced_bound_manager(tmp_path: Path) -> None:
    """Metadata resolution should stay best-effort when a manager is bound but has no catalog yet."""

    class FakeManager:
        def has_server(self, _server_id: str) -> bool:
            return False

        def get_catalog(self, server_id: str) -> object:
            msg = f"Unknown MCP server '{server_id}'"
            raise KeyError(msg)

    config = _config(tmp_path)
    bind_mcp_server_manager(FakeManager())

    registry, metadata = resolved_mcp_tool_state(config)

    assert "mcp_demo" in registry
    assert "mcp_demo" in metadata
    assert metadata["mcp_demo"].function_names == ()


def test_sync_mcp_tool_registry_is_idempotent(tmp_path: Path) -> None:
    """Keep registry sync stable when the same config is applied twice."""
    config = _config(tmp_path)
    sync_mcp_tool_registry(config)
    sync_mcp_tool_registry(config)
    assert [name for name in TOOL_METADATA if name == "mcp_demo"] == ["mcp_demo"]


def test_sync_mcp_tool_registry_removes_deleted_servers(tmp_path: Path) -> None:
    """Remove registry entries when a configured MCP server disappears."""
    config = _config(tmp_path)
    sync_mcp_tool_registry(config)
    sync_mcp_tool_registry(
        Config.validate_with_runtime(
            {
                "agents": {
                    "code": {
                        "display_name": "Code",
                        "role": "Write code",
                    },
                },
            },
            _runtime_paths(tmp_path),
        ),
    )
    assert "mcp_demo" not in TOOL_METADATA
    assert "mcp_demo" not in TOOL_REGISTRY


def test_sync_mcp_tool_registry_marks_oauth_mcp_tools(tmp_path: Path) -> None:
    """Expose OAuth-backed MCP servers as OAuth-configured bridge tools."""
    sync_mcp_tool_registry(_oauth_config(tmp_path))

    metadata = TOOL_METADATA["mcp_demo"]
    assert metadata.setup_type is SetupType.OAUTH
    assert metadata.status is ToolStatus.REQUIRES_CONFIG
    assert metadata.auth_provider == "mcp_demo"
    assert metadata.function_names == (
        "demo_connection_status",
        "demo_list_tools",
        "demo_call_tool",
    )


def test_sync_mcp_tool_registry_removes_untracked_dynamic_entries(tmp_path: Path) -> None:
    """Remove leaked dynamic MCP entries even if the helper name set is stale."""
    config = _config(tmp_path)
    sync_mcp_tool_registry(config)
    _MCP_TOOL_NAMES.clear()
    sync_mcp_tool_registry(
        Config.validate_with_runtime(
            {
                "agents": {
                    "code": {
                        "display_name": "Code",
                        "role": "Write code",
                    },
                },
            },
            _runtime_paths(tmp_path),
        ),
    )
    assert "mcp_demo" not in TOOL_METADATA
    assert "mcp_demo" not in TOOL_REGISTRY


def test_sync_mcp_tool_registry_rejects_name_collisions(tmp_path: Path) -> None:
    """Fail fast instead of silently overwriting an existing built-in tool entry."""
    TOOL_REGISTRY["mcp_demo"] = TOOL_REGISTRY["shell"]
    TOOL_METADATA["mcp_demo"] = TOOL_METADATA["shell"]
    with pytest.raises(ValueError, match="conflicts with an existing registered tool"):
        sync_mcp_tool_registry(_config(tmp_path))


def test_sync_mcp_tool_registry_keeps_non_mcp_prefixed_plugin_tools() -> None:
    """Do not unregister unrelated tools just because their names start with mcp_."""
    TOOL_REGISTRY["mcp_custom_plugin"] = TOOL_REGISTRY["shell"]
    TOOL_METADATA["mcp_custom_plugin"] = replace(TOOL_METADATA["shell"], name="mcp_custom_plugin")

    sync_mcp_tool_registry(None)

    assert "mcp_custom_plugin" in TOOL_METADATA
    assert "mcp_custom_plugin" in TOOL_REGISTRY


def test_mcp_server_id_from_tool_name_ignores_non_mcp_prefixed_plugin_tools() -> None:
    """Only registry-owned MCP tools should be classified as MCP integrations."""
    TOOL_REGISTRY["mcp_custom_plugin"] = TOOL_REGISTRY["shell"]
    TOOL_METADATA["mcp_custom_plugin"] = replace(TOOL_METADATA["shell"], name="mcp_custom_plugin")

    assert mcp_server_id_from_tool_name("mcp_custom_plugin") is None
    assert supports_tool_name_for_worker_scope("mcp_custom_plugin", "user") is True


def test_config_validation_rejects_runtime_mcp_name_collisions(tmp_path: Path) -> None:
    """Reject MCP tool name collisions during config validation, before runtime sync."""
    plugin_root = tmp_path / "plugins" / "demo"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        '{"name":"demo_plugin","tools_module":"tools.py","skills":[]}\n',
        encoding="utf-8",
    )
    (plugin_root / "tools.py").write_text(
        "from agno.tools import Toolkit\n"
        "from mindroom.tool_system.metadata import ToolCategory, register_tool_with_metadata\n"
        "\n"
        "class DemoTool(Toolkit):\n"
        "    def __init__(self) -> None:\n"
        "        super().__init__(name='demo', tools=[])\n"
        "\n"
        "@register_tool_with_metadata(\n"
        "    name='mcp_demo',\n"
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
        "from mindroom.tool_system.metadata import ToolCategory, register_tool_with_metadata\n"
        "\n"
        "class DemoTool(Toolkit):\n"
        "    def __init__(self) -> None:\n"
        "        super().__init__(name='demo', tools=[])\n"
        "\n"
        "@register_tool_with_metadata(\n"
        "    name='mcp_custom_plugin',\n"
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

    assert "mcp_custom_plugin" in config.resolve_entity("code").available_tools


def test_mcp_tool_registry_returns_empty_toolkit_without_bound_manager(tmp_path: Path) -> None:
    """Direct agent creation paths should not crash when no orchestrator-bound MCP manager exists."""
    config = _config(tmp_path)
    sync_mcp_tool_registry(config)

    toolkit = get_tool_by_name("mcp_demo", _runtime_paths(tmp_path), worker_target=None)

    assert toolkit.name == "mcp_demo"
    assert toolkit.async_functions == {}


def test_non_oauth_mcp_toolkit_builds_for_private_per_user_worker_target(tmp_path: Path) -> None:
    """Tool construction accepts isolating worker targets for non-OAuth MCP tools."""
    config = _config(tmp_path)
    sync_mcp_tool_registry(config)
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

    toolkit = get_tool_by_name("mcp_demo", _runtime_paths(tmp_path), worker_target=worker_target)

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
    sync_mcp_tool_registry(config)
    _bind_failed_manager(tmp_path, config.mcp_servers["demo"])

    toolkit = get_tool_by_name("mcp_demo", _runtime_paths(tmp_path), worker_target=None)

    assert toolkit.name == "mcp_demo"
    assert toolkit.async_functions == {}


def test_mcp_tool_registry_fails_when_required_server_unavailable(tmp_path: Path) -> None:
    """A failed required MCP server keeps the old hard-fail toolkit behavior."""
    config = Config.validate_with_runtime(
        {
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
    sync_mcp_tool_registry(config)
    _bind_failed_manager(tmp_path, config.mcp_servers["demo"])

    with pytest.raises(MCPTimeoutError, match="startup timed out"):
        get_tool_by_name("mcp_demo", _runtime_paths(tmp_path), worker_target=None)


def test_non_oauth_mcp_toolkit_declares_constructor_managed_init_args(tmp_path: Path) -> None:
    """Non-OAuth MCP tools still expose the shared MCP toolkit constructor contract."""
    sync_mcp_tool_registry(_config(tmp_path))

    assert TOOL_METADATA["mcp_demo"].managed_init_args == (
        ToolManagedInitArg.RUNTIME_PATHS,
        ToolManagedInitArg.CREDENTIALS_MANAGER,
        ToolManagedInitArg.WORKER_TARGET,
    )


def test_non_oauth_mcp_tool_names_are_supported_on_isolating_scope(tmp_path: Path) -> None:
    """Non-OAuth MCP registry tools are scope-agnostic; calls always use the shared server session."""
    sync_mcp_tool_registry(_config(tmp_path))

    assert mcp_server_id_from_tool_name("mcp_demo") == "demo"
    assert supports_tool_name_for_worker_scope("mcp_demo", "user") is True
    assert supports_tool_name_for_worker_scope("mcp_demo", "user_agent") is True


def test_oauth_mcp_tool_names_are_supported_on_isolating_scope(tmp_path: Path) -> None:
    """OAuth-backed MCP registry tools can use requester-scoped credentials."""
    sync_mcp_tool_registry(_oauth_config(tmp_path))

    assert mcp_server_id_from_tool_name("mcp_demo") == "demo"
    assert supports_tool_name_for_worker_scope("mcp_demo", "user") is True


def test_oauth_mcp_toolkit_instantiates_with_managed_runtime_args(tmp_path: Path) -> None:
    """OAuth-backed dynamic MCP tools declare managed init args as runtime enum values."""
    sync_mcp_tool_registry(_oauth_config(tmp_path))

    toolkit = get_tool_by_name("mcp_demo", _runtime_paths(tmp_path), worker_target=None)

    assert toolkit.name == "mcp_demo"
    assert set(toolkit.async_functions) == {
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
