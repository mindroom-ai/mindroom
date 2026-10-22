"""Exact MCP bridge identity shared by admission and current execution checks."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mindroom.mcp.config import resolved_mcp_tool_prefix
from mindroom.mcp.toolkit import MindRoomMCPToolkit

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.tool_jobs.execution_authority import JobTool


def function_provenance(tool: JobTool, arguments: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Read exact MCP bridge identity without importing or warming tools."""
    toolkit = tool.toolkit
    if not isinstance(toolkit, MindRoomMCPToolkit):
        return {}
    name = tool.function.name
    remote = (
        next(
            (remote_tool.remote_name for remote_tool in toolkit.catalog.tools if remote_tool.function_name == name),
            None,
        )
        if toolkit.catalog is not None
        else None
    )
    if toolkit.server_config is not None:
        prefix = resolved_mcp_tool_prefix(toolkit.server_id, toolkit.server_config)
        if name == f"{prefix}_call_tool":
            remote = (arguments or {}).get("tool_name")
    return {"mcp_server_id": toolkit.server_id, "mcp_tool_name": remote}
