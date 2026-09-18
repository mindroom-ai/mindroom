"""Exact local callable provenance shared by admission and current execution checks."""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any

from mindroom.mcp.config import resolved_mcp_tool_prefix
from mindroom.mcp.toolkit import MindRoomMCPToolkit
from mindroom.tool_system.declarations import tool_schema_source
from mindroom.tool_system.skills import skill_access_origin

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from agno.tools.function import Function


def _source_entrypoint(function: Function) -> Callable[..., object] | None:
    """Resolve SDK hooks and the source retained by MindRoom's output-file wrapper."""
    return tool_schema_source(inspect.unwrap(function.entrypoint)) if function.entrypoint is not None else None


def callable_origin(function: Function) -> dict[str, Any]:
    """Return the concrete callable origin behind the SDK hook wrappers."""
    entrypoint = _source_entrypoint(function)
    return {
        "module": entrypoint.__module__ if inspect.isfunction(entrypoint) or inspect.ismethod(entrypoint) else None,
        "qualname": entrypoint.__qualname__ if inspect.isfunction(entrypoint) or inspect.ismethod(entrypoint) else None,
    }


def function_provenance(function: Function, arguments: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Read exact callable and MCP bridge identity without importing or warming tools."""
    origin = callable_origin(function)
    origin.update(skill_access_origin(_source_entrypoint(function), (arguments or {}).get("skill_name")))
    toolkit = function.source_toolkit
    if isinstance(toolkit, MindRoomMCPToolkit):
        origin["mcp_server_id"] = toolkit.server_id
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
        origin["mcp_tool_name"] = remote
    return origin
