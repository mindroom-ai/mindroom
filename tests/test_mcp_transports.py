"""Tests for MCP transport helpers."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, cast

import pytest

import mindroom.mcp.transports as transport_module
from mindroom.constants import resolve_runtime_paths
from mindroom.mcp.config import MCPServerConfig
from mindroom.mcp.transports import (
    _build_stdio_server_parameters,
    _interpolate_mcp_env,
    _interpolate_mcp_headers,
    _TransportStreams,
    build_transport_handle,
)
from mindroom.server_fetch_url import ServerFetchUrlError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mindroom.constants import RuntimePaths


def _runtime_paths(tmp_path: Path) -> RuntimePaths:
    return resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env={"API_TOKEN": "secret-token", "EXTRA_ARG": "value"},
    )


def _public_addrinfo() -> list[tuple[int, int, int, str, tuple[str, int]]]:
    return [(0, 0, 0, "", ("93.184.216.34", 443))]


def test_interpolate_mcp_env_and_headers(tmp_path: Path) -> None:
    """Resolve environment placeholders in env vars and HTTP headers."""
    runtime_paths = _runtime_paths(tmp_path)
    assert _interpolate_mcp_env({"TOKEN": "${API_TOKEN}"}, runtime_paths) == {"TOKEN": "secret-token"}
    assert _interpolate_mcp_headers({"Authorization": "Bearer ${API_TOKEN}"}, runtime_paths) == {
        "Authorization": "Bearer secret-token",
    }


def test_build_stdio_server_parameters_interpolates_env(tmp_path: Path) -> None:
    """Interpolate stdio env vars while leaving argv entries unchanged."""
    runtime_paths = _runtime_paths(tmp_path)
    params = _build_stdio_server_parameters(
        MCPServerConfig(
            transport="stdio",
            command="npx",
            args=["-y", "${EXTRA_ARG}"],
            env={"TOKEN": "${API_TOKEN}"},
        ),
        runtime_paths,
    )
    assert params.command == "npx"
    assert params.args == ["-y", "${EXTRA_ARG}"]
    assert params.env is not None
    assert params.env["TOKEN"] == runtime_paths.env_value("API_TOKEN")


def test_build_transport_handle_returns_expected_transport(tmp_path: Path) -> None:
    """Return the deferred opener matching the configured transport."""
    runtime_paths = _runtime_paths(tmp_path)
    assert (
        build_transport_handle(
            "demo",
            MCPServerConfig(transport="stdio", command="npx"),
            runtime_paths,
        ).transport
        == "stdio"
    )
    assert (
        build_transport_handle(
            "demo",
            MCPServerConfig(transport="sse", url="http://localhost:8000/sse"),
            runtime_paths,
        ).transport
        == "sse"
    )


@pytest.mark.asyncio
async def test_open_sse_interpolates_headers_and_passes_timeouts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Open SSE transports with interpolated headers and configured timeouts."""
    runtime_paths = _runtime_paths(tmp_path)
    monkeypatch.setattr("mindroom.server_fetch_url.socket.getaddrinfo", lambda *_args, **_kwargs: _public_addrinfo())
    streams = cast("_TransportStreams", (object(), object()))
    captured: dict[str, object] = {}

    @asynccontextmanager
    async def fake_sse_client(
        url: str,
        **kwargs: object,
    ) -> AsyncIterator[_TransportStreams]:
        captured.update(url=url, **kwargs)
        yield streams

    monkeypatch.setattr(transport_module, "sse_client", fake_sse_client)
    server_config = MCPServerConfig(
        transport="sse",
        url="https://mcp.example/sse",
        headers={"Authorization": "Bearer ${API_TOKEN}"},
        startup_timeout_seconds=1.5,
        call_timeout_seconds=2.5,
    )

    handle = build_transport_handle("demo", server_config, runtime_paths)

    async with handle.opener() as opened_streams:
        assert opened_streams == streams

    assert captured == {
        "url": "https://mcp.example/sse",
        "headers": {"Authorization": "Bearer secret-token"},
        "timeout": 1.5,
        "sse_read_timeout": 2.5,
    }


@pytest.mark.asyncio
async def test_open_streamable_http_interpolates_headers_passes_timeouts_and_drops_session_getter(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Open streamable HTTP transports while dropping the session id getter."""
    runtime_paths = _runtime_paths(tmp_path)
    monkeypatch.setattr("mindroom.server_fetch_url.socket.getaddrinfo", lambda *_args, **_kwargs: _public_addrinfo())
    read_stream = object()
    write_stream = object()
    captured: dict[str, object] = {}

    @asynccontextmanager
    async def fake_streamablehttp_client(
        url: str,
        **kwargs: object,
    ) -> AsyncIterator[tuple[object, object, object]]:
        captured.update(url=url, **kwargs)
        yield read_stream, write_stream, lambda: "session-id"

    monkeypatch.setattr(transport_module, "streamablehttp_client", fake_streamablehttp_client)
    server_config = MCPServerConfig(
        transport="streamable-http",
        url="https://mcp.example/mcp",
        headers={"X-Token": "${API_TOKEN}"},
        startup_timeout_seconds=3.5,
        call_timeout_seconds=4.5,
    )

    handle = build_transport_handle("demo", server_config, runtime_paths)

    async with handle.opener() as streams:
        assert streams == (read_stream, write_stream)

    assert captured == {
        "url": "https://mcp.example/mcp",
        "headers": {"X-Token": "secret-token"},
        "timeout": 3.5,
        "sse_read_timeout": 4.5,
    }


@pytest.mark.asyncio
async def test_open_sse_requires_runtime_url(tmp_path: Path) -> None:
    """Keep the SSE runtime guard for configs that bypass model validation."""
    runtime_paths = _runtime_paths(tmp_path)
    server_config = MCPServerConfig.model_construct(transport="sse", url=None)
    handle = build_transport_handle("demo", server_config, runtime_paths)

    with pytest.raises(ValueError, match="sse MCP servers require url"):
        async with handle.opener():
            pass


@pytest.mark.asyncio
async def test_open_streamable_http_requires_runtime_url(tmp_path: Path) -> None:
    """Keep the streamable HTTP runtime guard for configs that bypass model validation."""
    runtime_paths = _runtime_paths(tmp_path)
    server_config = MCPServerConfig.model_construct(transport="streamable-http", url=None)
    handle = build_transport_handle("demo", server_config, runtime_paths)

    with pytest.raises(ValueError, match="streamable-http MCP servers require url"):
        async with handle.opener():
            pass


@pytest.mark.asyncio
async def test_open_remote_transport_rejects_private_url_before_client_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Remote MCP transports should reject SSRF URLs before opening the client."""
    runtime_paths = _runtime_paths(tmp_path)
    client_called = False

    @asynccontextmanager
    async def fake_sse_client(
        url: str,
        **kwargs: object,
    ) -> AsyncIterator[_TransportStreams]:
        nonlocal client_called
        client_called = True
        del url, kwargs
        yield cast("_TransportStreams", (object(), object()))

    monkeypatch.setattr(transport_module, "sse_client", fake_sse_client)
    server_config = MCPServerConfig(transport="sse", url="http://127.0.0.1:8000/sse")
    handle = build_transport_handle("demo", server_config, runtime_paths)

    with pytest.raises(ServerFetchUrlError):
        async with handle.opener():
            pass

    assert client_called is False
