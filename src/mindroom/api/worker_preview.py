"""Sandbox runner routes that relay the primary's requests to apps listening on this worker's loopback.

``GET /api/sandbox-runner/ports`` lists the ports apps listen on at loopback, and ``/api/sandbox-runner/ports/{port}/{path}``
passes a GET, HEAD, POST, PUT, PATCH, DELETE, or OPTIONS request, its body streamed both ways, or a WebSocket, to
``{path}`` on the app at ``127.0.0.1:{port}``, or at ``[::1]:{port}`` when it listens only there.
Only a dedicated worker's runner relays: the shared runner's loopback is every agent's, and in a pod it is the primary's too.
The runner's own token never reaches an app.
"""

from __future__ import annotations

import asyncio
import ipaddress
import sys
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING

import aiohttp
from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from starlette.responses import StreamingResponse
from yarl import URL

from mindroom.api.sandbox_exec import runner_uses_dedicated_worker
from mindroom.api.sandbox_runner import app_runtime_paths, validate_runner_token
from mindroom.previews.relay import relay_websocket

if TYPE_CHECKING:
    from collections.abc import Iterable

    from starlette.requests import HTTPConnection
    from starlette.types import Receive, Scope, Send

router = APIRouter(prefix="/api/sandbox-runner/ports", tags=["sandbox-runner"])

# Marks the runner's own refusals, so a caller tells them from an app's answers.
_REFUSED_HEADER = "x-mindroom-relay-refused"
_TOKEN_HEADER = "x-mindroom-sandbox-token"  # noqa: S105 - a header name, not a secret
_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
# Hop-by-hop headers (RFC 9110 7.6.1) belong to one connection, so the relay passes them on in neither direction.
_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    },
)
# A browser's handshake headers an app may read; the relay makes its own WebSocket handshake.
_WEBSOCKET_HEADERS = frozenset({"accept-language", "authorization", "cookie", "origin", "user-agent"})
# uvicorn's limit on the messages the relay receives from the primary, applied to the app's messages too.
_MAX_MESSAGE_BYTES = 16 * 1024 * 1024
# The addresses a socket listening at loopback is bound to: loopback itself, or every address.
_LOOPBACK_ADDRESSES = frozenset(
    ipaddress.ip_address(address)
    for address in ("127.0.0.1", "0.0.0.0", "::1", "::")  # noqa: S104 - every address, loopback included
)
_TCP_LISTEN = "0A"
# The relay reaches an app within this long, then waits on it as long as the stream lasts.
_CONNECT_SECONDS = 10
_TIMEOUT = aiohttp.ClientTimeout(total=None, sock_connect=_CONNECT_SECONDS)


def _refused(status_code: int, detail: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail=detail, headers={_REFUSED_HEADER: "1"})


def _address(hex_words: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Decode a /proc/net address: 32-bit words in hex, each in host byte order; an IPv4-mapped one as IPv4."""
    words = (int(hex_words[start : start + 8], 16) for start in range(0, len(hex_words), 8))
    address = ipaddress.ip_address(b"".join(word.to_bytes(4, sys.byteorder) for word in words))
    # A dual-stack socket bound to an IPv4 address, as Java binds localhost, shows in tcp6 as ::ffff:<address>.
    return (address.ipv4_mapped or address) if isinstance(address, ipaddress.IPv6Address) else address


def _loopback_ports() -> set[int]:
    """Return each port a TCP socket listens on at loopback or every address."""
    ports: set[int] = set()
    for table in ("tcp", "tcp6"):
        try:
            lines = Path("/proc/net", table).read_text(encoding="ascii").splitlines()[1:]
        except OSError:  # No IPv6 in this network namespace.
            continue
        for line in lines:
            _, local, _, state, *_ = line.split()
            hex_address, _, hex_port = local.partition(":")
            if state == _TCP_LISTEN and _address(hex_address) in _LOOPBACK_ADDRESSES:
                ports.add(int(hex_port, 16))
    return ports


def _require_dedicated_worker(connection: HTTPConnection) -> None:
    if not runner_uses_dedicated_worker(app_runtime_paths(connection.app)):
        raise _refused(403, "Only a dedicated worker's runner relays to its ports.")


async def _loopback_host(port: int) -> str | None:
    """Return the loopback host in a URL that an app listening on ``port`` answers at, or None when none answers.

    Connecting costs far less than reading /proc/net, whose size grows with every connection the relay closes.
    """
    for host in ("127.0.0.1", "::1"):
        try:
            async with asyncio.timeout(_CONNECT_SECONDS):
                _, writer = await asyncio.open_connection(host, port)
        except (OSError, TimeoutError):
            continue
        writer.close()
        with suppress(OSError):
            await writer.wait_closed()
        return f"[{host}]" if ":" in host else host
    return None


async def _loopback_only(request: aiohttp.ClientRequest, handler: aiohttp.ClientHandlerType) -> aiohttp.ClientResponse:
    """Send ``request`` only to loopback: aiohttp follows a WebSocket handshake's redirects, wherever they point."""
    if request.url.host not in {"127.0.0.1", "::1"}:
        raise aiohttp.InvalidURL(request.url, "The relay reaches only loopback.")
    return await handler(request)


async def _upstream_url(connection: HTTPConnection, port: int) -> URL:
    """Return the URL of the app on ``port`` that ``connection`` asks for."""
    _require_dedicated_worker(connection)
    own = port == connection.scope["server"][1]
    host = None if own or not 0 < port < 65536 else await _loopback_host(port)
    if host is None:
        raise _refused(502, f"No app listens on port {port} at this worker's loopback.")
    # The path below the port as the primary sent it, still percent-encoded.
    target = connection.scope["raw_path"].decode("latin-1").removeprefix(f"{router.prefix}/").partition("/")[2]
    query = connection.scope["query_string"].decode("latin-1")
    return URL(f"http://{host}:{port}/{target}{'?' if query else ''}{query}", encoded=True)


def _end_to_end(headers: Iterable[tuple[str, str]], drop: frozenset[str]) -> list[tuple[str, str]]:
    """Return ``headers`` without the hop-by-hop ones, those their Connection header names, and ``drop``."""
    pairs = [(name.lower(), value) for name, value in headers]
    named = {token.strip().lower() for name, value in pairs if name == "connection" for token in value.split(",")}
    excluded = _HOP_HEADERS | named | drop
    return [(name, value) for name, value in pairs if name not in excluded]


class _AppResponse(StreamingResponse):
    """An app's answer, streamed as it arrives, whose connection closes however the stream ends."""

    def __init__(self, upstream: aiohttp.ClientResponse, session: aiohttp.ClientSession) -> None:
        super().__init__(upstream.content.iter_any(), status_code=upstream.status)
        self.raw_headers = [
            (name.encode("latin-1"), value.encode("latin-1"))
            for name, value in _end_to_end(
                ((name.decode("latin-1"), value.decode("latin-1")) for name, value in upstream.raw_headers),
                # The runner's server already sends its own Date and Server, which the answer keeps once.
                frozenset({_REFUSED_HEADER, "date", "server"}),
            )
        ]
        self._upstream = upstream
        self._session = session

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Stream the answer, then close the app connection."""
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._upstream.close()
            await self._session.close()


@router.get("", dependencies=[Depends(validate_runner_token)])
async def listening_ports(request: Request) -> list[int]:
    """List the ports apps listen on at this worker's loopback, which the relay reaches; the runner's own is not one."""
    _require_dedicated_worker(request)
    return sorted(_loopback_ports() - {request.scope["server"][1]})


@router.api_route("/{port}/{path:path}", methods=_METHODS, dependencies=[Depends(validate_runner_token)])
async def relay(request: Request, port: int) -> StreamingResponse:
    """Pass one request to the app on ``port`` at this worker's loopback and stream its answer back."""
    url = await _upstream_url(request, port)
    has_body = request.headers.get("content-length", "0") != "0" or "transfer-encoding" in request.headers
    session = aiohttp.ClientSession(auto_decompress=False, timeout=_TIMEOUT)
    try:
        upstream = await session.request(
            request.method,
            url,
            headers=_end_to_end(request.headers.items(), frozenset({"host", _TOKEN_HEADER})),
            data=request.stream() if has_body else None,
            allow_redirects=False,
            skip_auto_headers=("Accept", "User-Agent", "Accept-Encoding", "Content-Type"),
        )
    except (aiohttp.ClientError, TimeoutError, OSError):
        await session.close()
        raise _refused(502, f"The app on port {port} did not answer.") from None
    return _AppResponse(upstream, session)


@router.websocket("/{port}/{path:path}")
async def relay_socket(websocket: WebSocket, port: int) -> None:
    """Pass one WebSocket to the app on ``port`` at this worker's loopback, message by message."""
    try:
        await validate_runner_token(websocket, websocket.headers.get(_TOKEN_HEADER))
        url = await _upstream_url(websocket, port)
    except HTTPException:
        await websocket.close(code=1008)
        return
    protocols = [name.strip() for offer in websocket.scope["subprotocols"] for name in offer.split(",") if name.strip()]
    headers = [(name, value) for name, value in websocket.headers.items() if name in _WEBSOCKET_HEADERS]
    try:
        async with (
            aiohttp.ClientSession(timeout=_TIMEOUT, middlewares=(_loopback_only,)) as session,
            session.ws_connect(url, protocols=protocols, headers=headers, max_msg_size=_MAX_MESSAGE_BYTES) as upstream,
        ):
            await websocket.accept(subprotocol=upstream.protocol)
            await relay_websocket(websocket, upstream)
    except (aiohttp.ClientError, TimeoutError, OSError):
        with suppress(RuntimeError, OSError):
            await websocket.close(code=1011)
