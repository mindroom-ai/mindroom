"""MindRoom-specific MCP client session capabilities."""

from __future__ import annotations

from typing import TYPE_CHECKING

import mcp.types as mcp_types
from mcp import ClientSession

if TYPE_CHECKING:
    from datetime import timedelta

    from mcp.shared.message import MessageMetadata
    from mcp.shared.session import ProgressFnT, ReceiveResultT


_MCP_APP_EXTENSION_ID = "io.modelcontextprotocol/ui"
MCP_APP_HTML_MIME_TYPE = "text/html;profile=mcp-app"


def _with_mcp_app_capability(request: mcp_types.ClientRequest) -> mcp_types.ClientRequest:
    root = request.root
    if not isinstance(root, mcp_types.InitializeRequest):
        return request

    capabilities = root.params.capabilities.model_dump(by_alias=True, exclude_none=True)
    existing_extensions = capabilities.get("extensions")
    extensions = dict(existing_extensions) if isinstance(existing_extensions, dict) else {}
    existing_ui = extensions.get(_MCP_APP_EXTENSION_ID)
    ui_capability = dict(existing_ui) if isinstance(existing_ui, dict) else {}
    existing_mime_types = ui_capability.get("mimeTypes")
    mime_types = list(existing_mime_types) if isinstance(existing_mime_types, list) else []
    if MCP_APP_HTML_MIME_TYPE not in mime_types:
        mime_types.append(MCP_APP_HTML_MIME_TYPE)
    ui_capability["mimeTypes"] = mime_types
    extensions[_MCP_APP_EXTENSION_ID] = ui_capability
    capabilities["extensions"] = extensions

    updated_params = root.params.model_copy(
        update={"capabilities": mcp_types.ClientCapabilities.model_validate(capabilities)},
    )
    return mcp_types.ClientRequest(root.model_copy(update={"params": updated_params}))


class MCPClientSession(ClientSession):
    """MCP client session that negotiates MCP Apps support."""

    async def send_request(
        self,
        request: mcp_types.ClientRequest,
        result_type: type[ReceiveResultT],
        request_read_timeout_seconds: timedelta | None = None,
        metadata: MessageMetadata = None,
        progress_callback: ProgressFnT | None = None,
    ) -> ReceiveResultT:
        """Advertise MCP Apps on initialization, then use the SDK request path."""
        return await super().send_request(
            _with_mcp_app_capability(request),
            result_type,
            request_read_timeout_seconds=request_read_timeout_seconds,
            metadata=metadata,
            progress_callback=progress_callback,
        )
