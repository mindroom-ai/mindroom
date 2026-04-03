"""Tests for dynamic MCP tool registry integration."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

import mindroom.tools  # noqa: F401
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.mcp.registry import (
    _MCP_TOOL_NAMES,
    mcp_server_id_from_tool_name,
    mcp_tool_name,
    sync_mcp_tool_registry,
)
from mindroom.mcp.toolkit import bind_mcp_server_manager
from mindroom.tool_system.metadata import _TOOL_REGISTRY, TOOL_METADATA
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


def test_mcp_tool_names_are_shared_only() -> None:
    """Treat all MCP registry tools as shared-only integrations."""
    assert mcp_server_id_from_tool_name("mcp_demo") == "demo"
    assert requires_shared_only_integration_scope("mcp_demo") is True
