"""Transport builders for MindRoom MCP client sessions."""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import httpx
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, get_default_environment, stdio_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared.message import SessionMessage

from mindroom.server_fetch_url import ServerFetchAsyncHTTPTransport, validate_server_fetch_url

_ENV_REFERENCE_PATTERN = re.compile(r"\$\{([^}]+)\}")

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Mapping
    from contextlib import AbstractAsyncContextManager
    from typing import Any

    from mindroom.constants import RuntimePaths
    from mindroom.mcp.config import MCPServerConfig, MCPTransport

_TransportStreams = tuple[
    MemoryObjectReceiveStream[SessionMessage | Exception],
    MemoryObjectSendStream[SessionMessage],
]

if TYPE_CHECKING:
    _RemoteTransportClient = Callable[..., AbstractAsyncContextManager[tuple[Any, ...]]]


@dataclass(frozen=True)
class _MCPTransportHandle:
    """Deferred transport opener for one configured server."""

    transport: MCPTransport
    opener: Callable[[], AbstractAsyncContextManager[_TransportStreams]]


def _interpolate_value(value: str, runtime_paths: RuntimePaths) -> str:
    def replace(match: re.Match[str]) -> str:
        return runtime_paths.env_value(match.group(1), default="") or ""

    return _ENV_REFERENCE_PATTERN.sub(replace, value)


def _interpolate_mcp_env(values: Mapping[str, str], runtime_paths: RuntimePaths) -> dict[str, str]:
    """Resolve `${ENV_VAR}` placeholders in MCP env config."""
    return {name: _interpolate_value(value, runtime_paths) for name, value in values.items()}


def _interpolate_mcp_headers(values: Mapping[str, str], runtime_paths: RuntimePaths) -> dict[str, str]:
    """Resolve `${ENV_VAR}` placeholders in MCP header config."""
    return {name: _interpolate_value(value, runtime_paths) for name, value in values.items()}


def _server_fetch_mcp_http_client(
    headers: dict[str, str] | None = None,
    timeout: httpx.Timeout | None = None,
    auth: httpx.Auth | None = None,
    **_ignored: object,
) -> httpx.AsyncClient:
    """Create an MCP HTTP client that validates requests, redirects, and dialed addresses."""
    kwargs: dict[str, Any] = {
        "follow_redirects": True,
        "transport": ServerFetchAsyncHTTPTransport(),
    }
    if timeout is not None:
        kwargs["timeout"] = timeout
    if headers is not None:
        kwargs["headers"] = headers
    if auth is not None:
        kwargs["auth"] = auth
    return httpx.AsyncClient(**kwargs)


def _build_stdio_server_parameters(
    server_config: MCPServerConfig,
    runtime_paths: RuntimePaths | None = None,
) -> StdioServerParameters:
    """Build stdio launch parameters for the pinned MCP client."""
    if server_config.command is None:
        msg = "stdio MCP servers require command"
        raise ValueError(msg)
    env = server_config.env
    if runtime_paths is not None:
        env = _interpolate_mcp_env(server_config.env, runtime_paths)
    return StdioServerParameters(
        command=server_config.command,
        args=list(server_config.args),
        env={
            **get_default_environment(),
            **env,
        },
        cwd=server_config.cwd,
    )


@asynccontextmanager
async def _open_stdio(
    server_config: MCPServerConfig,
    runtime_paths: RuntimePaths,
) -> AsyncIterator[_TransportStreams]:
    async with stdio_client(_build_stdio_server_parameters(server_config, runtime_paths)) as streams:
        yield streams


@asynccontextmanager
async def _open_remote_transport(
    server_config: MCPServerConfig,
    runtime_paths: RuntimePaths,
    *,
    transport: MCPTransport,
    client: _RemoteTransportClient,
    extra_headers: Mapping[str, str] | None = None,
) -> AsyncIterator[_TransportStreams]:
    if server_config.url is None:
        msg = f"{transport} MCP servers require url"
        raise ValueError(msg)
    url = validate_server_fetch_url(server_config.url)
    headers = {
        **_interpolate_mcp_headers(server_config.headers, runtime_paths),
        **(dict(extra_headers) if extra_headers is not None else {}),
    }
    async with client(
        url,
        headers=headers,
        timeout=server_config.startup_timeout_seconds,
        sse_read_timeout=server_config.call_timeout_seconds,
        httpx_client_factory=_server_fetch_mcp_http_client,
    ) as streams:
        yield cast("_TransportStreams", streams[:2])


def build_transport_handle(
    server_id: str,
    server_config: MCPServerConfig,
    runtime_paths: RuntimePaths,
    *,
    extra_headers: Mapping[str, str] | None = None,
) -> _MCPTransportHandle:
    """Build a deferred transport opener for one configured MCP server."""
    if server_config.transport == "stdio":
        return _MCPTransportHandle(transport="stdio", opener=lambda: _open_stdio(server_config, runtime_paths))
    if server_config.transport == "sse":
        return _MCPTransportHandle(
            transport="sse",
            opener=lambda: _open_remote_transport(
                server_config,
                runtime_paths,
                transport="sse",
                client=sse_client,
                extra_headers=extra_headers,
            ),
        )
    if server_config.transport == "streamable-http":
        return _MCPTransportHandle(
            transport="streamable-http",
            opener=lambda: _open_remote_transport(
                server_config,
                runtime_paths,
                transport="streamable-http",
                client=streamablehttp_client,
                extra_headers=extra_headers,
            ),
        )
    msg = f"Unsupported MCP transport for server '{server_id}': {server_config.transport}"
    raise ValueError(msg)
