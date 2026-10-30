"""Shared JSON payload helpers for custom tools."""

from __future__ import annotations

import json

from mindroom.tool_system.runtime_context import ToolRuntimeContext, get_tool_runtime_context


def custom_tool_payload(
    tool_name: str,
    status: str,
    sort_keys: bool = True,
    /,
    **fields: object,
) -> str:
    """Build the JSON payload returned by custom tools."""
    payload: dict[str, object] = {"status": status, "tool": tool_name}
    payload.update(fields)
    return json.dumps(payload, sort_keys=sort_keys)


def tool_context_or_error(
    tool_name: str,
    label: str,
    sort_keys: bool = True,
    /,
    **fields: object,
) -> ToolRuntimeContext | str:
    """Return the live tool runtime context, or this tool's error payload saying it is unavailable."""
    context = get_tool_runtime_context()
    if context is not None:
        return context
    return custom_tool_payload(
        tool_name,
        "error",
        sort_keys,
        **fields,
        message=f"{label} tool context is unavailable in this runtime path.",
    )
