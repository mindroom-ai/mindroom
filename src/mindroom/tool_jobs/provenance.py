"""Exact MCP bridge identity shared by admission and current execution checks."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mindroom.mcp.config import resolved_mcp_tool_prefix
from mindroom.mcp.toolkit import MindRoomMCPToolkit

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agno.tools.function import Function


def function_provenance(function: Function, arguments: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Read exact MCP bridge identity without importing or warming tools."""
    toolkit = function.source_toolkit
    if not isinstance(toolkit, MindRoomMCPToolkit):
        return {}
    remote = (
        next(
            (tool.remote_name for tool in toolkit.catalog.tools if tool.function_name == function.name),
            None,
        )
        if toolkit.catalog is not None
        else None
    )
    if toolkit.server_config is not None:
        prefix = resolved_mcp_tool_prefix(toolkit.server_id, toolkit.server_config)
        if function.name == f"{prefix}_call_tool":
            remote = (arguments or {}).get("tool_name")
    return {"mcp_server_id": toolkit.server_id, "mcp_tool_name": remote}
