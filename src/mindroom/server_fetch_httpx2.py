"""Server-fetch transport for httpx2 clients, such as the ones handed to the MCP SDK.

It lives apart from `mindroom.server_fetch_url` so modules that only validate URLs do not import httpx2,
and it applies that module's validation and dial-time address pinning unchanged.
"""

from __future__ import annotations

import asyncio
import ssl  # noqa: TC003 - Required for runtime get_type_hints on public transport constructors.
from collections.abc import Iterable  # noqa: TC003

import httpcore2
import httpx2
from httpcore2._backends.anyio import AnyIOBackend
from httpx2._config import DEFAULT_LIMITS
from httpx2._types import CertTypes  # noqa: TC002

from mindroom.server_fetch_url import connect_validated_async, validate_server_fetch_url, validated_connect_addresses


class _ServerFetchAsyncNetworkBackend(httpcore2.AsyncNetworkBackend):
    """httpcore2 async network backend that validates the address it dials."""

    def __init__(self, *, allow_private_networks: bool) -> None:
        self._allow_private_networks = allow_private_networks
        self._backend: httpcore2.AsyncNetworkBackend = AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 - Signature must match httpcore2.
        local_address: str | None = None,
        socket_options: Iterable[httpcore2.SOCKET_OPTION] | None = None,
    ) -> httpcore2.AsyncNetworkStream:
        # The lookup blocks, so it runs in a thread to keep the event loop serving other work.
        addresses = await asyncio.to_thread(
            validated_connect_addresses,
            host,
            port=port,
            allow_private_networks=self._allow_private_networks,
        )
        return await connect_validated_async(
            addresses,
            lambda address: self._backend.connect_tcp(
                address.compressed,
                port,
                timeout=timeout,
                local_address=local_address,
                socket_options=socket_options,
            ),
            connect_errors=(httpcore2.ConnectError, httpcore2.ConnectTimeout),
        )

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,  # noqa: ASYNC109 - Signature must match httpcore2.
        socket_options: Iterable[httpcore2.SOCKET_OPTION] | None = None,
    ) -> httpcore2.AsyncNetworkStream:
        return await self._backend.connect_unix_socket(path, timeout=timeout, socket_options=socket_options)

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


class ServerFetchAsyncHTTPX2Transport(httpx2.AsyncHTTPTransport):
    """Async httpx2 transport that validates server-fetch URLs and dialed addresses."""

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
        socket_options: Iterable[httpcore2.SOCKET_OPTION] | None = None,
    ) -> None:
        self._allow_private_networks = allow_private_networks
        ssl_context = httpx2.create_ssl_context(verify=verify, cert=cert, trust_env=trust_env)
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
        """Validate each async request before httpx2 sends it."""
        validate_server_fetch_url(
            str(request.url),
            allow_private_networks=self._allow_private_networks,
            resolve_hostnames=False,
        )
        return await super().handle_async_request(request)

    async def aclose(self) -> None:
        """Close the underlying async connection pool."""
        await self._pool.aclose()
