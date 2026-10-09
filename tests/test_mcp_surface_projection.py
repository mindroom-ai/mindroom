"""Collision projection loads shared state once and builds only toolkits that can collide."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import Mock

from mindroom.config.main import Config
from mindroom.mcp import surface_projection
from mindroom.mcp.surface_projection import MCPFunctionSurfaceContext, function_collision_reports
from mindroom.mcp.types import MCPDiscoveredTool, MCPServerCatalog, MCPServerState
from mindroom.tool_system import plugins
from tests.conftest import orchestrator_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_collision_projection_loads_plugins_once_for_all_agents(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Load shared plugins once while retaining every agent's collision report."""
    runtime_paths = orchestrator_runtime_paths(tmp_path, config_path=tmp_path / "config.yaml")
    config = Config.validate_with_runtime(
        {
            "defaults": {"tools": []},
            "mcp_servers": {"demo": {"transport": "stdio", "command": "test-server"}},
            "agents": {
                name: {"display_name": name, "tools": ["shell", "mcp_demo"]} for name in ("first", "second", "third")
            },
        },
        runtime_paths=runtime_paths,
    )
    catalog = MCPServerCatalog(
        server_id="demo",
        tool_name="mcp_demo",
        tool_prefix="demo",
        tools=(MCPDiscoveredTool("shell", "run_shell_command", None, {}, None),),
        instructions=None,
        catalog_hash="test",
    )
    context = MCPFunctionSurfaceContext(
        runtime_paths,
        config,
        {"demo": MCPServerState("demo", config.mcp_servers["demo"], catalog=catalog)},
        (),
    )
    load_plugins = Mock(wraps=plugins.load_plugins)
    monkeypatch.setattr(plugins, "load_plugins", load_plugins)
    reports = function_collision_reports(context)
    assert {report.agent_name for report in reports} == {"first", "second", "third"}
    assert all(report.function_name_collisions[0][0] == "run_shell_command" for report in reports)
    load_plugins.assert_called_once()


def test_collision_projection_builds_local_tools_only_for_agents_with_mcp_tools(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Agents without MCP tools cannot collide, so validation must not build (and auto-install) their tools."""
    runtime_paths = orchestrator_runtime_paths(tmp_path, config_path=tmp_path / "config.yaml")
    config = Config.validate_with_runtime(
        {
            "defaults": {"tools": []},
            "mcp_servers": {"demo": {"transport": "stdio", "command": "test-server"}},
            "agents": {
                "with_mcp": {"display_name": "With MCP", "tools": ["shell", "mcp_demo"]},
                "without_mcp": {"display_name": "Without MCP", "tools": ["browser"]},
            },
        },
        runtime_paths=runtime_paths,
    )
    catalog = MCPServerCatalog(
        server_id="demo",
        tool_name="mcp_demo",
        tool_prefix="demo",
        tools=(MCPDiscoveredTool("shell", "run_shell_command", None, {}, None),),
        instructions=None,
        catalog_hash="test",
    )
    context = MCPFunctionSurfaceContext(
        runtime_paths,
        config,
        {"demo": MCPServerState("demo", config.mcp_servers["demo"], catalog=catalog)},
        (),
    )
    built_tool_names: list[str] = []
    real_get_tool_by_name = surface_projection.get_tool_by_name

    def recording_get_tool_by_name(tool_name: str, *args: object, **kwargs: object) -> object:
        built_tool_names.append(tool_name)
        if tool_name == "browser":
            msg = "browser must not be built during validation"
            raise ImportError(msg)
        return real_get_tool_by_name(tool_name, *args, **kwargs)

    monkeypatch.setattr(surface_projection, "get_tool_by_name", recording_get_tool_by_name)
    reports = function_collision_reports(context)
    assert "browser" not in built_tool_names
    assert "shell" in built_tool_names
    assert [(report.agent_name, report.function_name_collisions[0][0]) for report in reports] == [
        ("with_mcp", "run_shell_command"),
    ]
