"""MindRoom MCP client integration."""

from importlib import import_module
from typing import Any

from mindroom.mcp.config import MCPServerConfig, MCPTransport

__all__ = [
    "MCPServerConfig",
    "MCPServerManager",
    "MCPTransport",
]


def __getattr__(name: str) -> Any:  # noqa: ANN401
    if name != "MCPServerManager":
        msg = f"module {__name__!r} has no attribute {name!r}"
        raise AttributeError(msg)
    value = getattr(import_module("mindroom.mcp.manager"), name)
    globals()[name] = value
    return value
