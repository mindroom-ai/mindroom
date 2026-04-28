"""Mindroom compatibility helpers for Vertex Claude models."""

from __future__ import annotations

from typing import Any

from agno.models.vertexai.claude import Claude as VertexAIClaude


def strip_vertex_claude_tool_strict(
    tools: list[dict[str, Any]] | None,
) -> list[dict[str, Any]] | None:
    """Return Vertex-compatible tool definitions without mutating the caller's list.

    Agno 2.5.13 can emit OpenAI-style ``strict`` flags on tool definitions.
    Anthropic-on-Vertex rejects those provider-level fields with a 400 error
    (``tools.0.custom.strict``), while schema properties named ``strict`` are
    valid user data and must be preserved. Strip only the provider metadata here
    until Agno normalizes Vertex Claude tool payloads itself.
    """
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


class MindroomVertexAIClaude(VertexAIClaude):
    """Vertex Claude model with Mindroom-specific provider compatibility fixes."""

    def _prepare_request_kwargs(
        self,
        system_message: str,
        tools: list[dict[str, Any]] | None = None,
        response_format: dict[str, Any] | type[Any] | None = None,
        messages: list[Any] | None = None,
    ) -> dict[str, Any]:
        return super()._prepare_request_kwargs(
            system_message=system_message,
            tools=strip_vertex_claude_tool_strict(tools),
            response_format=response_format,
            messages=messages,
        )

    def _has_beta_features(
        self,
        response_format: dict[str, Any] | type[Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> bool:
        return super()._has_beta_features(
            response_format=response_format,
            tools=strip_vertex_claude_tool_strict(tools),
        )
