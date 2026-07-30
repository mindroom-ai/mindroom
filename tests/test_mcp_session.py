"""Tests for MindRoom's MCP client session capabilities."""

from __future__ import annotations

from typing import TYPE_CHECKING

import mcp.types as mcp_types
import pytest

from mindroom.mcp.session import MCPClientSession

if TYPE_CHECKING:
    from collections.abc import Callable


@pytest.mark.asyncio
async def test_mcp_client_session_advertises_mcp_apps_without_losing_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Initialization should add MCP Apps beside capabilities already chosen by the SDK."""
    captured_requests: list[mcp_types.ClientRequest] = []

    async def _capture_request(
        _session: object,
        request: mcp_types.ClientRequest,
        _result_type: type[object],
        request_read_timeout_seconds: object = None,
        metadata: object = None,
        progress_callback: Callable[..., object] | None = None,
    ) -> object:
        assert request_read_timeout_seconds is None
        assert metadata is None
        assert progress_callback is None
        captured_requests.append(request)
        return object()

    monkeypatch.setattr("mcp.ClientSession.send_request", _capture_request)
    capabilities = mcp_types.ClientCapabilities.model_validate(
        {
            "roots": {"listChanged": True},
            "extensions": {"example.com/other": {"enabled": True}},
        },
    )
    request = mcp_types.ClientRequest(
        mcp_types.InitializeRequest(
            params=mcp_types.InitializeRequestParams(
                protocolVersion=mcp_types.LATEST_PROTOCOL_VERSION,
                capabilities=capabilities,
                clientInfo=mcp_types.Implementation(name="mindroom-test", version="1"),
            ),
        ),
    )
    session = MCPClientSession.__new__(MCPClientSession)

    await session.send_request(request, object)

    advertised = captured_requests[0].root
    assert isinstance(advertised, mcp_types.InitializeRequest)
    dumped_capabilities = advertised.params.capabilities.model_dump(by_alias=True, exclude_none=True)
    assert dumped_capabilities["roots"] == {"listChanged": True}
    assert dumped_capabilities["extensions"] == {
        "example.com/other": {"enabled": True},
        "io.modelcontextprotocol/ui": {
            "mimeTypes": ["text/html;profile=mcp-app"],
        },
    }
