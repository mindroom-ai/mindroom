"""Tests for the MindRoom MCP toolkit wrapper."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from agno.tools.function import ToolResult
from mcp.types import Implementation

from mindroom.mcp.toolkit import MindRoomMCPToolkit
from mindroom.mcp.types import MCPDiscoveredTool, MCPServerCatalog


class _DummyManager:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, object], float | None]] = []

    async def call_tool(
        self,
        server_id: str,
        remote_tool_name: str,
        arguments: dict[str, object],
        *,
        timeout_seconds: float | None = None,
    ) -> ToolResult:
        """Record the call and return a fixed tool result."""
        self.calls.append((server_id, remote_tool_name, arguments, timeout_seconds))
        return ToolResult(content="ok")


def _catalog(*tools: MCPDiscoveredTool) -> MCPServerCatalog:
    return MCPServerCatalog(
        server_id="demo",
        tool_name="mcp_demo",
        tool_prefix="demo",
        tools=tools,
        server_info=Implementation(name="demo", version="1.0"),
        instructions=None,
        catalog_hash="hash",
        discovered_at=datetime.now(UTC),
    )


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
    result = await toolkit.async_functions["demo_echo"].entrypoint(text="hello")
    assert result.content == "ok"
    assert manager.calls == [("demo", "echo", {"text": "hello"}, 15.0)]


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
    assert list(toolkit.async_functions) == ["demo_ping"]


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
