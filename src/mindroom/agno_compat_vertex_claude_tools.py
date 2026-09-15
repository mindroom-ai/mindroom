"""Temporary Vertex Claude tool-schema compatibility for Agno."""

from __future__ import annotations

from typing import Any

from agno.utils.models.claude import format_tools_for_model

# AGNO_COMPAT: Vertex Claude tool definitions contain unsupported strict fields.
# Reason: Agno 3.0.9 emits provider-level strict fields that Vertex Claude
# rejects, while nested JSON-schema properties named strict remain valid.
# Upstream issue: https://github.com/agno-agi/agno/issues/6599
# Upstream PR: https://github.com/agno-agi/agno/pull/6923 (closed without merge)
# Remove when: The pinned Agno release formats Vertex Claude tools without
# top-level or function-level strict fields and preserves nested schema data.
# Coverage: tests/test_extra_kwargs.py::test_strip_vertex_claude_tool_strict_preserves_schema_and_input;
# tests/test_extra_kwargs.py::test_mindroom_vertexai_claude_request_kwargs_strip_tool_strict.


def strip_vertex_claude_tool_strict(
    tools: list[dict[str, Any]] | None,
) -> list[dict[str, Any]] | None:
    """Copy only changed definitions and remove provider-level strict fields."""
    if not tools:
        return tools

    changed = False
    sanitized: list[dict[str, Any]] = []
    for tool in tools:
        next_tool = tool
        if "strict" in next_tool:
            next_tool = dict(next_tool)
            next_tool.pop("strict", None)
            changed = True

        function = next_tool.get("function")
        if isinstance(function, dict) and "strict" in function:
            if next_tool is tool:
                next_tool = dict(next_tool)
            next_function = dict(function)
            next_function.pop("strict", None)
            next_tool["function"] = next_function
            changed = True

        sanitized.append(next_tool)

    return sanitized if changed else tools


def format_tools_for_vertex_claude(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    """Format sanitized tools through Agno's shared Claude formatter."""
    sanitized = strip_vertex_claude_tool_strict(tools)
    return format_tools_for_model(sanitized) if sanitized else None
