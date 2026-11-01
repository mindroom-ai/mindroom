"""Typed MCP runtime errors."""

from __future__ import annotations

from typing import Any


class MCPError(RuntimeError):
    """Base class for MindRoom MCP failures."""

    def __init__(self, server_id: str, message: str) -> None:
        super().__init__(message)
        self.server_id = server_id


class MCPConnectionError(MCPError):
    """Raised when a server cannot be reached or reconnects fail."""


class MCPTimeoutError(MCPError):
    """Raised when an MCP operation times out."""


class MCPProtocolError(MCPError):
    """Raised when an MCP response is invalid or inconsistent."""


class MCPToolUnavailableError(MCPProtocolError):
    """Raised when the current filtered catalog does not expose a requested tool."""

    def __init__(self, server_id: str, tool_name: str, available_tools: tuple[str, ...]) -> None:
        super().__init__(server_id, f"MCP tool '{tool_name}' is not available for server '{server_id}'")
        self.tool_name = tool_name
        self.available_tools = available_tools


class MCPToolCallError(MCPError):
    """Raised when a tool call returns an explicit MCP error result.

    ``input_schema`` is the called tool's input schema in the catalog the call was sent with, when that is known.
    """

    def __init__(self, server_id: str, message: str, *, input_schema: dict[str, Any] | None = None) -> None:
        super().__init__(server_id, message)
        self.input_schema = input_schema
