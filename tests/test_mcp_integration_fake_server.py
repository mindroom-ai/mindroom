"""Integration test for the real stdio MCP client path."""

from __future__ import annotations

import sys
import textwrap
from typing import TYPE_CHECKING

import pytest

from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.mcp.manager import MCPServerManager
from mindroom.mcp.registry import sync_mcp_tool_registry
from mindroom.mcp.toolkit import bind_mcp_server_manager
from mindroom.tool_system.metadata import get_tool_by_name

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths


def _runtime_paths(tmp_path: Path) -> RuntimePaths:
    return resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path, process_env={})


@pytest.mark.asyncio
async def test_mcp_fake_stdio_server_end_to_end(tmp_path: Path) -> None:
    """Connect to a real stdio MCP subprocess and call its exported tool."""
    server_script = tmp_path / "fake_mcp_server.py"
    server_script.write_text(
        textwrap.dedent(
            """
            from mcp.server.fastmcp import FastMCP

            server = FastMCP("Fake MCP")

            @server.tool()
            def echo(text: str) -> str:
                return f"echo:{text}"

            if __name__ == "__main__":
                server.run()
            """,
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    runtime_paths = _runtime_paths(tmp_path)
    config = Config.validate_with_runtime(
        {
            "mcp_servers": {
                "echo": {
                    "transport": "stdio",
                    "command": sys.executable,
                    "args": [str(server_script)],
                },
            },
            "agents": {
                "code": {
                    "display_name": "Code",
                    "role": "Write code",
                    "tools": ["mcp_echo"],
                },
            },
        },
        runtime_paths,
    )
    manager = MCPServerManager(runtime_paths)
    bind_mcp_server_manager(manager)
    try:
        changed = await manager.sync_servers(config)
        assert changed == {"echo"}
        sync_mcp_tool_registry(config)
        toolkit = get_tool_by_name("mcp_echo", runtime_paths, worker_target=None)
        assert "echo_echo" in toolkit.async_functions
        result = await toolkit.async_functions["echo_echo"].entrypoint(text="hello")
        assert result.content.startswith("echo:hello")
    finally:
        bind_mcp_server_manager(None)
        sync_mcp_tool_registry(None)
        await manager.shutdown()
