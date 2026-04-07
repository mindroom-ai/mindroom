"""Typed MCP runtime errors."""

from __future__ import annotations


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


class MCPToolCallError(MCPError):
    """Raised when a tool call returns an explicit MCP error result."""
