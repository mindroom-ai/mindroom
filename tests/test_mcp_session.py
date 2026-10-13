"""Tests for MindRoom's MCP client session capabilities."""

from __future__ import annotations

import anyio
import mcp.types as mcp_types
import pytest
from mcp import ClientSession

from mindroom.mcp.session import MCP_APP_CLIENT_EXTENSIONS


class _StopInitializeError(Exception):
    """Stop the handshake once the initialize request is captured."""


@pytest.mark.asyncio
async def test_mcp_client_session_advertises_mcp_apps_on_initialize(monkeypatch: pytest.MonkeyPatch) -> None:
    """The initialize request should advertise MCP Apps HTML support."""
    captured_requests: list[object] = []

    async def _capture_request(_session: object, request: object, *_args: object, **_kwargs: object) -> object:
        captured_requests.append(request)
        raise _StopInitializeError

    monkeypatch.setattr(ClientSession, "send_request", _capture_request)
    write_stream, read_stream = anyio.create_memory_object_stream(1)
    session = ClientSession(read_stream, write_stream, extensions=MCP_APP_CLIENT_EXTENSIONS)

    with pytest.raises(_StopInitializeError):
        await session.initialize()

    request = captured_requests[0]
    assert isinstance(request, mcp_types.InitializeRequest)
    assert request.params.capabilities.model_dump(by_alias=True, exclude_none=True)["extensions"] == {
        "io.modelcontextprotocol/ui": {"mimeTypes": ["text/html;profile=mcp-app"]},
    }
