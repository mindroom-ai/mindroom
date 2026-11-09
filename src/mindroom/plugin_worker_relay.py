"""The primary's client of the relay a dedicated worker's runner serves (``mindroom.api.worker_preview``).

A plugin route reaches a worker service of its own plugin by name, or a server the agent runs by port, and gets
aiohttp's answer as the runner streamed it: the client never follows a redirect, decompresses, or keeps a cookie.
"""

from __future__ import annotations

import asyncio
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import aiohttp
from fastapi import HTTPException
from yarl import URL

from mindroom.plugin_worker_services import SERVICE_NAME_PATTERN
from mindroom.previews.relay import relay_websocket
from mindroom.tool_system.sandbox_proxy import (
    lease_worker_manager,
    sandbox_proxy_config,
    worker_proxy_client_config,
    worker_route,
)
from mindroom.tool_system.worker_proxy_client import worker_request_headers
from mindroom.workers.backend_kinds import worker_backend_kind
from mindroom.workers.in_flight import worker_call_in_flight
from mindroom.workers.models import worker_api_root

if TYPE_CHECKING:
    from collections.abc import AsyncIterable, AsyncIterator, Iterable

    from starlette.websockets import WebSocket

    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget
    from mindroom.workers.backend import WorkerBackend

_REFUSED_HEADER = "x-mindroom-relay-refused"
# Headers aiohttp adds to a request that lacks them, which the relay leaves to the route.
_AIOHTTP_DEFAULT_HEADERS = ("Accept", "User-Agent", "Accept-Encoding", "Content-Type")
# The runner's limit on one WebSocket message.
_MAX_MESSAGE_BYTES = 16 * 1024 * 1024
_TIMEOUT = aiohttp.ClientTimeout(total=None, sock_connect=15)


@dataclass(frozen=True)
class WorkerRelay:
    """The relay into one agent's running dedicated worker, for the plugin whose route opened it.

    Each call names exactly one of ``service``, a worker service of that plugin, or ``port``, a server listening on the
    worker's loopback that the worker's own processes run. ``path`` is percent-encoded, with its query, and stays
    below the service or port it names. The runner marks its own refusals with the ``x-mindroom-relay-refused``
    header: 403 for a port it never reaches, 502 when nothing answers, 503 for a service that is not running. While a
    call's answer or WebSocket is open, idle cleanup leaves the worker running.
    """

    _root: str
    _headers: dict[str, str] = field(repr=False)
    _plugin: str
    _backend: WorkerBackend = field(repr=False)
    _worker_key: str

    def _url(self, path: str, service: str | None, port: int | None) -> URL:
        if (service is None) == (port is None):
            msg = "Name exactly one of service and port."
            raise ValueError(msg)
        if service is not None and not re.fullmatch(SERVICE_NAME_PATTERN, service):
            msg = f"{service!r} is no worker service name."
            raise ValueError(msg)
        base = (
            f"{self._root}/services/{self._plugin}/{service}" if service is not None else f"{self._root}/ports/{port}"
        )
        # Parsed as a path of its own and joined encoded, so its dot segments stay below the service or port.
        return URL(f"{base}{URL('/' + path.lstrip('/')).raw_path_qs}", encoded=True)

    def _route_headers(self, headers: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
        """Return the runner token, then the route's ``headers`` without any copy of it."""
        return [
            *self._headers.items(),
            *((name, value) for name, value in headers if name.lower() not in self._headers),
        ]

    @staticmethod
    def _session() -> aiohttp.ClientSession:
        return aiohttp.ClientSession(
            cookie_jar=aiohttp.DummyCookieJar(),
            skip_auto_headers=_AIOHTTP_DEFAULT_HEADERS,
            timeout=_TIMEOUT,
        )

    @asynccontextmanager
    async def http(
        self,
        method: str,
        path: str,
        *,
        service: str | None = None,
        port: int | None = None,
        headers: Iterable[tuple[str, str]] = (),
        body: bytes | AsyncIterable[bytes] | None = None,
    ) -> AsyncIterator[aiohttp.ClientResponse]:
        """Send one request, its ``body`` streamed, and yield the answer as it streams in, until the block ends."""
        url = self._url(path, service, port)
        with worker_call_in_flight(self._backend, self._worker_key):
            async with (
                self._session() as session,
                session.request(
                    method,
                    url,
                    headers=self._route_headers(headers),
                    data=body,
                    allow_redirects=False,
                    auto_decompress=False,
                ) as response,
            ):
                yield response

    @asynccontextmanager
    async def websocket(
        self,
        path: str,
        *,
        service: str | None = None,
        port: int | None = None,
        protocols: Iterable[str] = (),
    ) -> AsyncIterator[aiohttp.ClientWebSocketResponse]:
        """Open one WebSocket and yield it until the block ends.

        A refused handshake raises ``aiohttp.WSServerHandshakeError`` with the runner's status and headers.
        """
        url = self._url(path, service, port)
        with worker_call_in_flight(self._backend, self._worker_key):
            async with (
                self._session() as session,
                session.ws_connect(
                    url,
                    protocols=tuple(protocols),
                    headers=self._headers,
                    max_msg_size=_MAX_MESSAGE_BYTES,
                ) as upstream,
            ):
                yield upstream

    async def relay_messages(self, websocket: WebSocket, upstream: aiohttp.ClientWebSocketResponse) -> None:
        """Pass messages both ways between the route's accepted ``websocket`` and ``upstream`` until either closes."""
        await relay_websocket(websocket, upstream)

    async def ports(self) -> list[int]:
        """Return the ports the relay reaches: those servers the worker's own processes run listen on."""
        async with (
            self._session() as session,
            session.get(f"{self._root}/ports", headers=self._headers) as response,
        ):
            if response.status != 200:
                try:
                    detail = str((await response.json(content_type=None))["detail"])
                except (ValueError, KeyError, TypeError):
                    detail = f"HTTP {response.status}"
                if _REFUSED_HEADER in response.headers:
                    raise HTTPException(response.status, detail, headers={_REFUSED_HEADER: "1"})
                raise HTTPException(502, f"The agent's worker refused: {detail}")
            return await response.json()


def _open(paths: RuntimePaths, target: ResolvedWorkerTarget, plugin: str) -> WorkerRelay:
    """Return the relay into the target's dedicated worker while it runs, never starting it. Blocking."""
    if not worker_backend_kind(paths).dedicated:
        detail = (
            "The worker relay needs a dedicated worker backend: set MINDROOM_WORKER_BACKEND to docker or kubernetes."
        )
        raise HTTPException(503, detail)
    proxy_config = sandbox_proxy_config(paths)
    try:
        with lease_worker_manager(paths, proxy_config) as backend:
            _, handle = worker_route(
                runtime_paths=paths,
                # A config only fills the request body this builds, which the relay never sends.
                runtime_config=None,
                tool_name="worker relay",
                function_name="open",
                worker_target=target,
                worker_manager=backend,
                base_dir=None,
                start=False,
            )
    except RuntimeError as error:
        raise HTTPException(503, f"The agent's worker is unavailable: {error}") from None
    if handle is None or handle.status != "ready":
        raise HTTPException(503, "The agent's worker is not running.")
    headers = worker_request_headers(handle, config=worker_proxy_client_config(proxy_config, paths))
    return WorkerRelay(worker_api_root(handle), headers, plugin, backend, handle.worker_key)


async def open_worker_relay(paths: RuntimePaths, target: ResolvedWorkerTarget, plugin: str) -> WorkerRelay:
    """Return the relay into the target's running dedicated worker for ``plugin``, or answer 503."""
    return await asyncio.to_thread(_open, paths, target, plugin)
