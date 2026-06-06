"""Tests for MCP config models and config integration."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom.config.main import Config, ConfigRuntimeValidationError
from mindroom.constants import resolve_runtime_paths
from mindroom.mcp.config import MCPServerConfig, resolved_mcp_tool_prefix

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths


def _runtime_paths(tmp_path: Path) -> RuntimePaths:
    return resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path, process_env={})


def test_mcp_server_config_validates_stdio_shape() -> None:
    """Accept stdio server configs with explicit command and args."""
    config = MCPServerConfig(transport="stdio", command="npx", args=["-y", "demo"])
    assert config.transport == "stdio"
    assert config.command == "npx"


def test_mcp_server_config_rejects_invalid_transport_mix() -> None:
    """Reject fields that do not belong to the selected transport."""
    with pytest.raises(ValueError, match="stdio MCP servers do not allow url"):
        MCPServerConfig(transport="stdio", command="npx", url="http://localhost:8000")

    with pytest.raises(ValueError, match="streamable-http MCP servers do not allow command"):
        MCPServerConfig(transport="streamable-http", url="http://localhost:8000/mcp", command="npx")


def test_mcp_server_config_rejects_overlapping_filters() -> None:
    """Reject overlapping include and exclude tool filters."""
    with pytest.raises(ValueError, match="MCP include_tools and exclude_tools overlap") as exc_info:
        MCPServerConfig(
            transport="stdio",
            command="npx",
            include_tools=["ping", "echo"],
            exclude_tools=["echo", "ping"],
        )
    assert str(exc_info.value.errors()[0]["ctx"]["error"]) == "MCP include_tools and exclude_tools overlap: echo, ping"


def test_mcp_server_config_normalizes_tool_filters() -> None:
    """Strip whitespace-only entries and trim stored filter names."""
    config = MCPServerConfig(
        transport="stdio",
        command="npx",
        include_tools=[" echo ", "  ", "ping"],
        exclude_tools=[" pong "],
    )
    assert config.include_tools == ["echo", "ping"]
    assert config.exclude_tools == ["pong"]


def test_mcp_server_config_accepts_oauth_auth_for_remote_servers() -> None:
    """Parse OAuth settings for requester-scoped remote MCP servers."""
    config = MCPServerConfig(
        transport="streamable-http",
        url="https://mcp.example.test/mcp",
        tool_prefix="example",
        auth={
            "type": "oauth",
            "provider_id": "mcp_example",
            "display_name": "Example MCP",
            "resource": "https://mcp.example.test/mcp",
            "discovery": "manual",
            "authorization_url": "https://auth.example.test/authorize",
            "token_url": "https://auth.example.test/token",
            "token_endpoint_auth_method": "none",
            "scopes": [],
            "extra_auth_params": {"audience": "example"},
            "extra_token_params": {"resource": "https://mcp.example.test/mcp"},
        },
    )

    assert config.auth is not None
    assert config.auth.type == "oauth"
    assert config.auth.provider_id == "mcp_example"
    assert config.auth.scopes == []
    assert config.auth.extra_token_params == {"resource": "https://mcp.example.test/mcp"}


def test_mcp_server_config_rejects_oauth_auth_for_stdio_servers() -> None:
    """OAuth-backed MCP auth is only meaningful for remote HTTP transports."""
    with pytest.raises(ValueError, match="OAuth-backed MCP servers require remote HTTP transport"):
        MCPServerConfig(
            transport="stdio",
            command="npx",
            auth={
                "type": "oauth",
                "authorization_url": "https://auth.example.test/authorize",
                "token_url": "https://auth.example.test/token",
            },
        )


def test_resolved_mcp_tool_prefix_uses_server_id_when_missing() -> None:
    """Default the model-visible tool prefix to the server id."""
    config = MCPServerConfig(transport="stdio", command="npx")
    assert resolved_mcp_tool_prefix("chrome_devtools", config) == "chrome_devtools"


def test_config_accepts_top_level_mcp_servers(tmp_path: Path) -> None:
    """Parse top-level MCP server config and expose the dynamic tool name."""
    runtime_paths = _runtime_paths(tmp_path)
    config = Config.validate_with_runtime(
        {
            "mcp_servers": {
                "chrome_devtools": {
                    "transport": "stdio",
                    "command": "npx",
                    "args": ["-y", "chrome-devtools-mcp@latest"],
                    "tool_prefix": "chrome",
                },
            },
            "agents": {
                "code": {
                    "display_name": "Code",
                    "role": "Write code",
                    "tools": ["mcp_chrome_devtools"],
                },
            },
        },
        runtime_paths,
    )
    assert "chrome_devtools" in config.mcp_servers
    assert "mcp_chrome_devtools" in config.get_agent_available_tools("code")


def test_config_rejects_mcp_stdio_cwd_escape(tmp_path: Path) -> None:
    """MCP stdio cwd should stay under runtime-owned roots."""
    runtime_paths = _runtime_paths(tmp_path)
    with pytest.raises(ConfigRuntimeValidationError, match=r"mcp_servers\.demo\.cwd must stay"):
        Config.validate_with_runtime(
            {
                "mcp_servers": {
                    "demo": {
                        "transport": "stdio",
                        "command": "npx",
                        "cwd": str(tmp_path.parent / "outside"),
                    },
                },
                "agents": {},
            },
            runtime_paths,
        )


def test_config_rejects_knowledge_path_escape(tmp_path: Path) -> None:
    """Knowledge base roots should stay under runtime-owned roots."""
    runtime_paths = _runtime_paths(tmp_path)
    with pytest.raises(ConfigRuntimeValidationError, match=r"knowledge_bases\.docs\.path must stay"):
        Config.validate_with_runtime(
            {
                "knowledge_bases": {"docs": {"path": str(tmp_path.parent / "outside-docs")}},
                "agents": {},
            },
            runtime_paths,
        )


def test_config_rejects_mcp_tools_on_user_scoped_agents(tmp_path: Path) -> None:
    """Keep MCP tools restricted to shared-scope integrations."""
    runtime_paths = _runtime_paths(tmp_path)
    with pytest.raises(ValueError, match="Shared-only integrations"):
        Config.validate_with_runtime(
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
                        "worker_scope": "user",
                        "tools": ["mcp_demo"],
                    },
                },
            },
            runtime_paths,
        )


def test_config_allows_oauth_mcp_tools_on_user_scoped_agents(tmp_path: Path) -> None:
    """Allow requester-scoped OAuth MCP tools on isolated agents."""
    runtime_paths = _runtime_paths(tmp_path)
    config = Config.validate_with_runtime(
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
        runtime_paths,
    )

    assert "mcp_demo" in config.get_agent_available_tools("code")


def test_config_tracks_initial_deferred_mcp_dependencies_for_agents_and_teams(tmp_path: Path) -> None:
    """Treat initial deferred MCP tools as dependencies for restart planning."""
    config = Config.validate_with_runtime(
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
                    "tools": [{"mcp_demo": {"defer": True, "initial": True}}],
                },
                "plain": {
                    "display_name": "Plain",
                    "role": "No MCP",
                },
            },
            "teams": {
                "dev_team": {
                    "display_name": "Dev Team",
                    "role": "Collaborate",
                    "agents": ["code"],
                },
            },
        },
        _runtime_paths(tmp_path),
    )

    assert config.get_entities_referencing_tools({"mcp_demo"}) == {"code", "dev_team"}


def test_config_does_not_treat_deferred_only_mcp_tools_as_hard_dependencies(tmp_path: Path) -> None:
    """Deferred-only tools should stay optional for restart and startup dependency tracking."""
    config = Config.validate_with_runtime(
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
                    "tools": [{"mcp_demo": {"defer": True}}],
                },
            },
            "teams": {
                "dev_team": {
                    "display_name": "Dev Team",
                    "role": "Collaborate",
                    "agents": ["code"],
                },
            },
        },
        _runtime_paths(tmp_path),
    )

    assert config.get_entities_referencing_tools({"mcp_demo"}) == set()


def test_config_rejects_invalid_mcp_assignment_overrides(tmp_path: Path) -> None:
    """Mirror server-level override validation for per-assignment MCP config."""
    runtime_paths = _runtime_paths(tmp_path)

    with pytest.raises(ConfigRuntimeValidationError, match="include_tools and exclude_tools overlap"):
        Config.validate_with_runtime(
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
                        "tools": [
                            {
                                "mcp_demo": {
                                    "include_tools": ["echo"],
                                    "exclude_tools": ["echo"],
                                },
                            },
                        ],
                    },
                },
            },
            runtime_paths,
        )

    with pytest.raises(ConfigRuntimeValidationError, match="greater than 0"):
        Config.validate_with_runtime(
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
                        "tools": [
                            {
                                "mcp_demo": {
                                    "call_timeout_seconds": 0,
                                },
                            },
                        ],
                    },
                },
            },
            runtime_paths,
        )
