"""A collision projection loads its shared tool registry once, not per agent."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import Mock

from mindroom.config.main import Config
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
