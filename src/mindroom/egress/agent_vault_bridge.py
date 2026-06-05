"""Forward proxy adapter that hides Agent Vault proxy sessions from workers."""

from __future__ import annotations

import argparse
import http.client
import os
import selectors
import socket
import threading
from contextlib import suppress
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Self, cast
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from collections.abc import Iterable

__all__ = ["RunningAdapter", "start_adapter"]

_HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}
_TUNNEL_BUFFER_BYTES = 64 * 1024
_TUNNEL_BACKPRESSURE_LIMIT_BYTES = 1024 * 1024
_TUNNEL_BACKPRESSURE_RESUME_BYTES = _TUNNEL_BACKPRESSURE_LIMIT_BYTES // 2
_TUNNEL_IDLE_TIMEOUT_SECONDS = 30
_UPSTREAM_AUTH_FAILED_MESSAGE = "Bad Gateway: upstream proxy authentication failed"


@dataclass(slots=True)
class RunningAdapter:
    """Started adapter server handle."""

    httpd: ThreadingHTTPServer
    thread: threading.Thread

    def __enter__(self) -> Self:
        """Return this running adapter for context-manager use."""
        return self

    def __exit__(self, *_exc: object) -> None:
        """Shutdown the adapter and wait briefly for its thread to stop."""
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    @property
    def host(self) -> str:
        """Return the bound host."""
        return str(self.httpd.server_address[0])

    @property
    def port(self) -> int:
        """Return the bound port."""
        return int(self.httpd.server_address[1])

    @property
    def proxy_url(self) -> str:
        """Return the adapter proxy URL."""
        return f"http://{self.host}:{self.port}"


class _QuietHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        del format, args


def start_adapter(  # noqa: C901
    *,
    upstream_proxy_url: str,
    session_token: str,
    host: str = "127.0.0.1",
    port: int = 0,
) -> RunningAdapter:
    """Start an HTTP proxy that injects Proxy-Authorization upstream."""
    if not session_token:
        msg = "session_token is required"
        raise ValueError(msg)
    upstream = urlsplit(upstream_proxy_url)
    if upstream.scheme != "http" or not upstream.hostname:
        msg = "upstream_proxy_url must be an http://host:port URL"
        raise ValueError(msg)
    upstream_port = upstream.port or 80

    def proxy_authorization() -> str:
        return f"Bearer {session_token}"

    class AgentVaultAdapterHandler(_QuietHandler):
        def do_CONNECT(self) -> None:
            self.close_connection = True
            _forward_connect(
                self,
                proxy_host=upstream.hostname or "",
                proxy_port=upstream_port,
                proxy_authorization=proxy_authorization(),
            )

        def do_DELETE(self) -> None:
            self._forward_request()

        def do_GET(self) -> None:
            self._forward_request()

        def do_HEAD(self) -> None:
            self._forward_request()

        def do_OPTIONS(self) -> None:
            self._forward_request()

        def do_PATCH(self) -> None:
            self._forward_request()

        def do_POST(self) -> None:
            self._forward_request()

        def do_PUT(self) -> None:
            self._forward_request()

        def _forward_request(self) -> None:
            _forward_http_request(
                self,
                proxy_host=upstream.hostname or "",
                proxy_port=upstream_port,
                proxy_authorization=proxy_authorization(),
            )

    httpd = ThreadingHTTPServer((host, port), AgentVaultAdapterHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return RunningAdapter(httpd=httpd, thread=thread)


def _forward_http_request(
    handler: BaseHTTPRequestHandler,
    *,
    proxy_host: str,
    proxy_port: int,
    proxy_authorization: str,
) -> None:
    try:
        body = _read_request_body(handler)
    except ValueError as exc:
        handler.send_error(400, str(exc))
        return
    headers = _forward_headers(handler.headers.items(), proxy_authorization=proxy_authorization)
    if body is not None:
        headers["Content-Length"] = str(len(body))
    connection = http.client.HTTPConnection(proxy_host, proxy_port, timeout=10)
    try:
        connection.request(handler.command, handler.path, body=body, headers=headers)
        response = connection.getresponse()
        if response.status == 407:
            handler.send_error(502, _UPSTREAM_AUTH_FAILED_MESSAGE)
            return
        _copy_response(handler, response)
    except (OSError, TimeoutError, http.client.HTTPException) as exc:
        handler.send_error(502, f"Bad Gateway: {exc}")
    finally:
        connection.close()


def _forward_connect(
    handler: BaseHTTPRequestHandler,
    *,
    proxy_host: str,
    proxy_port: int,
    proxy_authorization: str,
) -> None:
    upstream_sock: socket.socket | None = None
    try:
        try:
            upstream_sock = socket.create_connection((proxy_host, proxy_port), timeout=10)
            connect_request = (
                f"CONNECT {handler.path} HTTP/1.1\r\n"
                f"Host: {handler.path}\r\n"
                f"Proxy-Authorization: {proxy_authorization}\r\n"
                "\r\n"
            )
            upstream_sock.sendall(connect_request.encode("iso-8859-1"))
            response = http.client.HTTPResponse(upstream_sock)
            response.begin()
            if response.status == 407:
                handler.send_error(502, _UPSTREAM_AUTH_FAILED_MESSAGE)
                return
            if response.status != 200:
                _copy_response(handler, response)
                return

            handler.send_response(200, response.reason)
            handler.end_headers()
        except (OSError, TimeoutError, http.client.HTTPException) as exc:
            handler.send_error(502, f"Bad Gateway: {exc}")
            return

        try:
            _tunnel_sockets(handler.connection, upstream_sock)
        except OSError:
            return
    finally:
        if upstream_sock is not None:
            upstream_sock.close()


def _read_request_body(handler: BaseHTTPRequestHandler) -> bytes | None:
    transfer_encoding = handler.headers.get("Transfer-Encoding", "")
    if "chunked" in transfer_encoding.lower():
        return _read_chunked_request_body(handler)

    raw_length = handler.headers.get("Content-Length")
    if raw_length is None:
        return None
    try:
        length = int(raw_length)
    except ValueError as exc:
        msg = f"Invalid Content-Length: {raw_length}"
        raise ValueError(msg) from exc
    if length <= 0:
        return None
    return handler.rfile.read(length)


def _read_chunked_request_body(handler: BaseHTTPRequestHandler) -> bytes:
    chunks: list[bytes] = []
    while True:
        size_line = handler.rfile.readline()
        if not size_line:
            msg = "Malformed chunked request body"
            raise ValueError(msg)
        raw_size = size_line.split(b";", 1)[0].strip()
        try:
            size = int(raw_size, 16)
        except ValueError as exc:
            msg = "Malformed chunked request body"
            raise ValueError(msg) from exc
        if size < 0:
            msg = "Negative chunk size"
            raise ValueError(msg)
        if size == 0:
            _drain_chunked_trailers(handler)
            return b"".join(chunks)

        chunk = handler.rfile.read(size)
        if len(chunk) != size:
            msg = "Incomplete chunked request body"
            raise ValueError(msg)
        chunks.append(chunk)
        terminator = handler.rfile.read(2)
        if terminator != b"\r\n":
            msg = "Malformed chunked request body"
            raise ValueError(msg)


def _drain_chunked_trailers(handler: BaseHTTPRequestHandler) -> None:
    while True:
        line = handler.rfile.readline()
        if line in {b"", b"\n", b"\r\n"}:
            return


def _forward_headers(
    items: Iterable[tuple[str, str]],
    *,
    proxy_authorization: str,
) -> dict[str, str]:
    headers: dict[str, str] = {}
    header_names: dict[str, str] = {}
    for key, value in items:
        normalized_key = key.lower()
        if normalized_key in _HOP_BY_HOP_HEADERS:
            continue
        existing_key = header_names.get(normalized_key)
        if existing_key is None:
            header_names[normalized_key] = key
            headers[key] = value
        else:
            headers[existing_key] = f"{headers[existing_key]}, {value}"
    headers["Proxy-Authorization"] = proxy_authorization
    return headers


def _copy_response(handler: BaseHTTPRequestHandler, response: http.client.HTTPResponse) -> None:
    body = response.read()
    handler.send_response(response.status, response.reason)
    for key, value in response.getheaders():
        if key.lower() in _HOP_BY_HOP_HEADERS:
            continue
        handler.send_header(key, value)
    handler.end_headers()
    if body:
        handler.wfile.write(body)


def _tunnel_sockets(client_sock: socket.socket, upstream_sock: socket.socket) -> None:
    sockets = [client_sock, upstream_sock]
    for sock in sockets:
        sock.setblocking(False)

    peers = {
        client_sock: upstream_sock,
        upstream_sock: client_sock,
    }
    pending_writes = {
        client_sock: bytearray(),
        upstream_sock: bytearray(),
    }
    read_enabled = {
        client_sock: True,
        upstream_sock: True,
    }
    read_closed: set[socket.socket] = set()

    try:
        with selectors.DefaultSelector() as selector:
            registered: set[socket.socket] = set()

            for sock in sockets:
                _refresh_tunnel_interest(selector, registered, sock, read_enabled, pending_writes)

            while selector.get_map():
                ready = selector.select(_TUNNEL_IDLE_TIMEOUT_SECONDS)
                if not ready:
                    return

                for key, mask in ready:
                    sock = cast("socket.socket", key.fileobj)
                    if mask & selectors.EVENT_READ and not _handle_tunnel_read(
                        selector,
                        registered,
                        sock,
                        peers,
                        pending_writes,
                        read_enabled,
                        read_closed,
                    ):
                        return
                    if mask & selectors.EVENT_WRITE and not _handle_tunnel_write(
                        selector,
                        registered,
                        sock,
                        peers,
                        pending_writes,
                        read_enabled,
                        read_closed,
                    ):
                        return
    finally:
        for sock in sockets:
            with suppress(OSError):
                sock.setblocking(True)


def _refresh_tunnel_interest(
    selector: selectors.BaseSelector,
    registered: set[socket.socket],
    sock: socket.socket,
    read_enabled: dict[socket.socket, bool],
    pending_writes: dict[socket.socket, bytearray],
) -> None:
    events = 0
    if read_enabled[sock]:
        events |= selectors.EVENT_READ
    if pending_writes[sock]:
        events |= selectors.EVENT_WRITE

    if sock in registered:
        if events:
            selector.modify(sock, events)
        else:
            selector.unregister(sock)
            registered.remove(sock)
    elif events:
        selector.register(sock, events)
        registered.add(sock)


def _handle_tunnel_read(
    selector: selectors.BaseSelector,
    registered: set[socket.socket],
    sock: socket.socket,
    peers: dict[socket.socket, socket.socket],
    pending_writes: dict[socket.socket, bytearray],
    read_enabled: dict[socket.socket, bool],
    read_closed: set[socket.socket],
) -> bool:
    target = peers[sock]
    try:
        chunk = sock.recv(_TUNNEL_BUFFER_BYTES)
    except BlockingIOError:
        return True
    except OSError:
        return False

    if not chunk:
        read_enabled[sock] = False
        read_closed.add(sock)
        _refresh_tunnel_interest(selector, registered, sock, read_enabled, pending_writes)
        return bool(pending_writes[target])

    pending_writes[target].extend(chunk)
    if len(pending_writes[target]) >= _TUNNEL_BACKPRESSURE_LIMIT_BYTES:
        read_enabled[sock] = False
    _refresh_tunnel_interest(selector, registered, target, read_enabled, pending_writes)
    _refresh_tunnel_interest(selector, registered, sock, read_enabled, pending_writes)
    return True


def _handle_tunnel_write(
    selector: selectors.BaseSelector,
    registered: set[socket.socket],
    sock: socket.socket,
    peers: dict[socket.socket, socket.socket],
    pending_writes: dict[socket.socket, bytearray],
    read_enabled: dict[socket.socket, bool],
    read_closed: set[socket.socket],
) -> bool:
    pending = pending_writes[sock]
    if not pending:
        return True
    try:
        sent = sock.send(pending)
    except BlockingIOError:
        return True
    except OSError:
        return False
    if sent == 0:
        return False

    del pending[:sent]
    source = peers[sock]
    if source not in read_closed and len(pending) <= _TUNNEL_BACKPRESSURE_RESUME_BYTES:
        read_enabled[source] = True
    _refresh_tunnel_interest(selector, registered, sock, read_enabled, pending_writes)
    _refresh_tunnel_interest(selector, registered, source, read_enabled, pending_writes)
    return source not in read_closed or bool(pending)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run Agent Vault forward proxy adapter.",
        allow_abbrev=False,
    )
    parser.add_argument("--host", default="0.0.0.0")  # noqa: S104
    parser.add_argument("--port", type=int, default=18080)
    parser.add_argument("--upstream-proxy-url", required=True)
    parser.add_argument("--session-token-env", default="AGENT_VAULT_PROXY_SESSION_TOKEN")
    return parser.parse_args(argv)


def _session_token_from_env(env_var: str) -> str:
    session_token = os.environ.get(env_var)
    if not session_token:
        msg = f"{env_var} environment variable must be set"
        raise ValueError(msg)
    return session_token


def _main() -> None:
    """Run the adapter process."""
    args = _parse_args()
    try:
        session_token = _session_token_from_env(args.session_token_env)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None

    with start_adapter(
        host=args.host,
        port=args.port,
        upstream_proxy_url=args.upstream_proxy_url,
        session_token=session_token,
    ) as adapter:
        adapter.thread.join()


if __name__ == "__main__":
    _main()
