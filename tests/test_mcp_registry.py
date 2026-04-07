"""Tests for dynamic MCP tool registry integration."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

import mindroom.tools  # noqa: F401
from mindroom.config.main import Config, ConfigRuntimeValidationError
from mindroom.constants import resolve_runtime_paths
from mindroom.mcp.registry import (
    _MCP_TOOL_NAMES,
    mcp_server_id_from_tool_name,
    mcp_tool_name,
    sync_mcp_tool_registry,
)
from mindroom.mcp.toolkit import bind_mcp_server_manager
from mindroom.tool_system.metadata import _TOOL_REGISTRY, TOOL_METADATA, get_tool_by_name
from mindroom.tool_system.worker_routing import requires_shared_only_integration_scope

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from mindroom.constants import RuntimePaths

_BASE_TOOL_REGISTRY = {
    tool_name: factory for tool_name, factory in _TOOL_REGISTRY.items() if not tool_name.startswith("mcp_")
}
_BASE_TOOL_METADATA = {
    tool_name: metadata for tool_name, metadata in TOOL_METADATA.items() if not tool_name.startswith("mcp_")
}


@pytest.fixture(autouse=True)
def _restore_tool_registry() -> Iterator[None]:
    _MCP_TOOL_NAMES.clear()
    _TOOL_REGISTRY.clear()
    _TOOL_REGISTRY.update(_BASE_TOOL_REGISTRY)
    TOOL_METADATA.clear()
    TOOL_METADATA.update(_BASE_TOOL_METADATA)
    bind_mcp_server_manager(None)
    sync_mcp_tool_registry(None)
    yield
    _MCP_TOOL_NAMES.clear()
    _TOOL_REGISTRY.clear()
    _TOOL_REGISTRY.update(_BASE_TOOL_REGISTRY)
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


def test_sync_mcp_tool_registry_registers_dynamic_tool(tmp_path: Path) -> None:
    """Register a dynamic tool entry for each enabled MCP server."""
    config = _config(tmp_path)
    sync_mcp_tool_registry(config)
    tool_name = mcp_tool_name("demo")
    assert tool_name in TOOL_METADATA
    assert tool_name in _TOOL_REGISTRY
    assert TOOL_METADATA[tool_name].agent_override_fields is not None


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
    assert "mcp_demo" not in _TOOL_REGISTRY


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
    assert "mcp_demo" not in _TOOL_REGISTRY


def test_sync_mcp_tool_registry_rejects_name_collisions(tmp_path: Path) -> None:
    """Fail fast instead of silently overwriting an existing built-in tool entry."""
    _TOOL_REGISTRY["mcp_demo"] = _TOOL_REGISTRY["shell"]
    TOOL_METADATA["mcp_demo"] = TOOL_METADATA["shell"]
    with pytest.raises(ValueError, match="conflicts with an existing registered tool"):
        sync_mcp_tool_registry(_config(tmp_path))


def test_sync_mcp_tool_registry_keeps_non_mcp_prefixed_plugin_tools() -> None:
    """Do not unregister unrelated tools just because their names start with mcp_."""
    _TOOL_REGISTRY["mcp_custom_plugin"] = _TOOL_REGISTRY["shell"]
    TOOL_METADATA["mcp_custom_plugin"] = replace(TOOL_METADATA["shell"], name="mcp_custom_plugin")

    sync_mcp_tool_registry(None)

    assert "mcp_custom_plugin" in TOOL_METADATA
    assert "mcp_custom_plugin" in _TOOL_REGISTRY


def test_mcp_server_id_from_tool_name_ignores_non_mcp_prefixed_plugin_tools() -> None:
    """Only registry-owned MCP tools should be classified as MCP integrations."""
    _TOOL_REGISTRY["mcp_custom_plugin"] = _TOOL_REGISTRY["shell"]
    TOOL_METADATA["mcp_custom_plugin"] = replace(TOOL_METADATA["shell"], name="mcp_custom_plugin")

    assert mcp_server_id_from_tool_name("mcp_custom_plugin") is None
    assert requires_shared_only_integration_scope("mcp_custom_plugin") is False


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

    assert "mcp_custom_plugin" in config.get_agent_tools("code")


def test_mcp_tool_registry_returns_empty_toolkit_without_bound_manager(tmp_path: Path) -> None:
    """Direct agent creation paths should not crash when no orchestrator-bound MCP manager exists."""
    config = _config(tmp_path)
    sync_mcp_tool_registry(config)

    toolkit = get_tool_by_name("mcp_demo", _runtime_paths(tmp_path), worker_target=None)

    assert toolkit.name == "mcp_demo"
    assert toolkit.async_functions == {}


def test_mcp_tool_names_are_shared_only(tmp_path: Path) -> None:
    """Treat all MCP registry tools as shared-only integrations."""
    sync_mcp_tool_registry(_config(tmp_path))

    assert mcp_server_id_from_tool_name("mcp_demo") == "demo"
    assert requires_shared_only_integration_scope("mcp_demo") is True
