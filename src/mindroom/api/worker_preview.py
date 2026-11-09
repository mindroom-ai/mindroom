"""Sandbox runner routes that relay the primary's requests to the apps a dedicated worker runs on its loopback.

``GET /api/sandbox-runner/ports`` lists the ports those apps listen on at loopback, and
``/api/sandbox-runner/ports/{port}/{path}`` passes a GET, HEAD, POST, PUT, PATCH, DELETE, or OPTIONS request, its body
streamed both ways, or a WebSocket, to ``{path}`` on the app at ``127.0.0.1:{port}``, or at ``[::1]:{port}`` when it
listens only there.
``/api/sandbox-runner/services/{plugin}/{name}/{path}`` relays the same way to the plugin's worker service while it
runs, adding the secret only that service knows and dropping any copy the caller sent; each request counts as use, as an
ensure does, and so does every half idle timeout a stream to it stays open.
Only a dedicated worker's runner relays: the shared runner's loopback is every agent's, and in a pod it is the primary's too.
A dedicated worker's loopback is not only the worker's either: every container of a Kubernetes worker pod shares it,
as does any container started in the worker's network namespace.
So the relay reaches a port only when the worker's own processes, those of its runner's process tree or its cgroup,
hold every socket listening on it, and never the runner's own port.
The runner marks each refusal with the ``x-mindroom-relay-refused`` header, on a WebSocket's refused handshake too: 403
for a port the relay never reaches, 502 for one no app answers on, and 503 for a service that is not running.
The runner's own token never reaches an app.
"""

from __future__ import annotations

import asyncio
import ipaddress
from concurrent.futures import ThreadPoolExecutor
from contextlib import AsyncExitStack, asynccontextmanager, nullcontext, suppress
from typing import TYPE_CHECKING

import aiohttp
from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from starlette.responses import JSONResponse, Response, StreamingResponse
from yarl import URL

from mindroom.api.sandbox_exec import runner_uses_dedicated_worker
from mindroom.api.sandbox_runner import app_runtime_paths, validate_runner_token
from mindroom.api.worker_services import RunningService, running_service
from mindroom.previews.listeners import listening_sockets, workers_sockets
from mindroom.previews.relay import HandshakeRedirected, no_redirects, relay_websocket

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterable
    from contextlib import AbstractAsyncContextManager

    from fastapi import FastAPI
    from starlette.requests import HTTPConnection
    from starlette.types import Receive, Scope, Send

router = APIRouter(prefix="/api/sandbox-runner", tags=["sandbox-runner"])

# Marks the runner's own refusals, so a caller tells them from an app's answers.
_REFUSED_HEADER = "x-mindroom-relay-refused"
# An app's headers the relay leaves out of its answer: the runner's server already sends its own Date and Server, which
# the answer keeps once, and only the runner marks its refusals.
_SERVER_HEADERS = frozenset({_REFUSED_HEADER, "date", "server"})
_TOKEN_HEADER = "x-mindroom-sandbox-token"  # noqa: S105 - a header name, not a secret
_SERVICE_SECRET_HEADER = "x-mindroom-service-secret"  # noqa: S105 - a header name, not a secret
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
# Headers aiohttp adds to a request that lacks them, which the relay leaves to the caller.
_AIOHTTP_DEFAULT_HEADERS = ("Accept", "User-Agent", "Accept-Encoding")
# A browser's handshake headers an app may read; the relay makes its own WebSocket handshake.
_WEBSOCKET_HEADERS = frozenset({"accept-language", "authorization", "cookie", "origin", "user-agent"})
# uvicorn's limit on the messages the relay receives from the primary, applied to the app's messages too.
_MAX_MESSAGE_BYTES = 16 * 1024 * 1024
# The addresses a socket listening at loopback is bound to: loopback itself, or every address.
_LOOPBACK_ADDRESSES = frozenset(
    ipaddress.ip_address(address)
    for address in ("127.0.0.1", "0.0.0.0", "::1", "::")  # noqa: S104 - every address, loopback included
)
_CANNOT_TELL = "This runner can't tell which processes hold its ports, so it relays to none."
# The listening sockets each port was last found to be the worker's by, so their holders are read again only once
# another socket listens there.
_found_workers_sockets: dict[int, frozenset[int]] = {}
# The relay's checks read /proc on a thread of their own, so tool calls busy on the loop's default threads never delay
# them; one thread also keeps the record above to one writer.
_CHECKS = ThreadPoolExecutor(max_workers=1, thread_name_prefix="worker-relay-checks")
# The relay reaches an app within this long, then waits on it as long as the stream lasts.
_CONNECT_SECONDS = 10
_TIMEOUT = aiohttp.ClientTimeout(total=None, sock_connect=_CONNECT_SECONDS)


def _refused(status_code: int, detail: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail=detail, headers={_REFUSED_HEADER: "1"})


def _no_app(port: int) -> HTTPException:
    return _refused(502, f"No app listens on port {port} at this worker's loopback.")


def _require_dedicated_worker(connection: HTTPConnection) -> None:
    if not runner_uses_dedicated_worker(app_runtime_paths(connection.app)):
        raise _refused(403, "Only a dedicated worker's runner relays to its ports.")


async def _loopback_host(port: int) -> str | None:
    """Return the loopback host in a URL that an app listening on ``port`` answers at, or None when none answers."""
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


def _workers_ports(listening: dict[int, frozenset[int]]) -> list[int]:
    """Return the ports in ``listening`` whose listening sockets, its values, only the worker's processes hold.

    Blocking: reads in one pass which processes hold the sockets of the ports not last found to be the worker's by them.
    Raises OSError when ``/proc`` can't say.
    """
    unseen = {port: sockets for port, sockets in listening.items() if _found_workers_sockets.get(port) != sockets}
    if unseen:
        workers = workers_sockets(frozenset().union(*unseen.values()))
        _found_workers_sockets.update((port, sockets) for port, sockets in unseen.items() if sockets <= workers)
    return sorted(port for port, sockets in listening.items() if _found_workers_sockets.get(port) == sockets)


def _check_reachable(port: int, runner_port: int) -> None:
    """Raise the relay's refusal unless a server of the worker's own listens on ``port``. Blocking."""
    if port == runner_port:
        raise _refused(403, f"Port {port} is this runner's own, which the relay never reaches.")
    try:
        sockets = frozenset(listener.inode for listener in listening_sockets() if listener.port == port)
        held = bool(sockets) and port in _workers_ports({port: sockets})
    except OSError:
        raise _refused(403, _CANNOT_TELL) from None
    if not sockets:
        raise _no_app(port)
    if not held:
        raise _refused(403, f"The server on port {port} is not one this worker runs.")


async def _upstream_url(connection: HTTPConnection, port: int, segments: int) -> URL:
    """Return the URL of the app on ``port`` that ``connection`` asks for, below its route's first ``segments``."""
    _require_dedicated_worker(connection)
    await asyncio.get_running_loop().run_in_executor(_CHECKS, _check_reachable, port, connection.scope["server"][1])
    host = await _loopback_host(port)
    if host is None:
        raise _no_app(port)
    # The path below the route's own segments as the primary sent it, still percent-encoded.
    raw_path = connection.scope["raw_path"].decode("latin-1").removeprefix(f"{router.prefix}/")
    target = raw_path.split("/", segments)[segments]
    query = connection.scope["query_string"].decode("latin-1")
    return URL(f"http://{host}:{port}/{target}{'?' if query else ''}{query}", encoded=True)


def _end_to_end(headers: Iterable[tuple[str, str]], drop: frozenset[str]) -> list[tuple[str, str]]:
    """Return ``headers`` without the hop-by-hop ones, those their Connection header names, and ``drop``."""
    pairs = [(name.lower(), value) for name, value in headers]
    named = {token.strip().lower() for name, value in pairs if name == "connection" for token in value.split(",")}
    excluded = _HOP_HEADERS | named | drop
    return [(name, value) for name, value in pairs if name not in excluded]


def _reachable_ports(runner_port: int) -> list[int]:
    """Return each port listening at loopback or every address that the relay reaches. Blocking."""
    try:
        listeners = listening_sockets()
        sockets: dict[int, set[int]] = {}
        for listener in listeners:
            sockets.setdefault(listener.port, set()).add(listener.inode)
        ports = {listener.port for listener in listeners if listener.address in _LOOPBACK_ADDRESSES} - {runner_port}
        return _workers_ports({port: frozenset(sockets[port]) for port in ports})
    except OSError:
        raise _refused(403, _CANNOT_TELL) from None


class _AppResponse(StreamingResponse):
    """An app's answer, streamed as it arrives, whose connection, and what ``held`` keeps, close however it ends."""

    def __init__(self, upstream: aiohttp.ClientResponse, held: AsyncExitStack) -> None:
        super().__init__(upstream.content.iter_any(), status_code=upstream.status)
        self.raw_headers = [
            (name.encode("latin-1"), value.encode("latin-1"))
            for name, value in _end_to_end(
                ((name.decode("latin-1"), value.decode("latin-1")) for name, value in upstream.raw_headers),
                _SERVER_HEADERS,
            )
        ]
        self._upstream = upstream
        self._held = held

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Stream the answer, then close the app connection."""
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._upstream.close()
            await self._held.aclose()


def _require_service(app: FastAPI, plugin: str, name: str) -> RunningService:
    if (service := running_service(app, plugin, name)) is None:
        msg = f"Service {name!r} of plugin {plugin!r} is not running in this worker."
        raise _refused(503, msg)
    return service


@asynccontextmanager
async def _service_in_use(app: FastAPI, plugin: str, name: str, idle_timeout_seconds: float) -> AsyncIterator[None]:
    """Count the service as used every half idle timeout while a stream to it lasts."""

    async def use() -> None:
        # A periodic use, not a wait for a condition.
        while running_service(app, plugin, name) is not None:  # noqa: ASYNC110
            await asyncio.sleep(idle_timeout_seconds / 2)

    task = asyncio.create_task(use(), name="worker-service-in-use")
    try:
        yield
    finally:
        task.cancel()


async def _relay(
    request: Request,
    url: URL,
    secret: str | None,
    in_use: AbstractAsyncContextManager[None],
) -> StreamingResponse:
    """Pass the request to ``url``, adding the service's ``secret``, and stream its answer back."""
    has_body = request.headers.get("content-length", "0") != "0" or "transfer-encoding" in request.headers
    headers = _end_to_end(request.headers.items(), frozenset({"host", _TOKEN_HEADER, _SERVICE_SECRET_HEADER}))
    if secret is not None:
        headers.append((_SERVICE_SECRET_HEADER, secret))
    held = AsyncExitStack()
    await held.enter_async_context(in_use)
    session = await held.enter_async_context(aiohttp.ClientSession(auto_decompress=False, timeout=_TIMEOUT))
    try:
        upstream = await session.request(
            request.method,
            url,
            headers=headers,
            data=request.stream() if has_body else None,
            allow_redirects=False,
            skip_auto_headers=(*_AIOHTTP_DEFAULT_HEADERS, "Content-Type"),
        )
    except (aiohttp.ClientError, TimeoutError, OSError):
        await held.aclose()
        raise _refused(502, f"The app on port {url.port} did not answer.") from None
    return _AppResponse(upstream, held)


async def _send_denial(websocket: WebSocket, response: Response) -> None:
    """Answer the handshake with ``response`` instead of accepting it."""
    # The server frames the denial itself, and some of its WebSocket implementations add these after the app's own.
    del response.headers["content-length"]
    del response.headers["content-type"]
    await websocket.send_denial_response(response)


async def _deny(websocket: WebSocket, refusal: HTTPException) -> None:
    """Refuse the handshake with the refusal's status, detail, and headers, as an HTTP answer carries them."""
    response = JSONResponse({"detail": refusal.detail}, status_code=refusal.status_code, headers=refusal.headers)
    await _send_denial(websocket, response)


async def _hand_back(websocket: WebSocket, redirect: HandshakeRedirected) -> None:
    """Answer the handshake with the app's redirect, its status and headers, which the relay never follows."""
    response = Response(status_code=redirect.status)
    for name, value in _end_to_end((redirect.headers or {}).items(), _SERVER_HEADERS):
        response.headers.append(name, value)
    await _send_denial(websocket, response)


async def _relay_socket(
    websocket: WebSocket,
    url: URL,
    secret: str | None,
    in_use: AbstractAsyncContextManager[None],
) -> None:
    """Pass the WebSocket to ``url``, adding the service's ``secret``, message by message."""
    protocols = [name.strip() for offer in websocket.scope["subprotocols"] for name in offer.split(",") if name.strip()]
    headers = [(name, value) for name, value in websocket.headers.items() if name in _WEBSOCKET_HEADERS]
    if secret is not None:
        headers.append((_SERVICE_SECRET_HEADER, secret))
    try:
        async with (
            aiohttp.ClientSession(
                timeout=_TIMEOUT,
                middlewares=(no_redirects,),
                skip_auto_headers=_AIOHTTP_DEFAULT_HEADERS,
            ) as session,
            session.ws_connect(url, protocols=protocols, headers=headers, max_msg_size=_MAX_MESSAGE_BYTES) as upstream,
            in_use,
        ):
            await websocket.accept(subprotocol=upstream.protocol)
            await relay_websocket(websocket, upstream)
    except HandshakeRedirected as redirect:
        await _hand_back(websocket, redirect)
    except (aiohttp.ClientError, TimeoutError, OSError):
        with suppress(RuntimeError, OSError):
            await websocket.close(code=1011)


@router.get("/ports", dependencies=[Depends(validate_runner_token)])
async def listening_ports(request: Request) -> list[int]:
    """List the ports the worker's own apps listen on at its loopback: those the relay reaches."""
    _require_dedicated_worker(request)
    return await asyncio.get_running_loop().run_in_executor(_CHECKS, _reachable_ports, request.scope["server"][1])


@router.api_route("/ports/{port}/{path:path}", methods=_METHODS, dependencies=[Depends(validate_runner_token)])
async def relay(request: Request, port: int) -> StreamingResponse:
    """Pass one request to the app on ``port`` at this worker's loopback and stream its answer back."""
    url = await _upstream_url(request, port, 2)
    return await _relay(request, url, None, nullcontext())


@router.api_route(
    "/services/{plugin}/{name}/{path:path}",
    methods=_METHODS,
    dependencies=[Depends(validate_runner_token)],
)
async def relay_to_service(request: Request, plugin: str, name: str) -> StreamingResponse:
    """Pass one request to the plugin's running service and stream its answer back."""
    _require_dedicated_worker(request)
    service = _require_service(request.app, plugin, name)
    url = await _upstream_url(request, service.port, 3)
    in_use = _service_in_use(request.app, plugin, name, service.idle_timeout_seconds)
    return await _relay(request, url, service.secret, in_use)


@router.websocket("/ports/{port}/{path:path}")
async def relay_socket(websocket: WebSocket, port: int) -> None:
    """Pass one WebSocket to the app on ``port`` at this worker's loopback, message by message."""
    try:
        await validate_runner_token(websocket, websocket.headers.get(_TOKEN_HEADER))
        url = await _upstream_url(websocket, port, 2)
    except HTTPException as refusal:
        await _deny(websocket, refusal)
        return
    await _relay_socket(websocket, url, None, nullcontext())


@router.websocket("/services/{plugin}/{name}/{path:path}")
async def relay_socket_to_service(websocket: WebSocket, plugin: str, name: str) -> None:
    """Pass one WebSocket to the plugin's running service, message by message, as long as it lasts."""
    try:
        await validate_runner_token(websocket, websocket.headers.get(_TOKEN_HEADER))
        _require_dedicated_worker(websocket)
        service = _require_service(websocket.app, plugin, name)
        url = await _upstream_url(websocket, service.port, 3)
    except HTTPException as refusal:
        await _deny(websocket, refusal)
        return
    in_use = _service_in_use(websocket.app, plugin, name, service.idle_timeout_seconds)
    await _relay_socket(websocket, url, service.secret, in_use)
