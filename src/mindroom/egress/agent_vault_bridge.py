"""Forward proxy adapter that hides Agent Vault proxy sessions from workers."""

from __future__ import annotations

import argparse
import http.client
import select
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Self
from urllib.parse import urlsplit

if TYPE_CHECKING:
    import socket
    from collections.abc import Iterable

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
_TUNNEL_IDLE_TIMEOUT_SECONDS = 30


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
    def log_message(self, _format: str, *_args: object) -> None:
        return


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
    connection = http.client.HTTPConnection(proxy_host, proxy_port, timeout=10)
    try:
        connection.request(
            "CONNECT",
            handler.path,
            headers={
                "Host": handler.path,
                "Proxy-Authorization": proxy_authorization,
            },
        )
        response = connection.getresponse()
        if response.status != 200:
            _copy_response(handler, response)
            return

        handler.send_response(200, response.reason)
        handler.end_headers()
        upstream_sock = connection.sock
        if upstream_sock is None:
            return
        _tunnel_sockets(handler.connection, upstream_sock)
    except (OSError, TimeoutError, http.client.HTTPException) as exc:
        handler.send_error(502, f"Bad Gateway: {exc}")
    finally:
        connection.close()


def _read_request_body(handler: BaseHTTPRequestHandler) -> bytes | None:
    transfer_encoding = handler.headers.get("Transfer-Encoding", "")
    if "chunked" in transfer_encoding.lower():
        return _read_chunked_request_body(handler)

    raw_length = handler.headers.get("Content-Length")
    if raw_length is None:
        return None
    try:
        length = int(raw_length)
    except ValueError:
        return None
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
    for key, value in items:
        if key.lower() in _HOP_BY_HOP_HEADERS:
            continue
        headers[key] = value
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

    while sockets:
        readable, _, errored = select.select(sockets, [], sockets, _TUNNEL_IDLE_TIMEOUT_SECONDS)
        if errored or not readable:
            return
        for source in readable:
            target = upstream_sock if source is client_sock else client_sock
            try:
                chunk = source.recv(_TUNNEL_BUFFER_BYTES)
            except OSError:
                return
            if not chunk:
                return
            try:
                target.sendall(chunk)
            except OSError:
                return


def main() -> None:
    """Run the adapter process."""
    parser = argparse.ArgumentParser(description="Run Agent Vault forward proxy adapter.")
    parser.add_argument("--host", default="0.0.0.0")  # noqa: S104
    parser.add_argument("--port", type=int, default=18080)
    parser.add_argument("--upstream-proxy-url", required=True)
    parser.add_argument("--session-token", required=True)
    args = parser.parse_args()

    with start_adapter(
        host=args.host,
        port=args.port,
        upstream_proxy_url=args.upstream_proxy_url,
        session_token=args.session_token,
    ) as adapter:
        adapter.thread.join()


if __name__ == "__main__":
    main()
