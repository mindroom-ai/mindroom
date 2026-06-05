"""Forward proxy adapter that hides Agent Vault proxy sessions from workers."""

from __future__ import annotations

import argparse
import http.client
import os
import socket
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Protocol, Self, cast
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
_TUNNEL_IDLE_TIMEOUT_SECONDS = 30
_HTTP_STREAM_CHUNK_BYTES = 64 * 1024
_CONNECT_RESPONSE_HEADER_LIMIT_BYTES = 64 * 1024
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


@dataclass(slots=True)
class _ConnectResponse:
    status: int
    reason: str
    headers: list[tuple[str, str]]
    leftover: bytes


@dataclass(slots=True)
class _TunnelActivity:
    at: float


class _PeekableReader(Protocol):
    def peek(self, size: int = 0, /) -> bytes: ...

    def read(self, size: int = -1, /) -> bytes: ...


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
        content_length = _request_content_length(handler)
    except ValueError as exc:
        handler.send_error(400, str(exc))
        return
    is_chunked = "chunked" in handler.headers.get("Transfer-Encoding", "").lower()
    expects_continue = handler.headers.get("Expect", "").lower() == "100-continue"
    headers = _forward_headers(handler.headers.items(), proxy_authorization=proxy_authorization)
    if expects_continue:
        _remove_header(headers, "expect")
    if is_chunked:
        _remove_header(headers, "content-length")
        headers["Transfer-Encoding"] = "chunked"
    connection = http.client.HTTPConnection(proxy_host, proxy_port, timeout=10)
    try:
        if expects_continue:
            handler.send_response_only(100)
            handler.end_headers()
        _send_streaming_request(
            connection,
            handler,
            headers=headers,
            content_length=content_length,
            is_chunked=is_chunked,
        )
        response = connection.getresponse()
        if response.status == 407:
            handler.send_error(502, _UPSTREAM_AUTH_FAILED_MESSAGE)
            return
        _copy_response(handler, response)
    except ValueError as exc:
        handler.send_error(400, str(exc))
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
            response = _read_connect_response(upstream_sock)
            if response.status == 407:
                handler.send_error(502, _UPSTREAM_AUTH_FAILED_MESSAGE)
                return
            if response.status != 200:
                _copy_connect_response(handler, response, upstream_sock)
                return

            handler.send_response(200, response.reason)
            handler.end_headers()
        except (OSError, TimeoutError, http.client.HTTPException) as exc:
            handler.send_error(502, f"Bad Gateway: {exc}")
            return

        try:
            _tunnel_sockets(handler.connection, upstream_sock, upstream_initial=response.leftover)
        except OSError:
            return
    finally:
        if upstream_sock is not None:
            upstream_sock.close()


def _send_streaming_request(
    connection: http.client.HTTPConnection,
    handler: BaseHTTPRequestHandler,
    *,
    headers: dict[str, str],
    content_length: int | None,
    is_chunked: bool,
) -> None:
    header_names = {key.lower() for key in headers}
    connection.putrequest(
        handler.command,
        handler.path,
        skip_host="host" in header_names,
        skip_accept_encoding="accept-encoding" in header_names,
    )
    for key, value in headers.items():
        connection.putheader(key, value)
    connection.endheaders()
    if is_chunked:
        _stream_chunked_request_body(handler, connection)
    elif content_length:
        _stream_content_length_request_body(handler, connection, content_length)


def _request_content_length(handler: BaseHTTPRequestHandler) -> int | None:
    raw_length = handler.headers.get("Content-Length")
    if raw_length is None:
        return None
    try:
        length = int(raw_length)
    except ValueError as exc:
        msg = f"Invalid Content-Length: {raw_length}"
        raise ValueError(msg) from exc
    if length < 0:
        msg = f"Invalid Content-Length: {raw_length}"
        raise ValueError(msg)
    return length


def _stream_content_length_request_body(
    handler: BaseHTTPRequestHandler,
    connection: http.client.HTTPConnection,
    content_length: int,
) -> None:
    remaining = content_length
    reader = cast(_PeekableReader, handler.rfile)  # noqa: TC006
    while remaining:
        available = reader.peek(1)
        chunk = reader.read(min(len(available), _HTTP_STREAM_CHUNK_BYTES, remaining))
        if not chunk:
            msg = "Incomplete request body"
            raise ValueError(msg)
        connection.send(chunk)
        remaining -= len(chunk)


def _stream_chunked_request_body(
    handler: BaseHTTPRequestHandler,
    connection: http.client.HTTPConnection,
) -> None:
    while True:
        size_line = handler.rfile.readline()
        size = _chunk_size(size_line)
        connection.send(size_line)
        if size == 0:
            _stream_chunked_trailers(handler, connection)
            return

        remaining = size
        while remaining:
            chunk = handler.rfile.read(min(remaining, _HTTP_STREAM_CHUNK_BYTES))
            if not chunk:
                msg = "Incomplete chunked request body"
                raise ValueError(msg)
            connection.send(chunk)
            remaining -= len(chunk)
        terminator = handler.rfile.read(2)
        if terminator != b"\r\n":
            msg = "Malformed chunked request body"
            raise ValueError(msg)
        connection.send(terminator)


def _chunk_size(size_line: bytes) -> int:
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
    return size


def _stream_chunked_trailers(
    handler: BaseHTTPRequestHandler,
    connection: http.client.HTTPConnection,
) -> None:
    while True:
        line = handler.rfile.readline()
        if line == b"":
            return
        connection.send(line)
        if line in {b"\n", b"\r\n"}:
            return


def _read_connect_response(sock: socket.socket) -> _ConnectResponse:
    data = bytearray()
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(_TUNNEL_BUFFER_BYTES)
        if not chunk:
            msg = "upstream proxy closed before CONNECT response"
            raise http.client.HTTPException(msg)
        data.extend(chunk)
        if len(data) > _CONNECT_RESPONSE_HEADER_LIMIT_BYTES:
            msg = "upstream CONNECT response headers are too large"
            raise http.client.HTTPException(msg)

    raw_headers, leftover = bytes(data).split(b"\r\n\r\n", 1)
    lines = raw_headers.decode("iso-8859-1").split("\r\n")
    status_parts = lines[0].split(" ", 2)
    if len(status_parts) < 2 or not status_parts[0].startswith("HTTP/"):
        msg = "malformed upstream CONNECT response"
        raise http.client.HTTPException(msg)
    try:
        status = int(status_parts[1])
    except ValueError as exc:
        msg = "malformed upstream CONNECT response status"
        raise http.client.HTTPException(msg) from exc
    reason = status_parts[2] if len(status_parts) == 3 else ""
    headers: list[tuple[str, str]] = []
    for line in lines[1:]:
        if not line:
            continue
        key, separator, value = line.partition(":")
        if not separator:
            msg = "malformed upstream CONNECT response header"
            raise http.client.HTTPException(msg)
        headers.append((key, value.strip()))
    return _ConnectResponse(status=status, reason=reason, headers=headers, leftover=leftover)


def _copy_connect_response(
    handler: BaseHTTPRequestHandler,
    response: _ConnectResponse,
    sock: socket.socket,
) -> None:
    body = _read_connect_response_body(sock, response)
    handler.send_response(response.status, response.reason)
    connection_header_names = _connection_header_names(response.headers)
    for key, value in response.headers:
        normalized_key = key.lower()
        if normalized_key in _HOP_BY_HOP_HEADERS or normalized_key in connection_header_names:
            continue
        handler.send_header(key, value)
    handler.end_headers()
    if body:
        handler.wfile.write(body)


def _read_connect_response_body(sock: socket.socket, response: _ConnectResponse) -> bytes:
    headers = {key.lower(): value for key, value in response.headers}
    if "chunked" in headers.get("transfer-encoding", "").lower():
        return _read_chunked_connect_response_body(sock, response.leftover)
    raw_length = headers.get("content-length")
    if raw_length is None:
        return response.leftover
    try:
        content_length = int(raw_length)
    except ValueError:
        return response.leftover
    body = bytearray(response.leftover)
    while len(body) < content_length:
        chunk = sock.recv(content_length - len(body))
        if not chunk:
            break
        body.extend(chunk)
    return bytes(body[:content_length])


def _read_chunked_connect_response_body(sock: socket.socket, initial: bytes) -> bytes:
    buffer = bytearray(initial)
    body = bytearray()
    while True:
        size = _chunk_size(_read_connect_response_line(sock, buffer))
        if size == 0:
            _read_connect_response_trailers(sock, buffer)
            return bytes(body)

        body.extend(_read_connect_response_bytes(sock, buffer, size))
        terminator = _read_connect_response_bytes(sock, buffer, 2)
        if terminator != b"\r\n":
            msg = "malformed upstream CONNECT chunked response body"
            raise http.client.HTTPException(msg)


def _read_connect_response_line(sock: socket.socket, buffer: bytearray) -> bytes:
    while b"\n" not in buffer:
        chunk = sock.recv(_TUNNEL_BUFFER_BYTES)
        if not chunk:
            msg = "upstream proxy closed during CONNECT response body"
            raise http.client.HTTPException(msg)
        buffer.extend(chunk)
    line, separator, rest = bytes(buffer).partition(b"\n")
    buffer[:] = rest
    return line + separator


def _read_connect_response_bytes(sock: socket.socket, buffer: bytearray, size: int) -> bytes:
    while len(buffer) < size:
        chunk = sock.recv(size - len(buffer))
        if not chunk:
            msg = "upstream proxy closed during CONNECT response body"
            raise http.client.HTTPException(msg)
        buffer.extend(chunk)
    data = bytes(buffer[:size])
    del buffer[:size]
    return data


def _read_connect_response_trailers(sock: socket.socket, buffer: bytearray) -> None:
    while True:
        line = _read_connect_response_line(sock, buffer)
        if line in {b"\n", b"\r\n"}:
            return


def _forward_headers(
    items: Iterable[tuple[str, str]],
    *,
    proxy_authorization: str,
) -> dict[str, str]:
    header_items = list(items)
    connection_header_names = _connection_header_names(header_items)
    headers: dict[str, str] = {}
    header_names: dict[str, str] = {}
    for key, value in header_items:
        normalized_key = key.lower()
        if normalized_key in _HOP_BY_HOP_HEADERS or normalized_key in connection_header_names:
            continue
        existing_key = header_names.get(normalized_key)
        if existing_key is None:
            header_names[normalized_key] = key
            headers[key] = value
        else:
            headers[existing_key] = f"{headers[existing_key]}, {value}"
    headers["Proxy-Authorization"] = proxy_authorization
    return headers


def _remove_header(headers: dict[str, str], header_name: str) -> None:
    for key in list(headers):
        if key.lower() == header_name:
            del headers[key]


def _connection_header_names(items: Iterable[tuple[str, str]]) -> set[str]:
    names: set[str] = set()
    for key, value in items:
        if key.lower() != "connection":
            continue
        names.update(token.strip().lower() for token in value.split(",") if token.strip())
    return names


def _copy_response(handler: BaseHTTPRequestHandler, response: http.client.HTTPResponse) -> None:
    handler.send_response(response.status, response.reason)
    has_content_length = False
    response_headers = response.getheaders()
    connection_header_names = _connection_header_names(response_headers)
    for key, value in response_headers:
        normalized_key = key.lower()
        if normalized_key in _HOP_BY_HOP_HEADERS or normalized_key in connection_header_names:
            continue
        if normalized_key == "content-length":
            has_content_length = True
        handler.send_header(key, value)
    if not has_content_length:
        handler.close_connection = True
    handler.end_headers()
    try:
        while chunk := response.read1(_HTTP_STREAM_CHUNK_BYTES):
            handler.wfile.write(chunk)
    except (OSError, TimeoutError, http.client.HTTPException):
        handler.close_connection = True


def _tunnel_sockets(
    client_sock: socket.socket,
    upstream_sock: socket.socket,
    *,
    upstream_initial: bytes = b"",
) -> None:
    activity = _TunnelActivity(at=time.monotonic())
    client_sock.settimeout(_TUNNEL_IDLE_TIMEOUT_SECONDS)
    upstream_sock.settimeout(_TUNNEL_IDLE_TIMEOUT_SECONDS)

    upstream_to_client = threading.Thread(
        target=_relay_tunnel_data,
        args=(upstream_sock, client_sock),
        kwargs={"activity": activity, "initial": upstream_initial},
        daemon=True,
    )
    upstream_to_client.start()
    _relay_tunnel_data(client_sock, upstream_sock, activity=activity)
    while upstream_to_client.is_alive():
        upstream_to_client.join(timeout=_TUNNEL_IDLE_TIMEOUT_SECONDS)
        if upstream_to_client.is_alive() and _tunnel_is_idle(activity):
            _shutdown_tunnel(client_sock, upstream_sock)
            upstream_to_client.join(timeout=1)
            return


def _relay_tunnel_data(
    source: socket.socket,
    target: socket.socket,
    *,
    activity: _TunnelActivity,
    initial: bytes = b"",
) -> None:
    if initial and not _send_tunnel_data(target, initial, activity):
        _shutdown_tunnel(source, target)
        return

    while True:
        try:
            chunk = source.recv(_TUNNEL_BUFFER_BYTES)
        except TimeoutError:
            if _tunnel_is_idle(activity):
                _shutdown_tunnel(source, target)
                return
            continue
        except OSError:
            _shutdown_tunnel(source, target)
            return

        if not chunk:
            with suppress(OSError):
                target.shutdown(socket.SHUT_WR)
            return
        activity.at = time.monotonic()
        if not _send_tunnel_data(target, chunk, activity):
            _shutdown_tunnel(source, target)
            return


def _send_tunnel_data(sock: socket.socket, data: bytes, activity: _TunnelActivity) -> bool:
    view = memoryview(data)
    while view:
        try:
            sent = sock.send(view)
        except TimeoutError:
            if _tunnel_is_idle(activity):
                return False
            continue
        except OSError:
            return False
        if sent == 0:
            return False
        activity.at = time.monotonic()
        view = view[sent:]
    return True


def _tunnel_is_idle(activity: _TunnelActivity) -> bool:
    return time.monotonic() - activity.at >= _TUNNEL_IDLE_TIMEOUT_SECONDS


def _shutdown_tunnel(*socks: socket.socket) -> None:
    for sock in socks:
        with suppress(OSError):
            sock.shutdown(socket.SHUT_RDWR)


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
