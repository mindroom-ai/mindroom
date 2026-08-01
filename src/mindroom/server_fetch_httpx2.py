"""Lazy-loaded HTTPX2 transport with MindRoom's server-fetch protections."""

from __future__ import annotations

import ssl  # noqa: TC003 - Required for runtime get_type_hints on the public transport constructor.
from collections.abc import Awaitable, Callable, Iterable  # noqa: TC003
from ipaddress import IPv4Address, IPv6Address

import httpcore2
import httpx2
from httpcore2._backends.anyio import AnyIOBackend
from httpcore2._backends.base import SOCKET_OPTION  # noqa: TC002
from httpx2._config import DEFAULT_LIMITS, create_ssl_context
from httpx2._types import CertTypes  # noqa: TC002

from mindroom.server_fetch_url import (
    ServerFetchUrlError,
    resolve_server_fetch_connect_addresses,
    validate_server_fetch_request_url,
)

_IPAddress = IPv4Address | IPv6Address


class _ServerFetchAsyncNetworkBackend(httpcore2.AsyncNetworkBackend):
    """httpcore2 backend that validates every address dialed by MCP 2."""

    def __init__(self, *, allow_private_networks: bool) -> None:
        self._allow_private_networks = allow_private_networks
        self._backend: httpcore2.AsyncNetworkBackend = AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 - Signature must match httpcore2.
        local_address: str | None = None,
        socket_options: Iterable[SOCKET_OPTION] | None = None,
    ) -> httpcore2.AsyncNetworkStream:
        return await _connect_validated_async(
            resolve_server_fetch_connect_addresses(
                host,
                port=port,
                allow_private_networks=self._allow_private_networks,
            ),
            lambda address: self._backend.connect_tcp(
                address.compressed,
                port,
                timeout=timeout,
                local_address=local_address,
                socket_options=socket_options,
            ),
        )

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,  # noqa: ASYNC109 - Signature must match httpcore2.
        socket_options: Iterable[SOCKET_OPTION] | None = None,
    ) -> httpcore2.AsyncNetworkStream:
        return await self._backend.connect_unix_socket(path, timeout=timeout, socket_options=socket_options)

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


async def _connect_validated_async(
    addresses: list[_IPAddress],
    connect: Callable[[_IPAddress], Awaitable[httpcore2.AsyncNetworkStream]],
) -> httpcore2.AsyncNetworkStream:
    last_error: httpcore2.ConnectError | httpcore2.ConnectTimeout | None = None
    for address in addresses:
        try:
            return await connect(address)
        except (httpcore2.ConnectError, httpcore2.ConnectTimeout) as e:
            last_error = e
    if last_error is not None:
        raise last_error
    raise ServerFetchUrlError(reason="dns_resolution_failed")


class ServerFetchHTTPX2AsyncTransport(httpx2.AsyncHTTPTransport):
    """HTTPX2 transport that preserves the server-fetch guard for MCP 2."""

    def __init__(
        self,
        *,
        allow_private_networks: bool = False,
        verify: ssl.SSLContext | str | bool = True,
        cert: CertTypes | None = None,
        trust_env: bool = True,
        http1: bool = True,
        http2: bool = False,
        limits: httpx2.Limits = DEFAULT_LIMITS,
        local_address: str | None = None,
        retries: int = 0,
        socket_options: Iterable[SOCKET_OPTION] | None = None,
    ) -> None:
        self._allow_private_networks = allow_private_networks
        ssl_context = create_ssl_context(verify=verify, cert=cert, trust_env=trust_env)
        self._pool: httpcore2.AsyncConnectionPool = httpcore2.AsyncConnectionPool(
            ssl_context=ssl_context,
            max_connections=limits.max_connections,
            max_keepalive_connections=limits.max_keepalive_connections,
            keepalive_expiry=limits.keepalive_expiry,
            http1=http1,
            http2=http2,
            local_address=local_address,
            network_backend=_ServerFetchAsyncNetworkBackend(allow_private_networks=allow_private_networks),
            retries=retries,
            socket_options=socket_options,
        )

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        """Validate each MCP 2 request before HTTPX2 sends it."""
        validate_server_fetch_request_url(str(request.url), allow_private_networks=self._allow_private_networks)
        return await super().handle_async_request(request)

    async def aclose(self) -> None:
        """Close the underlying HTTPX2 connection pool."""
        await self._pool.aclose()
