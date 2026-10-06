"""Tests for MCP transport helpers."""

from __future__ import annotations

import asyncio
import socket
import threading
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, cast

import httpcore2
import httpx2
import pytest
from httpcore2._backends.anyio import AnyIOBackend
from mcp import ClientSession, MCPError
from mcp.types import INTERNAL_ERROR

import mindroom.mcp.transports as transport_module
from mindroom.constants import resolve_runtime_paths
from mindroom.mcp.config import MCPServerConfig
from mindroom.mcp.manager import MCPServerManager
from mindroom.mcp.transports import (
    _build_stdio_server_parameters,
    _interpolate_mcp_env,
    _interpolate_mcp_headers,
    _MCPTransportHandle,
    _server_fetch_mcp_http_client,
    build_transport_handle,
)
from mindroom.server_fetch_httpx2 import ServerFetchAsyncHTTPX2Transport
from mindroom.server_fetch_url import ServerFetchUrlError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.mcp.config import MCPTransport
    from mindroom.mcp.transports import _TransportStreams

_REMOTE_TRANSPORT_URLS: dict[MCPTransport, str] = {
    "sse": "https://mcp.example/sse",
    "streamable-http": "https://mcp.example/mcp",
}


def _runtime_paths(tmp_path: Path) -> RuntimePaths:
    return resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env={"API_TOKEN": "secret-token", "EXTRA_ARG": "value"},
    )


@pytest.fixture(autouse=True)
def _public_dns_for_mcp_transport_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep remote transport tests focused on URL policy instead of live DNS."""
    monkeypatch.setattr(
        "mindroom.server_fetch_url.socket.getaddrinfo",
        lambda *_args, **_kwargs: [(0, 0, 0, "", ("93.184.216.34", 443))],
    )


def _addrinfo(ip_address: str) -> list[tuple[int, int, int, str, tuple[str, int]]]:
    return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip_address, 443))]


def _nested_exceptions(exc: BaseException) -> list[BaseException]:
    """Flatten task-group and cause chains the SDK transports raise through."""
    found = [exc]
    if isinstance(exc, BaseExceptionGroup):
        for nested in exc.exceptions:
            found.extend(_nested_exceptions(nested))
    if exc.__cause__ is not None:
        found.extend(_nested_exceptions(exc.__cause__))
    return found


async def _initialize_over(handle: _MCPTransportHandle) -> None:
    """Drive the SDK's own client stack over one MindRoom transport handle."""
    async with (
        asyncio.timeout(5),
        handle.opener() as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()


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

    httpx_client_factory = cast("Callable[[], httpx2.AsyncClient]", captured.pop("httpx_client_factory"))
    async with httpx_client_factory() as client:
        assert isinstance(client._transport, ServerFetchAsyncHTTPX2Transport)
    assert captured == {
        "url": "https://mcp.example/sse",
        "headers": {"Authorization": "Bearer secret-token"},
        "timeout": 1.5,
        "sse_read_timeout": 2.5,
    }


@pytest.mark.asyncio
async def test_open_streamable_http_interpolates_headers_and_passes_timeouts_on_http_client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Open streamable HTTP transports on a guarded client carrying headers and timeouts."""
    runtime_paths = _runtime_paths(tmp_path)
    read_stream = object()
    write_stream = object()
    captured: dict[str, object] = {}

    @asynccontextmanager
    async def fake_streamable_http_client(
        url: str,
        *,
        http_client: httpx2.AsyncClient,
        max_sse_event_size: int | None,
    ) -> AsyncIterator[tuple[object, object]]:
        captured.update(url=url, http_client_closed=http_client.is_closed, max_sse_event_size=max_sse_event_size)
        assert isinstance(http_client._transport, ServerFetchAsyncHTTPX2Transport)
        assert http_client.headers["X-Token"] == "secret-token"
        assert http_client.timeout == httpx2.Timeout(3.5, read=4.5)
        yield read_stream, write_stream

    monkeypatch.setattr(transport_module, "streamable_http_client", fake_streamable_http_client)
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

    assert captured == {"url": "https://mcp.example/mcp", "http_client_closed": False, "max_sse_event_size": None}


@pytest.mark.asyncio
async def test_remote_transport_latches_http_401_without_response_content(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The handle retains structured bearer rejection after the MCP SDK closes its streams."""
    runtime_paths = _runtime_paths(tmp_path)
    captured: dict[str, object] = {}

    @asynccontextmanager
    async def fake_streamable_http_client(
        _url: str,
        *,
        http_client: httpx2.AsyncClient,
        max_sse_event_size: int | None,  # noqa: ARG001
    ) -> AsyncIterator[tuple[object, object]]:
        captured.update(http_client=http_client)
        yield object(), object()

    monkeypatch.setattr(transport_module, "streamable_http_client", fake_streamable_http_client)
    handle = build_transport_handle(
        "demo",
        MCPServerConfig(transport="streamable-http", url="https://mcp.example/mcp"),
        runtime_paths,
    )

    async with handle.opener():
        client = cast("httpx2.AsyncClient", captured["http_client"])
        request = httpx2.Request("POST", "https://mcp.example/mcp")
        for hook in client.event_hooks["response"]:
            await hook(httpx2.Response(401, request=request, content=b"secret provider response"))

    assert handle.authorization_rejected()


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
async def test_open_sse_validates_transport_url_off_event_loop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Offload URL validation because DNS resolution can block."""
    runtime_paths = _runtime_paths(tmp_path)
    loop_thread_id = threading.get_ident()
    validator_thread_ids: list[int] = []
    streams = cast("_TransportStreams", (object(), object()))

    def fake_validate_server_fetch_url(url: str) -> str:
        validator_thread_ids.append(threading.get_ident())
        if threading.get_ident() == loop_thread_id:
            msg = "URL validation should not run on the event-loop thread"
            raise AssertionError(msg)
        return url

    @asynccontextmanager
    async def fake_sse_client(
        url: str,
        **kwargs: object,
    ) -> AsyncIterator[_TransportStreams]:
        del url, kwargs
        yield streams

    monkeypatch.setattr(transport_module, "validate_server_fetch_url", fake_validate_server_fetch_url)
    monkeypatch.setattr(transport_module, "sse_client", fake_sse_client)
    server_config = MCPServerConfig(transport="sse", url="https://mcp.example/sse")
    handle = build_transport_handle("demo", server_config, runtime_paths)

    async with handle.opener() as opened_streams:
        assert opened_streams == streams

    assert validator_thread_ids
    assert loop_thread_id not in validator_thread_ids


@pytest.mark.asyncio
async def test_open_sse_rejects_private_transport_url(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Remote MCP transports should reject private-network URLs before opening clients."""
    runtime_paths = _runtime_paths(tmp_path)

    @asynccontextmanager
    async def fake_sse_client(
        url: str,
        **kwargs: object,
    ) -> AsyncIterator[_TransportStreams]:
        del url, kwargs
        msg = "unsafe MCP URL should be rejected before the SSE client opens"
        raise AssertionError(msg)
        yield cast("_TransportStreams", (object(), object()))

    monkeypatch.setattr(transport_module, "sse_client", fake_sse_client)
    server_config = MCPServerConfig(transport="sse", url="http://127.0.0.1:8000/sse")
    handle = build_transport_handle("demo", server_config, runtime_paths)

    with pytest.raises(ServerFetchUrlError) as exc_info:
        async with handle.opener():
            pass

    assert exc_info.value.reason == "private_address"


@pytest.mark.asyncio
async def test_open_streamable_http_rejects_metadata_transport_url(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Remote MCP transports should reject cloud metadata URLs before opening clients."""
    runtime_paths = _runtime_paths(tmp_path)

    @asynccontextmanager
    async def fake_streamable_http_client(
        url: str,
        **kwargs: object,
    ) -> AsyncIterator[tuple[object, object]]:
        del url, kwargs
        msg = "unsafe MCP URL should be rejected before the streamable HTTP client opens"
        raise AssertionError(msg)
        yield object(), object()

    monkeypatch.setattr(transport_module, "streamable_http_client", fake_streamable_http_client)
    server_config = MCPServerConfig(
        transport="streamable-http",
        url="http://169.254.169.254/latest/meta-data/",
    )
    handle = build_transport_handle("demo", server_config, runtime_paths)

    with pytest.raises(ServerFetchUrlError) as exc_info:
        async with handle.opener():
            pass

    assert exc_info.value.reason == "metadata_address"


@pytest.mark.asyncio
async def test_mcp_http_client_factory_rejects_private_request_url() -> None:
    """The MCP HTTP client factory should validate redirects and request URLs through server-fetch transport."""
    async with _server_fetch_mcp_http_client(follow_redirects=False, verify=False, future_sdk_option=True) as client:
        with pytest.raises(ServerFetchUrlError) as exc_info:
            await client.get("http://127.0.0.1:8000/mcp")

    assert exc_info.value.reason == "private_address"


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["sse", "streamable-http"])
@pytest.mark.parametrize(
    ("dialed_address", "reason"),
    [
        ("10.0.0.5", "private_address"),
        ("169.254.1.1", "blocked_address"),
        ("169.254.169.254", "metadata_address"),
    ],
)
async def test_sdk_http_client_refuses_rebound_dial_address(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    transport: MCPTransport,
    dialed_address: str,
    reason: str,
) -> None:
    """A hostname that passes the open-time check but resolves internally when the SDK dials is never connected."""
    lookups: list[str] = []
    dialed: list[str] = []

    def rebinding_getaddrinfo(host: str, *_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
        lookups.append(host)
        return _addrinfo("93.184.216.34" if len(lookups) == 1 else dialed_address)

    async def record_connect(_backend: object, host: str, *_args: object, **_kwargs: object) -> object:
        dialed.append(host)
        msg = "connection refused"
        raise httpcore2.ConnectError(msg)

    monkeypatch.setattr("mindroom.server_fetch_url.socket.getaddrinfo", rebinding_getaddrinfo)
    monkeypatch.setattr(AnyIOBackend, "connect_tcp", record_connect)
    handle = build_transport_handle(
        "demo",
        MCPServerConfig(transport=transport, url=_REMOTE_TRANSPORT_URLS[transport]),
        _runtime_paths(tmp_path),
    )

    with pytest.raises(BaseException) as exc_info:  # noqa: PT011 - SSE raises directly, streamable HTTP in a group
        await _initialize_over(handle)

    errors = [exc for exc in _nested_exceptions(exc_info.value) if isinstance(exc, ServerFetchUrlError)]
    assert [error.reason for error in errors] == [reason]
    assert lookups == ["mcp.example", "mcp.example"]
    assert dialed == []


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["sse", "streamable-http"])
async def test_sdk_http_client_dials_only_the_validated_address(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    transport: MCPTransport,
) -> None:
    """The SDK's connection goes to the address validated at dial time, not to a fresh hostname lookup."""
    dialed: list[tuple[str, int]] = []

    async def record_connect(_backend: object, host: str, port: int, *_args: object, **_kwargs: object) -> object:
        dialed.append((host, port))
        msg = "connection refused"
        raise httpcore2.ConnectError(msg)

    monkeypatch.setattr(AnyIOBackend, "connect_tcp", record_connect)
    handle = build_transport_handle(
        "demo",
        MCPServerConfig(transport=transport, url=_REMOTE_TRANSPORT_URLS[transport]),
        _runtime_paths(tmp_path),
    )

    with pytest.raises(BaseException) as exc_info:  # noqa: PT011 - SSE raises directly, streamable HTTP in a group
        await _initialize_over(handle)

    assert any(isinstance(exc, httpx2.ConnectError) for exc in _nested_exceptions(exc_info.value))
    assert dialed == [("93.184.216.34", 443)]


@pytest.mark.asyncio
async def test_streamable_http_401_is_latched_through_the_sdk(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A bearer rejection the SDK reports only as a per-request JSON-RPC error still marks the handle rejected."""

    def unauthorized(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(401, text="secret provider response", request=request)

    monkeypatch.setattr(transport_module, "ServerFetchAsyncHTTPX2Transport", lambda: httpx2.MockTransport(unauthorized))
    handle = build_transport_handle(
        "demo",
        MCPServerConfig(transport="streamable-http", url="https://mcp.example/mcp"),
        _runtime_paths(tmp_path),
    )

    with pytest.raises(BaseException) as exc_info:  # noqa: PT011 - the session's task group wraps the request error
        await _initialize_over(handle)

    errors = [exc for exc in _nested_exceptions(exc_info.value) if isinstance(exc, MCPError)]
    assert [error.code for error in errors] == [INTERNAL_ERROR]
    assert "secret provider response" not in str(errors[0])
    assert handle.authorization_rejected()


@pytest.mark.asyncio
async def test_sse_401_is_latched_and_classified_through_the_sdk(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A rejected SSE stream open keeps its structured HTTP status for OAuth reconnect classification."""

    def unauthorized(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(401, text="secret provider response", request=request)

    monkeypatch.setattr(transport_module, "ServerFetchAsyncHTTPX2Transport", lambda: httpx2.MockTransport(unauthorized))
    handle = build_transport_handle(
        "demo",
        MCPServerConfig(transport="sse", url="https://mcp.example/sse"),
        _runtime_paths(tmp_path),
    )

    with pytest.raises(httpx2.HTTPStatusError) as exc_info:
        await _initialize_over(handle)

    assert MCPServerManager._runtime_exception_has_http_status(exc_info.value, 401)
    assert handle.authorization_rejected()
