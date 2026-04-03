"""Tests for MCP transport helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.constants import resolve_runtime_paths
from mindroom.mcp.config import MCPServerConfig
from mindroom.mcp.transports import (
    build_stdio_server_parameters,
    build_transport_handle,
    interpolate_mcp_env,
    interpolate_mcp_headers,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths


def _runtime_paths(tmp_path: Path) -> RuntimePaths:
    return resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env={"API_TOKEN": "secret-token", "EXTRA_ARG": "value"},
    )


def test_interpolate_mcp_env_and_headers(tmp_path: Path) -> None:
    """Resolve environment placeholders in env vars and HTTP headers."""
    runtime_paths = _runtime_paths(tmp_path)
    assert interpolate_mcp_env({"TOKEN": "${API_TOKEN}"}, runtime_paths) == {"TOKEN": "secret-token"}
    assert interpolate_mcp_headers({"Authorization": "Bearer ${API_TOKEN}"}, runtime_paths) == {
        "Authorization": "Bearer secret-token",
    }


def test_build_stdio_server_parameters_interpolates_env(tmp_path: Path) -> None:
    """Interpolate stdio env vars while leaving argv entries unchanged."""
    runtime_paths = _runtime_paths(tmp_path)
    params = build_stdio_server_parameters(
        MCPServerConfig(
            transport="stdio",
            command="npx",
            args=["-y", "${EXTRA_ARG}"],
            env={"TOKEN": "${API_TOKEN}"},
        ),
        runtime_paths,
    )
    assert params.command == "npx"
    assert params.args == ["-y", "${EXTRA_ARG}"]
    assert params.env is not None
    assert params.env["TOKEN"] == runtime_paths.env_value("API_TOKEN")


def test_build_transport_handle_returns_expected_transport(tmp_path: Path) -> None:
    """Return the deferred opener matching the configured transport."""
    runtime_paths = _runtime_paths(tmp_path)
    assert (
        build_transport_handle(
            "demo",
            MCPServerConfig(transport="stdio", command="npx"),
            runtime_paths,
        ).transport
        == "stdio"
    )
    assert (
        build_transport_handle(
            "demo",
            MCPServerConfig(transport="sse", url="http://localhost:8000/sse"),
            runtime_paths,
        ).transport
        == "sse"
    )
