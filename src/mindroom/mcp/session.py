"""MindRoom-specific MCP client session capabilities."""

from __future__ import annotations

from typing import Any

_MCP_APP_EXTENSION_ID = "io.modelcontextprotocol/ui"
MCP_APP_HTML_MIME_TYPE = "text/html;profile=mcp-app"
MCP_APP_CLIENT_EXTENSIONS: dict[str, dict[str, Any]] = {
    _MCP_APP_EXTENSION_ID: {"mimeTypes": [MCP_APP_HTML_MIME_TYPE]},
}
