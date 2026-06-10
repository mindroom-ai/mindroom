"""Tests for the Agent Vault bridge adapter."""

# ruff: noqa: D101,D102,D103,D105,S105,S106,TC003,SIM117

from __future__ import annotations

import http.client
import importlib.util
import json
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any, Self, cast
from urllib.parse import urlsplit

import pytest

from mindroom.egress import agent_vault_bridge
from mindroom.egress.agent_vault_bridge import _parse_args, _session_token_from_file, start_adapter

_HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "trailers",
    "transfer-encoding",
    "upgrade",
}


@dataclass(slots=True)
class RunningServer:
    httpd: ThreadingHTTPServer
    thread: threading.Thread

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    @property
    def host(self) -> str:
        return str(self.httpd.server_address[0])

    @property
    def port(self) -> int:
        return int(self.httpd.server_address[1])

    @property
    def proxy_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def url(self, path: str) -> str:
        if not path.startswith("/"):
            path = f"/{path}"
        return f"http://{self.host}:{self.port}{path}"


class _QuietHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        del format, args


def _start_server(handler: type[BaseHTTPRequestHandler], *, host: str = "127.0.0.1", port: int = 0) -> RunningServer:
    httpd = ThreadingHTTPServer((host, port), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return RunningServer(httpd=httpd, thread=thread)


def _recv_until(sock: socket.socket, marker: bytes) -> bytes:
    data = b""
    while marker not in data:
        chunk = sock.recv(4096)
        if not chunk:
            return data
        data += chunk
    return data


def _load_live_smoke_module() -> ModuleType:
    path = Path(__file__).parent / "manual" / "agent_vault_bridge_live_smoke.py"
    spec = importlib.util.spec_from_file_location("agent_vault_bridge_live_smoke", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@dataclass(slots=True)
class RequestBodyHandler:
    headers: http.client.HTTPMessage

    @classmethod
    def from_pairs(cls, pairs: Iterable[tuple[str, str]]) -> RequestBodyHandler:
        message = http.client.HTTPMessage()
        for key, value in pairs:
            message[key] = value
        return cls(headers=message)


class _NullReader:
    def peek(self, size: int = 0, /) -> bytes:
        del size
        return b""

    def read(self, size: int = -1, /) -> bytes:
        del size
        return b""


class _NullConnection:
    def gettimeout(self) -> float | None:
        return None

    def setblocking(self, flag: bool, /) -> None:
        del flag

    def settimeout(self, timeout: float | None, /) -> None:
        del timeout


@dataclass(slots=True)
class ConnectHandler:
    path: str = "api.example.test:443"
    connection: _NullConnection = field(default_factory=_NullConnection)
    rfile: _NullReader = field(default_factory=_NullReader)
    responses: list[tuple[int, str | None]] | None = None
    errors: list[tuple[int, str | None]] | None = None
    headers: list[tuple[str, str]] | None = None
    wfile: ConnectHandler = field(init=False)
    body: bytes = b""
    ended_headers: int = 0

    def __post_init__(self) -> None:
        self.responses = []
        self.errors = []
        self.headers = []
        self.wfile = self

    def send_response(self, code: int, message: str | None = None) -> None:
        assert self.responses is not None
        self.responses.append((code, message))

    def send_error(self, code: int, message: str | None = None) -> None:
        assert self.errors is not None
        self.errors.append((code, message))

    def send_header(self, key: str, value: str) -> None:
        assert self.headers is not None
        self.headers.append((key, value))

    def end_headers(self) -> None:
        self.ended_headers += 1

    def write(self, body: bytes) -> None:
        self.body += body


def _forward_headers(
    items: Iterable[tuple[str, str]],
    *,
    add_headers: dict[str, str],
) -> dict[str, str]:
    headers: dict[str, str] = {}
    for key, value in items:
        if key.lower() in _HOP_BY_HOP_HEADERS:
            continue
        if key in headers:
            headers[key] = f"{headers[key]}, {value}"
        else:
            headers[key] = value
    headers.update(add_headers)
    return headers


def _copy_response(handler: BaseHTTPRequestHandler, response: http.client.HTTPResponse) -> None:
    body = response.read()
    handler.send_response(response.status, response.reason)
    for key, value in response.getheaders():
        if key.lower() in _HOP_BY_HOP_HEADERS:
            continue
        handler.send_header(key, value)
    handler.end_headers()
    handler.wfile.write(body)


def start_header_echo() -> RunningServer:
    class HeaderEchoHandler(_QuietHandler):
        def do_GET(self) -> None:
            payload = json.dumps(
                {
                    "path": self.path,
                    "headers": {key.lower(): value for key, value in self.headers.items()},
                },
                sort_keys=True,
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return _start_server(HeaderEchoHandler)


def start_fake_agent_vault(*, required_proxy_token: str, injected_authorization: str) -> RunningServer:
    class FakeAgentVaultHandler(_QuietHandler):
        def do_GET(self) -> None:
            expected = f"Bearer {required_proxy_token}"
            if self.headers.get("Proxy-Authorization") != expected:
                self.send_error(407, "Proxy authorization required")
                return
            _forward_absolute_proxy_request(
                self,
                add_headers={"Authorization": injected_authorization},
            )

    return _start_server(FakeAgentVaultHandler)


def start_rejecting_agent_vault() -> RunningServer:
    class RejectingAgentVaultHandler(_QuietHandler):
        def do_GET(self) -> None:
            self.send_response(407, "Proxy authorization required")
            self.send_header("Proxy-Authenticate", 'Bearer realm="agent-vault"')
            self.end_headers()

    return _start_server(RejectingAgentVaultHandler)


def _forward_absolute_proxy_request(
    handler: BaseHTTPRequestHandler,
    *,
    add_headers: dict[str, str],
) -> None:
    target = urlsplit(handler.path)
    if target.scheme not in {"http", "https"} or not target.hostname:
        handler.send_error(400, "Expected an absolute proxy URL")
        return

    connection_class = http.client.HTTPSConnection if target.scheme == "https" else http.client.HTTPConnection
    target_port = target.port or (443 if target.scheme == "https" else 80)
    target_path = target.path or "/"
    if target.query:
        target_path = f"{target_path}?{target.query}"

    headers = _forward_headers(handler.headers.items(), add_headers=add_headers)
    headers["Host"] = target.netloc
    connection = connection_class(target.hostname, target_port, timeout=10)
    try:
        connection.request(handler.command, target_path, headers=headers)
        response = connection.getresponse()
        _copy_response(handler, response)
    except (OSError, TimeoutError, http.client.HTTPException) as exc:
        handler.send_error(502, f"Bad Gateway: {exc}")
    finally:
        connection.close()


def _fetch(url: str, *, proxy_url: str | None = None) -> dict[str, object]:
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url} if proxy_url else {}),
    )
    with opener.open(url, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def test_adapter_brokers_hidden_url_without_exposing_session_token() -> None:
    with (
        start_header_echo() as upstream,
        start_fake_agent_vault(
            required_proxy_token="adapter-session",
            injected_authorization="Bearer fake-secret",
        ) as fake_vault,
        start_adapter(
            upstream_proxy_url=fake_vault.proxy_url,
            session_token="adapter-session",
        ) as adapter,
    ):
        data = _fetch(upstream.url("/headers"), proxy_url=adapter.proxy_url)

    headers = data["headers"]
    assert isinstance(headers, dict)
    assert headers["authorization"] == "Bearer fake-secret"
    assert "proxy-authorization" not in headers


def test_adapter_streams_http_request_body_before_full_content_length_arrives() -> None:
    fake_proxy = socket.socket()
    fake_proxy.settimeout(5)
    fake_proxy.bind(("127.0.0.1", 0))
    fake_proxy.listen()
    upstream_proxy_url = f"http://127.0.0.1:{fake_proxy.getsockname()[1]}"
    upstream_received = bytearray()
    first_body_bytes_seen = threading.Event()

    def serve_streaming_request_proxy() -> None:
        connection, _addr = fake_proxy.accept()
        with connection:
            request = _recv_until(connection, b"\r\n\r\n")
            _headers, _separator, body = request.partition(b"\r\n\r\n")
            upstream_received.extend(body)
            while len(upstream_received) < len(b"first"):
                chunk = connection.recv(len(b"first") - len(upstream_received))
                if not chunk:
                    return
                upstream_received.extend(chunk)
            first_body_bytes_seen.set()
            while len(upstream_received) < len(b"firstsecond"):
                chunk = connection.recv(1024)
                if not chunk:
                    return
                upstream_received.extend(chunk)
            connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")

    fake_proxy_thread = threading.Thread(target=serve_streaming_request_proxy, daemon=True)
    fake_proxy_thread.start()
    try:
        with start_adapter(upstream_proxy_url=upstream_proxy_url, session_token="adapter-session") as adapter:
            with socket.create_connection((adapter.host, adapter.port), timeout=5) as client:
                client.settimeout(5)
                client.sendall(
                    b"POST http://example.test/upload HTTP/1.1\r\n"
                    b"Host: example.test\r\n"
                    b"Content-Length: 11\r\n"
                    b"\r\n"
                    b"first",
                )
                assert first_body_bytes_seen.wait(timeout=2)
                client.sendall(b"second")
                response = _recv_until(client, b"\r\n\r\n")
                _headers, _separator, body = response.partition(b"\r\n\r\n")
                while len(body) < len(b"ok"):
                    body += client.recv(1024)
    finally:
        fake_proxy.close()
        fake_proxy_thread.join(timeout=5)

    assert bytes(upstream_received) == b"firstsecond"
    assert body == b"ok"


def test_adapter_answers_expect_continue_before_reading_http_request_body() -> None:
    fake_proxy = socket.socket()
    fake_proxy.settimeout(5)
    fake_proxy.bind(("127.0.0.1", 0))
    fake_proxy.listen()
    upstream_proxy_url = f"http://127.0.0.1:{fake_proxy.getsockname()[1]}"
    upstream_received = bytearray()
    upstream_headers: dict[str, str] = {}
    upstream_errors: list[Exception] = []

    def serve_expect_continue_proxy() -> None:
        try:
            connection, _addr = fake_proxy.accept()
            with connection:
                request = _recv_until(connection, b"\r\n\r\n")
                header_bytes, _separator, body = request.partition(b"\r\n\r\n")
                for line in header_bytes.decode("iso-8859-1").split("\r\n")[1:]:
                    if not line:
                        continue
                    key, value = line.split(":", 1)
                    upstream_headers[key.lower()] = value.strip()
                upstream_received.extend(body)
                while len(upstream_received) < len(b"request-body"):
                    chunk = connection.recv(len(b"request-body") - len(upstream_received))
                    if not chunk:
                        return
                    upstream_received.extend(chunk)
                connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
        except Exception as exc:
            upstream_errors.append(exc)

    fake_proxy_thread = threading.Thread(target=serve_expect_continue_proxy, daemon=True)
    fake_proxy_thread.start()
    try:
        with start_adapter(upstream_proxy_url=upstream_proxy_url, session_token="adapter-session") as adapter:
            with socket.create_connection((adapter.host, adapter.port), timeout=5) as client:
                client.settimeout(1)
                client.sendall(
                    b"POST http://example.test/upload HTTP/1.1\r\n"
                    b"Host: example.test\r\n"
                    b"Content-Length: 12\r\n"
                    b"Expect: 100-continue \r\n"
                    b"\r\n",
                )
                interim_response = _recv_until(client, b"\r\n\r\n")
                client.sendall(b"request-body")
                final_response = _recv_until(client, b"\r\n\r\n")
                _headers, _separator, body = final_response.partition(b"\r\n\r\n")
                while len(body) < len(b"ok"):
                    body += client.recv(1024)
    finally:
        fake_proxy.close()
        fake_proxy_thread.join(timeout=5)

    assert upstream_errors == []
    assert interim_response.startswith(b"HTTP/1.1 100")
    assert body == b"ok"
    assert bytes(upstream_received) == b"request-body"
    assert "expect" not in upstream_headers


def test_adapter_streams_chunked_http_request_body_before_full_chunk_arrives(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_vault_bridge, "_HTTP_STREAM_CHUNK_BYTES", len(b"first"))
    fake_proxy = socket.socket()
    fake_proxy.settimeout(5)
    fake_proxy.bind(("127.0.0.1", 0))
    fake_proxy.listen()
    upstream_proxy_url = f"http://127.0.0.1:{fake_proxy.getsockname()[1]}"
    upstream_received = bytearray()
    first_body_bytes_seen = threading.Event()

    def serve_streaming_chunked_request_proxy() -> None:
        connection, _addr = fake_proxy.accept()
        connection.settimeout(5)
        with connection, connection.makefile("rb") as reader:
            assert reader.readline().startswith(b"POST ")
            while reader.readline() != b"\r\n":
                pass
            assert reader.readline() == b"b\r\n"
            upstream_received.extend(reader.read(len(b"first")))
            first_body_bytes_seen.set()
            upstream_received.extend(reader.read(len(b"second")))
            assert reader.read(2) == b"\r\n"
            assert reader.readline() == b"0\r\n"
            assert reader.readline() == b"\r\n"
            connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")

    fake_proxy_thread = threading.Thread(target=serve_streaming_chunked_request_proxy, daemon=True)
    fake_proxy_thread.start()
    try:
        with start_adapter(upstream_proxy_url=upstream_proxy_url, session_token="adapter-session") as adapter:
            with socket.create_connection((adapter.host, adapter.port), timeout=5) as client:
                client.settimeout(5)
                client.sendall(
                    b"POST http://example.test/upload HTTP/1.1\r\n"
                    b"Host: example.test\r\n"
                    b"Transfer-Encoding: chunked\r\n"
                    b"\r\n"
                    b"b\r\n"
                    b"first",
                )
                assert first_body_bytes_seen.wait(timeout=2)
                client.sendall(b"second\r\n0\r\n\r\n")
                response = _recv_until(client, b"\r\n\r\n")
                _headers, _separator, body = response.partition(b"\r\n\r\n")
                while len(body) < len(b"ok"):
                    body += client.recv(1024)
    finally:
        fake_proxy.close()
        fake_proxy_thread.join(timeout=5)

    assert bytes(upstream_received) == b"firstsecond"
    assert body == b"ok"


def test_adapter_strips_content_length_from_chunked_requests_case_insensitively() -> None:
    fake_proxy = socket.socket()
    fake_proxy.settimeout(5)
    fake_proxy.bind(("127.0.0.1", 0))
    fake_proxy.listen()
    upstream_proxy_url = f"http://127.0.0.1:{fake_proxy.getsockname()[1]}"
    seen_headers: dict[str, str] = {}
    upstream_errors: list[Exception] = []

    def serve_chunked_request_proxy() -> None:
        try:
            connection, _addr = fake_proxy.accept()
            with connection:
                request = _recv_until(connection, b"\r\n\r\n")
                header_bytes, _separator, body = request.partition(b"\r\n\r\n")
                for line in header_bytes.decode("iso-8859-1").split("\r\n")[1:]:
                    if not line:
                        continue
                    key, value = line.split(":", 1)
                    seen_headers[key.lower()] = value.strip()
                while b"0\r\n\r\n" not in body:
                    chunk = connection.recv(1024)
                    if not chunk:
                        break
                    body += chunk
                connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
        except Exception as exc:
            upstream_errors.append(exc)

    fake_proxy_thread = threading.Thread(target=serve_chunked_request_proxy, daemon=True)
    fake_proxy_thread.start()
    try:
        with start_adapter(upstream_proxy_url=upstream_proxy_url, session_token="adapter-session") as adapter:
            with socket.create_connection((adapter.host, adapter.port), timeout=5) as client:
                client.settimeout(5)
                client.sendall(
                    b"POST http://example.test/upload HTTP/1.1\r\n"
                    b"Host: example.test\r\n"
                    b"content-length: 999\r\n"
                    b"Transfer-Encoding: chunked\r\n"
                    b"\r\n"
                    b"5\r\nhello\r\n0\r\n\r\n",
                )
                response = _recv_until(client, b"\r\n\r\n")
    finally:
        fake_proxy.close()
        fake_proxy_thread.join(timeout=5)

    assert upstream_errors == []
    assert response.startswith(b"HTTP/1.0 200")
    assert "content-length" not in seen_headers
    assert seen_headers["transfer-encoding"] == "chunked"


def test_adapter_streams_http_response_body_before_full_response_arrives() -> None:
    fake_proxy = socket.socket()
    fake_proxy.settimeout(5)
    fake_proxy.bind(("127.0.0.1", 0))
    fake_proxy.listen()
    upstream_proxy_url = f"http://127.0.0.1:{fake_proxy.getsockname()[1]}"
    send_response_tail = threading.Event()

    def serve_streaming_response_proxy() -> None:
        connection, _addr = fake_proxy.accept()
        with connection:
            _recv_until(connection, b"\r\n\r\n")
            connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 11\r\n\r\nfirst")
            if send_response_tail.wait(timeout=5):
                connection.sendall(b"second")

    fake_proxy_thread = threading.Thread(target=serve_streaming_response_proxy, daemon=True)
    fake_proxy_thread.start()
    body = b""
    try:
        with start_adapter(upstream_proxy_url=upstream_proxy_url, session_token="adapter-session") as adapter:
            with socket.create_connection((adapter.host, adapter.port), timeout=5) as client:
                client.settimeout(2)
                client.sendall(b"GET http://example.test/data HTTP/1.1\r\nHost: example.test\r\n\r\n")
                response = _recv_until(client, b"\r\n\r\n")
                _headers, _separator, body = response.partition(b"\r\n\r\n")
                while len(body) < len(b"first"):
                    body += client.recv(1024)
                assert body == b"first"
                send_response_tail.set()
                while len(body) < len(b"firstsecond"):
                    body += client.recv(1024)
    finally:
        fake_proxy.close()
        fake_proxy_thread.join(timeout=5)

    assert body == b"firstsecond"


def test_adapter_closes_http_response_when_upstream_uses_chunked_encoding() -> None:
    fake_proxy = socket.socket()
    fake_proxy.settimeout(5)
    fake_proxy.bind(("127.0.0.1", 0))
    fake_proxy.listen()
    upstream_proxy_url = f"http://127.0.0.1:{fake_proxy.getsockname()[1]}"

    def serve_chunked_response_proxy() -> None:
        connection, _addr = fake_proxy.accept()
        with connection:
            _recv_until(connection, b"\r\n\r\n")
            connection.sendall(
                b"HTTP/1.1 200 OK\r\n"
                b"Transfer-Encoding: chunked\r\n"
                b"Trailer: X-Checksum\r\n"
                b"\r\n"
                b"5\r\nhello\r\n0\r\nX-Checksum: abc\r\n\r\n",
            )

    fake_proxy_thread = threading.Thread(target=serve_chunked_response_proxy, daemon=True)
    fake_proxy_thread.start()
    body = b""
    try:
        with start_adapter(upstream_proxy_url=upstream_proxy_url, session_token="adapter-session") as adapter:
            with socket.create_connection((adapter.host, adapter.port), timeout=5) as client:
                client.settimeout(5)
                client.sendall(b"GET http://example.test/chunked HTTP/1.1\r\nHost: example.test\r\n\r\n")
                response = _recv_until(client, b"\r\n\r\n")
                header_bytes, _separator, body = response.partition(b"\r\n\r\n")
                while True:
                    chunk = client.recv(1024)
                    if not chunk:
                        break
                    body += chunk
    finally:
        fake_proxy.close()
        fake_proxy_thread.join(timeout=5)

    assert header_bytes.startswith(b"HTTP/1.0 200")
    assert b"Transfer-Encoding" not in header_bytes
    assert b"Trailer" not in header_bytes
    assert body == b"hello"


def test_fake_agent_vault_rejects_requests_without_proxy_authorization() -> None:
    with (
        start_header_echo() as upstream,
        start_fake_agent_vault(
            required_proxy_token="adapter-session",
            injected_authorization="Bearer fake-secret",
        ) as fake_vault,
        pytest.raises(urllib.error.HTTPError) as exc_info,
    ):
        _fetch(upstream.url("/headers"), proxy_url=fake_vault.proxy_url)

    assert exc_info.value.code == 407


def test_adapter_converts_upstream_http_407_to_bad_gateway() -> None:
    with (
        start_header_echo() as upstream,
        start_rejecting_agent_vault() as fake_vault,
        start_adapter(
            upstream_proxy_url=fake_vault.proxy_url,
            session_token="adapter-session",
        ) as adapter,
        pytest.raises(urllib.error.HTTPError) as exc_info,
    ):
        _fetch(upstream.url("/headers"), proxy_url=adapter.proxy_url)

    assert exc_info.value.code == 502
    assert "upstream proxy authentication failed" in exc_info.value.reason


def test_adapter_forwards_connect_proxy_authorization_and_tunnels_bytes() -> None:
    seen_headers: dict[str, str] = {}
    fake_proxy = socket.socket()
    fake_proxy.settimeout(5)
    fake_proxy.bind(("127.0.0.1", 0))
    fake_proxy.listen()
    upstream_proxy_url = f"http://127.0.0.1:{fake_proxy.getsockname()[1]}"

    def serve_connect_tunnel() -> None:
        connection, _addr = fake_proxy.accept()
        with connection:
            request = b""
            while b"\r\n\r\n" not in request:
                request += connection.recv(1024)
            header_lines = request.decode("iso-8859-1").split("\r\n")[1:]
            for line in header_lines:
                if not line:
                    continue
                key, value = line.split(":", 1)
                seen_headers[key.lower()] = value.strip()
            connection.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            payload = connection.recv(1024)
            connection.sendall(b"upstream:" + payload)

    fake_proxy_thread = threading.Thread(target=serve_connect_tunnel, daemon=True)
    fake_proxy_thread.start()
    try:
        with start_adapter(upstream_proxy_url=upstream_proxy_url, session_token="adapter-session") as adapter:
            with socket.create_connection((adapter.host, adapter.port), timeout=5) as client:
                client.sendall(b"CONNECT api.github.com:443 HTTP/1.1\r\nHost: api.github.com:443\r\n\r\n")
                response = client.recv(1024)
                client.sendall(b"ping")
                tunneled_response = client.recv(1024)
    finally:
        fake_proxy.close()
        fake_proxy_thread.join(timeout=5)

    assert response.startswith(b"HTTP/1.0 200")
    assert tunneled_response == b"upstream:ping"
    assert seen_headers["proxy-authorization"] == "Bearer adapter-session"


def test_adapter_connect_tunnel_handles_slow_reader_backpressure() -> None:
    payload = b"x" * (512 * 1024)
    upstream_errors: list[Exception] = []
    fake_proxy = socket.socket()
    fake_proxy.settimeout(5)
    fake_proxy.bind(("127.0.0.1", 0))
    fake_proxy.listen()
    upstream_proxy_url = f"http://127.0.0.1:{fake_proxy.getsockname()[1]}"

    def serve_large_tunnel_response() -> None:
        try:
            connection, _addr = fake_proxy.accept()
            with connection:
                _recv_until(connection, b"\r\n\r\n")
                connection.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                assert connection.recv(1024) == b"request"
                connection.sendall(payload)
        except Exception as exc:
            upstream_errors.append(exc)

    fake_proxy_thread = threading.Thread(target=serve_large_tunnel_response, daemon=True)
    fake_proxy_thread.start()
    received = bytearray()
    try:
        with start_adapter(upstream_proxy_url=upstream_proxy_url, session_token="adapter-session") as adapter:
            with socket.create_connection((adapter.host, adapter.port), timeout=5) as client:
                client.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
                client.settimeout(15)
                client.sendall(b"CONNECT api.github.com:443 HTTP/1.1\r\nHost: api.github.com:443\r\n\r\n")
                response = _recv_until(client, b"\r\n\r\n")
                assert response.startswith(b"HTTP/1.0 200")
                client.sendall(b"request")
                time.sleep(0.5)
                while len(received) < len(payload):
                    chunk = client.recv(65536)
                    if not chunk:
                        break
                    received.extend(chunk)
    finally:
        fake_proxy.close()
        fake_proxy_thread.join(timeout=5)

    assert upstream_errors == []
    assert len(received) == len(payload)
    assert set(received) == {ord("x")}


def test_connect_tunnel_closes_when_both_directions_are_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_vault_bridge, "_TUNNEL_IDLE_TIMEOUT_SECONDS", 0.05)
    worker_sock, adapter_client_sock = socket.socketpair()
    adapter_upstream_sock, upstream_sock = socket.socketpair()
    tunnel_thread = threading.Thread(
        target=agent_vault_bridge._tunnel_sockets,
        args=(adapter_client_sock, adapter_upstream_sock),
        daemon=True,
    )

    tunnel_thread.start()
    tunnel_thread.join(timeout=1)
    tunnel_still_running = tunnel_thread.is_alive()
    for sock in (worker_sock, adapter_client_sock, adapter_upstream_sock, upstream_sock):
        sock.close()
    tunnel_thread.join(timeout=1)

    assert not tunnel_still_running


def test_connect_tunnel_keeps_one_way_stream_alive_when_reverse_direction_is_idle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent_vault_bridge, "_TUNNEL_IDLE_TIMEOUT_SECONDS", 0.15)
    worker_sock, adapter_client_sock = socket.socketpair()
    adapter_upstream_sock, upstream_sock = socket.socketpair()
    for sock in (worker_sock, adapter_client_sock, adapter_upstream_sock, upstream_sock):
        sock.settimeout(2)
    tunnel_thread = threading.Thread(
        target=agent_vault_bridge._tunnel_sockets,
        args=(adapter_client_sock, adapter_upstream_sock),
        daemon=True,
    )

    tunnel_thread.start()
    received = bytearray()
    try:
        for index in range(8):
            byte = bytes([65 + index])
            upstream_sock.sendall(byte)
            received.extend(worker_sock.recv(1))
            time.sleep(0.05)
    finally:
        for sock in (worker_sock, adapter_client_sock, adapter_upstream_sock, upstream_sock):
            sock.close()
        tunnel_thread.join(timeout=1)

    assert bytes(received) == b"ABCDEFGH"


def test_adapter_connect_tunnel_preserves_bytes_buffered_with_upstream_200() -> None:
    fake_proxy = socket.socket()
    fake_proxy.settimeout(5)
    fake_proxy.bind(("127.0.0.1", 0))
    fake_proxy.listen()
    upstream_proxy_url = f"http://127.0.0.1:{fake_proxy.getsockname()[1]}"

    def serve_connect_with_immediate_tunnel_bytes() -> None:
        connection, _addr = fake_proxy.accept()
        with connection:
            _recv_until(connection, b"\r\n\r\n")
            connection.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\nearly-tunnel-bytes")

    fake_proxy_thread = threading.Thread(target=serve_connect_with_immediate_tunnel_bytes, daemon=True)
    fake_proxy_thread.start()
    try:
        with start_adapter(upstream_proxy_url=upstream_proxy_url, session_token="adapter-session") as adapter:
            with socket.create_connection((adapter.host, adapter.port), timeout=5) as client:
                client.settimeout(5)
                client.sendall(b"CONNECT api.github.com:443 HTTP/1.1\r\nHost: api.github.com:443\r\n\r\n")
                response = _recv_until(client, b"\r\n\r\n")
                header_bytes, _separator, tunneled_bytes = response.partition(b"\r\n\r\n")
                if not tunneled_bytes:
                    tunneled_bytes = client.recv(1024)
    finally:
        fake_proxy.close()
        fake_proxy_thread.join(timeout=5)

    assert header_bytes.startswith(b"HTTP/1.0 200")
    assert tunneled_bytes == b"early-tunnel-bytes"


def test_adapter_connect_tunnel_preserves_reverse_direction_after_client_half_close() -> None:
    fake_proxy = socket.socket()
    fake_proxy.settimeout(5)
    fake_proxy.bind(("127.0.0.1", 0))
    fake_proxy.listen()
    upstream_proxy_url = f"http://127.0.0.1:{fake_proxy.getsockname()[1]}"
    upstream_received = bytearray()

    def serve_connect_after_client_half_close() -> None:
        connection, _addr = fake_proxy.accept()
        with connection:
            _recv_until(connection, b"\r\n\r\n")
            connection.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            while True:
                chunk = connection.recv(1024)
                if not chunk:
                    break
                upstream_received.extend(chunk)
            connection.sendall(b"response-after-client-half-close")

    fake_proxy_thread = threading.Thread(target=serve_connect_after_client_half_close, daemon=True)
    fake_proxy_thread.start()
    received = bytearray()
    try:
        with start_adapter(upstream_proxy_url=upstream_proxy_url, session_token="adapter-session") as adapter:
            with socket.create_connection((adapter.host, adapter.port), timeout=5) as client:
                client.settimeout(5)
                client.sendall(b"CONNECT api.github.com:443 HTTP/1.1\r\nHost: api.github.com:443\r\n\r\n")
                response = _recv_until(client, b"\r\n\r\n")
                assert response.startswith(b"HTTP/1.0 200")
                client.sendall(b"request")
                client.shutdown(socket.SHUT_WR)
                while True:
                    chunk = client.recv(1024)
                    if not chunk:
                        break
                    received.extend(chunk)
    finally:
        fake_proxy.close()
        fake_proxy_thread.join(timeout=5)

    assert bytes(upstream_received) == b"request"
    assert bytes(received) == b"response-after-client-half-close"


def test_adapter_converts_upstream_connect_407_to_bad_gateway() -> None:
    class RejectingConnectProxyHandler(_QuietHandler):
        def do_CONNECT(self) -> None:
            self.send_response(407, "Proxy authorization required")
            self.send_header("Proxy-Authenticate", 'Bearer realm="agent-vault"')
            self.end_headers()

    with (
        _start_server(RejectingConnectProxyHandler) as fake_vault,
        start_adapter(upstream_proxy_url=fake_vault.proxy_url, session_token="adapter-session") as adapter,
        socket.create_connection((adapter.host, adapter.port), timeout=5) as client,
    ):
        client.sendall(b"CONNECT api.github.com:443 HTTP/1.1\r\nHost: api.github.com:443\r\n\r\n")
        response = client.recv(1024)

    assert response.startswith(b"HTTP/1.0 502")
    assert b"upstream proxy authentication failed" in response
    assert b"Proxy-Authenticate" not in response


def test_forward_headers_combines_duplicate_client_headers() -> None:
    headers = agent_vault_bridge._forward_headers(
        [
            ("X-Trace", "first"),
            ("Connection", "keep-alive"),
            ("X-Trace", "second"),
            ("Proxy-Authorization", "Bearer worker-token"),
        ],
        proxy_authorization="Bearer adapter-session",
    )

    assert headers == {
        "X-Trace": "first, second",
        "Proxy-Authorization": "Bearer adapter-session",
    }


def test_forward_headers_strips_connection_nominated_headers() -> None:
    headers = agent_vault_bridge._forward_headers(
        [
            ("Connection", "X-Hop, keep-alive"),
            ("X-Hop", "secret"),
            ("X-Trace", "visible"),
        ],
        proxy_authorization="Bearer adapter-session",
    )

    assert headers == {
        "X-Trace": "visible",
        "Proxy-Authorization": "Bearer adapter-session",
    }


def test_copy_response_strips_connection_nominated_headers_and_closes_on_stream_failure() -> None:
    class RaisingBodyResponse:
        status = 200
        reason = "OK"

        def getheaders(self) -> list[tuple[str, str]]:
            return [
                ("Connection", "X-Hop"),
                ("X-Hop", "secret"),
                ("Content-Type", "text/plain"),
            ]

        def read1(self, _size: int) -> bytes:
            msg = "upstream stream failed"
            raise OSError(msg)

    class CapturingHandler:
        close_connection = False

        def __init__(self) -> None:
            self.responses: list[tuple[int, str]] = []
            self.headers: list[tuple[str, str]] = []
            self.ended_headers = 0
            self.wfile = self

        def send_response(self, code: int, message: str) -> None:
            self.responses.append((code, message))

        def send_header(self, key: str, value: str) -> None:
            self.headers.append((key, value))

        def end_headers(self) -> None:
            self.ended_headers += 1

        def write(self, _chunk: bytes) -> None:
            msg = "response body write should not run"
            raise AssertionError(msg)

    handler = CapturingHandler()

    agent_vault_bridge._copy_response(handler, RaisingBodyResponse())

    assert handler.responses == [(200, "OK")]
    assert handler.headers == [("Content-Type", "text/plain")]
    assert handler.ended_headers == 1
    assert handler.close_connection is True


def test_copy_connect_response_strips_connection_nominated_headers() -> None:
    class CapturingHandler:
        def __init__(self) -> None:
            self.responses: list[tuple[int, str]] = []
            self.headers: list[tuple[str, str]] = []
            self.ended_headers = 0
            self.wfile = self

        def send_response(self, code: int, message: str) -> None:
            self.responses.append((code, message))

        def send_header(self, key: str, value: str) -> None:
            self.headers.append((key, value))

        def end_headers(self) -> None:
            self.ended_headers += 1

        def write(self, _body: bytes) -> None:
            msg = "empty CONNECT response body should not be written"
            raise AssertionError(msg)

    _client_sock, upstream_sock = socket.socketpair()
    handler = CapturingHandler()
    try:
        agent_vault_bridge._copy_connect_response(
            handler,
            agent_vault_bridge._ConnectResponse(
                status=403,
                reason="Forbidden",
                headers=[
                    ("Connection", "X-Hop"),
                    ("X-Hop", "secret"),
                    ("Content-Length", "0"),
                ],
                leftover=b"",
            ),
            upstream_sock,
        )
    finally:
        _client_sock.close()
        upstream_sock.close()

    assert handler.responses == [(403, "Forbidden")]
    assert handler.headers == [("Content-Length", "0")]
    assert handler.ended_headers == 1


def test_copy_connect_response_decodes_chunked_body_when_transfer_encoding_is_stripped() -> None:
    class CapturingHandler:
        def __init__(self) -> None:
            self.responses: list[tuple[int, str]] = []
            self.headers: list[tuple[str, str]] = []
            self.ended_headers = 0
            self.body = b""
            self.wfile = self

        def send_response(self, code: int, message: str) -> None:
            self.responses.append((code, message))

        def send_header(self, key: str, value: str) -> None:
            self.headers.append((key, value))

        def end_headers(self) -> None:
            self.ended_headers += 1

        def write(self, body: bytes) -> None:
            self.body += body

    _client_sock, upstream_sock = socket.socketpair()
    handler = CapturingHandler()
    try:
        agent_vault_bridge._copy_connect_response(
            handler,
            agent_vault_bridge._ConnectResponse(
                status=403,
                reason="Forbidden",
                headers=[
                    ("Transfer-Encoding", "chunked"),
                    ("Trailer", "X-Checksum"),
                ],
                leftover=b"5\r\nerror\r\n0\r\nX-Checksum: abc\r\n\r\n",
            ),
            upstream_sock,
        )
    finally:
        _client_sock.close()
        upstream_sock.close()

    assert handler.responses == [(403, "Forbidden")]
    assert handler.headers == []
    assert handler.body == b"error"
    assert handler.ended_headers == 1


@pytest.mark.parametrize("raw_length", ["-1", "not-an-int"])
def test_connect_response_body_rejects_invalid_content_length(raw_length: str) -> None:
    _client_sock, upstream_sock = socket.socketpair()
    try:
        with pytest.raises(http.client.HTTPException, match="upstream CONNECT response Content-Length"):
            agent_vault_bridge._read_connect_response_body(
                upstream_sock,
                agent_vault_bridge._ConnectResponse(
                    status=403,
                    reason="Forbidden",
                    headers=[("Content-Length", raw_length)],
                    leftover=b"abc",
                ),
            )
    finally:
        _client_sock.close()
        upstream_sock.close()


def test_request_content_length_rejects_invalid_header() -> None:
    handler = RequestBodyHandler.from_pairs([("Content-Length", "not-an-int")])

    with pytest.raises(ValueError, match="Invalid Content-Length: not-an-int"):
        agent_vault_bridge._request_content_length(handler)


def test_request_content_length_rejects_conflicting_duplicates() -> None:
    handler = RequestBodyHandler.from_pairs([("Content-Length", "5"), ("Content-Length", "10")])

    with pytest.raises(ValueError, match="Conflicting Content-Length: 5, 10"):
        agent_vault_bridge._request_content_length(handler)


def test_request_content_length_rejects_non_digit_numeric_forms() -> None:
    handler = RequestBodyHandler.from_pairs([("Content-Length", "+5")])

    with pytest.raises(ValueError, match=r"Invalid Content-Length: \+5"):
        agent_vault_bridge._request_content_length(handler)


def test_request_content_length_collapses_identical_duplicates() -> None:
    handler = RequestBodyHandler.from_pairs([("Content-Length", "7"), ("Content-Length", "7")])

    assert agent_vault_bridge._request_content_length(handler) == 7


def test_chunk_size_rejects_negative_values() -> None:
    with pytest.raises(ValueError, match="Negative chunk size"):
        agent_vault_bridge._chunk_size(b"-1\r\n")


def test_forward_connect_returns_bad_gateway_when_upstream_closes_before_response() -> None:
    fake_proxy = socket.socket()
    fake_proxy.settimeout(5)
    fake_proxy.bind(("127.0.0.1", 0))
    fake_proxy.listen()
    proxy_port = fake_proxy.getsockname()[1]

    def close_before_response() -> None:
        connection, _addr = fake_proxy.accept()
        connection.close()

    fake_proxy_thread = threading.Thread(target=close_before_response, daemon=True)
    fake_proxy_thread.start()
    handler = ConnectHandler()
    try:
        agent_vault_bridge._forward_connect(
            handler,
            proxy_host="127.0.0.1",
            proxy_port=proxy_port,
            proxy_authorization="Bearer adapter-session",
        )
    finally:
        fake_proxy.close()
        fake_proxy_thread.join(timeout=5)

    assert handler.responses == []
    assert handler.errors
    assert handler.errors[0][0] == 502


def test_forward_connect_returns_bad_gateway_for_truncated_non_200_response_body() -> None:
    fake_proxy = socket.socket()
    fake_proxy.settimeout(5)
    fake_proxy.bind(("127.0.0.1", 0))
    fake_proxy.listen()
    proxy_port = fake_proxy.getsockname()[1]

    def send_truncated_forbidden() -> None:
        connection, _addr = fake_proxy.accept()
        with connection:
            _recv_until(connection, b"\r\n\r\n")
            connection.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 5\r\n\r\nabc")

    fake_proxy_thread = threading.Thread(target=send_truncated_forbidden, daemon=True)
    fake_proxy_thread.start()
    handler = ConnectHandler()
    try:
        agent_vault_bridge._forward_connect(
            handler,
            proxy_host="127.0.0.1",
            proxy_port=proxy_port,
            proxy_authorization="Bearer adapter-session",
        )
    finally:
        fake_proxy.close()
        fake_proxy_thread.join(timeout=5)

    assert handler.responses == []
    assert handler.errors
    assert handler.errors[0][0] == 502


def test_forward_connect_does_not_write_http_error_after_tunnel_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_proxy = socket.socket()
    fake_proxy.settimeout(5)
    fake_proxy.bind(("127.0.0.1", 0))
    fake_proxy.listen()
    proxy_port = fake_proxy.getsockname()[1]

    def accept_connect() -> None:
        connection, _addr = fake_proxy.accept()
        with connection:
            request = b""
            while b"\r\n\r\n" not in request:
                request += connection.recv(1024)
            connection.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")

    fake_proxy_thread = threading.Thread(target=accept_connect, daemon=True)
    fake_proxy_thread.start()

    def fail_tunnel(*_args: object, **_kwargs: object) -> None:
        raise OSError

    monkeypatch.setattr(agent_vault_bridge, "_tunnel_sockets", fail_tunnel)
    handler = ConnectHandler()

    try:
        agent_vault_bridge._forward_connect(
            handler,
            proxy_host="127.0.0.1",
            proxy_port=proxy_port,
            proxy_authorization="Bearer adapter-session",
        )
    finally:
        fake_proxy.close()
        fake_proxy_thread.join(timeout=5)

    assert handler.responses == [(200, "Connection Established")]
    assert handler.errors == []


def test_cli_reads_session_token_from_named_environment_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_VAULT_PROXY_SESSION_TOKEN", "adapter-session")

    args = agent_vault_bridge._parse_args(
        [
            "--upstream-proxy-url",
            "http://agent-vault:14322",
        ],
    )

    assert args.session_token_env == "AGENT_VAULT_PROXY_SESSION_TOKEN"
    assert agent_vault_bridge._session_token_from_env(args.session_token_env) == "adapter-session"


def test_cli_rejects_raw_session_token_argument() -> None:
    with pytest.raises(SystemExit):
        agent_vault_bridge._parse_args(
            [
                "--upstream-proxy-url",
                "http://agent-vault:14322",
                "--session-token",
                "leaky-token",
            ],
        )


def test_cli_defaults_host_to_loopback() -> None:
    args = agent_vault_bridge._parse_args(["--upstream-proxy-url", "http://agent-vault:14322"])

    assert args.host == "127.0.0.1"


def test_live_smoke_parses_worker_json_after_docker_pull_output() -> None:
    smoke = cast("Any", _load_live_smoke_module())

    headers = smoke._parse_worker_headers(
        "Unable to find image 'python:3.13-alpine' locally\n"
        "3.13-alpine: Pulling from library/python\n"
        '{"Authorization": "Bearer fake-secret", "Host": "local-echo.test"}\n',
    )

    assert headers == {
        "Authorization": "Bearer fake-secret",
        "Host": "local-echo.test",
    }


def test_live_smoke_worker_targets_local_echo_server() -> None:
    smoke = cast("Any", _load_live_smoke_module())

    assert smoke._worker_target_url() == "http://local-echo.test/headers"


def test_adapter_connect_forwards_client_bytes_pipelined_with_request() -> None:
    fake_proxy = socket.socket()
    fake_proxy.settimeout(5)
    fake_proxy.bind(("127.0.0.1", 0))
    fake_proxy.listen()
    upstream_proxy_url = f"http://127.0.0.1:{fake_proxy.getsockname()[1]}"
    captured: dict[str, bytes] = {}

    def serve_connect_capture() -> None:
        connection, _addr = fake_proxy.accept()
        with connection:
            request = _recv_until(connection, b"\r\n\r\n")
            _headers, _separator, leftover = request.partition(b"\r\n\r\n")
            connection.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            received = bytearray(leftover)
            while len(received) < len(b"PIPELINED-CLIENTHELLO"):
                chunk = connection.recv(4096)
                if not chunk:
                    break
                received.extend(chunk)
            captured["bytes"] = bytes(received)

    fake_proxy_thread = threading.Thread(target=serve_connect_capture, daemon=True)
    fake_proxy_thread.start()
    try:
        with (
            start_adapter(upstream_proxy_url=upstream_proxy_url, session_token="adapter-session") as adapter,
            socket.create_connection((adapter.host, adapter.port), timeout=5) as client,
        ):
            client.settimeout(5)
            # Coalesce the CONNECT request and the first tunnel bytes in a single send, without
            # waiting for the 200, so the bytes land in handler.rfile's buffer.
            client.sendall(
                b"CONNECT api.example.test:443 HTTP/1.1\r\nHost: api.example.test:443\r\n\r\nPIPELINED-CLIENTHELLO",
            )
            response = _recv_until(client, b"\r\n\r\n")
    finally:
        fake_proxy.close()
        fake_proxy_thread.join(timeout=5)

    assert response.startswith(b"HTTP/1.0 200")
    assert captured.get("bytes") == b"PIPELINED-CLIENTHELLO"


def test_connect_tunnel_times_out_wedged_direction_while_reverse_is_busy(  # noqa: C901
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent_vault_bridge, "_TUNNEL_IDLE_TIMEOUT_SECONDS", 0.5)
    client_a, client_b = socket.socketpair()
    upstream_a, upstream_b = socket.socketpair()
    for sock in (client_a, client_b, upstream_a, upstream_b):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    stop = threading.Event()

    def flood_reverse() -> None:
        # upstream -> client direction stays busy and never reads upstream-bound data,
        # so the client -> upstream direction wedges while this keeps refreshing its own clock.
        block = b"y" * 4096
        while not stop.is_set():
            try:
                upstream_b.sendall(block)
            except OSError:
                return

    def drain_client() -> None:
        while not stop.is_set():
            try:
                if not client_b.recv(4096):
                    return
            except OSError:
                return

    def flood_forward() -> None:
        with suppress(OSError):
            client_b.sendall(b"x" * (2 * 1024 * 1024))

    workers = [
        threading.Thread(target=flood_reverse, daemon=True),
        threading.Thread(target=drain_client, daemon=True),
        threading.Thread(target=flood_forward, daemon=True),
    ]
    for worker in workers:
        worker.start()

    tunnel_thread = threading.Thread(
        target=agent_vault_bridge._tunnel_sockets,
        args=(client_a, upstream_a),
        daemon=True,
    )
    tunnel_thread.start()
    tunnel_thread.join(timeout=8)
    finished = not tunnel_thread.is_alive()

    stop.set()
    for sock in (client_a, client_b, upstream_a, upstream_b):
        with suppress(OSError):
            sock.close()
    for worker in workers:
        worker.join(timeout=5)

    assert finished


def test_copy_connect_response_closes_connection_when_client_body_write_fails() -> None:
    class FailingWriteHandler:
        close_connection = False

        def __init__(self) -> None:
            self.responses: list[tuple[int, str]] = []
            self.headers: list[tuple[str, str]] = []
            self.ended_headers = 0
            self.wfile = self

        def send_response(self, code: int, message: str) -> None:
            self.responses.append((code, message))

        def send_header(self, key: str, value: str) -> None:
            self.headers.append((key, value))

        def end_headers(self) -> None:
            self.ended_headers += 1

        def write(self, _body: bytes) -> None:
            msg = "client gone"
            raise OSError(msg)

    _client_sock, upstream_sock = socket.socketpair()
    handler = FailingWriteHandler()
    try:
        agent_vault_bridge._copy_connect_response(
            handler,
            agent_vault_bridge._ConnectResponse(
                status=403,
                reason="Forbidden",
                headers=[("Content-Length", "5")],
                leftover=b"nope!",
            ),
            upstream_sock,
        )
    finally:
        _client_sock.close()
        upstream_sock.close()

    assert handler.responses == [(403, "Forbidden")]
    assert handler.ended_headers == 1
    assert handler.close_connection is True


def test_adapter_rejects_conflicting_request_content_length() -> None:
    with (
        start_adapter(upstream_proxy_url="http://127.0.0.1:1", session_token="adapter-session") as adapter,
        socket.create_connection((adapter.host, adapter.port), timeout=5) as client,
    ):
        client.settimeout(5)
        client.sendall(
            b"POST http://example.test/upload HTTP/1.1\r\n"
            b"Host: example.test\r\n"
            b"Content-Length: 5\r\n"
            b"Content-Length: 6\r\n"
            b"\r\n"
            b"abcde",
        )
        response = _recv_until(client, b"\r\n\r\n")

    assert response.startswith(b"HTTP/1.0 400")


def test_adapter_times_out_stalled_request_body(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_vault_bridge, "_REQUEST_IDLE_TIMEOUT_SECONDS", 0.5)
    fake_proxy = socket.socket()
    fake_proxy.settimeout(5)
    fake_proxy.bind(("127.0.0.1", 0))
    fake_proxy.listen()
    upstream_proxy_url = f"http://127.0.0.1:{fake_proxy.getsockname()[1]}"

    def serve_silent_proxy() -> None:
        connection, _addr = fake_proxy.accept()
        with connection, suppress(OSError):
            while connection.recv(4096):
                pass

    fake_proxy_thread = threading.Thread(target=serve_silent_proxy, daemon=True)
    fake_proxy_thread.start()
    try:
        with (
            start_adapter(upstream_proxy_url=upstream_proxy_url, session_token="adapter-session") as adapter,
            socket.create_connection((adapter.host, adapter.port), timeout=10) as client,
        ):
            client.settimeout(10)
            client.sendall(
                b"POST http://example.test/upload HTTP/1.1\r\nHost: example.test\r\nContent-Length: 100\r\n\r\npartial",
            )
            response = _recv_until(client, b"\r\n\r\n")
    finally:
        fake_proxy.close()
        fake_proxy_thread.join(timeout=5)

    assert response.startswith(b"HTTP/1.0 502")


def test_adapter_does_not_send_interim_continue_to_http_1_0_client() -> None:
    fake_proxy = socket.socket()
    fake_proxy.settimeout(5)
    fake_proxy.bind(("127.0.0.1", 0))
    fake_proxy.listen()
    upstream_proxy_url = f"http://127.0.0.1:{fake_proxy.getsockname()[1]}"
    upstream_headers: dict[str, str] = {}
    upstream_errors: list[Exception] = []

    def serve_proxy() -> None:
        try:
            connection, _addr = fake_proxy.accept()
            with connection:
                request = _recv_until(connection, b"\r\n\r\n")
                header_bytes, _separator, body = request.partition(b"\r\n\r\n")
                for line in header_bytes.decode("iso-8859-1").split("\r\n")[1:]:
                    if not line:
                        continue
                    key, value = line.split(":", 1)
                    upstream_headers[key.lower()] = value.strip()
                received = bytearray(body)
                while len(received) < len(b"data"):
                    chunk = connection.recv(len(b"data") - len(received))
                    if not chunk:
                        return
                    received.extend(chunk)
                connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
        except Exception as exc:
            upstream_errors.append(exc)

    fake_proxy_thread = threading.Thread(target=serve_proxy, daemon=True)
    fake_proxy_thread.start()
    try:
        with (
            start_adapter(upstream_proxy_url=upstream_proxy_url, session_token="adapter-session") as adapter,
            socket.create_connection((adapter.host, adapter.port), timeout=5) as client,
        ):
            client.settimeout(5)
            client.sendall(
                b"POST http://example.test/upload HTTP/1.0\r\n"
                b"Host: example.test\r\n"
                b"Content-Length: 4\r\n"
                b"Expect: 100-continue\r\n"
                b"\r\n"
                b"data",
            )
            response = _recv_until(client, b"\r\n\r\n")
    finally:
        fake_proxy.close()
        fake_proxy_thread.join(timeout=5)

    assert upstream_errors == []
    assert response.startswith(b"HTTP/1.0 200")
    assert b"100 Continue" not in response
    assert "expect" not in upstream_headers


def test_adapter_rejects_truncated_chunked_trailers() -> None:
    fake_proxy = socket.socket()
    fake_proxy.settimeout(5)
    fake_proxy.bind(("127.0.0.1", 0))
    fake_proxy.listen()
    upstream_proxy_url = f"http://127.0.0.1:{fake_proxy.getsockname()[1]}"

    def serve_drain_proxy() -> None:
        connection, _addr = fake_proxy.accept()
        with connection, suppress(OSError):
            while connection.recv(4096):
                pass

    fake_proxy_thread = threading.Thread(target=serve_drain_proxy, daemon=True)
    fake_proxy_thread.start()
    try:
        with (
            start_adapter(upstream_proxy_url=upstream_proxy_url, session_token="adapter-session") as adapter,
            socket.create_connection((adapter.host, adapter.port), timeout=5) as client,
        ):
            client.settimeout(5)
            client.sendall(
                b"POST http://example.test/upload HTTP/1.1\r\n"
                b"Host: example.test\r\n"
                b"Transfer-Encoding: chunked\r\n"
                b"\r\n"
                b"5\r\nhello\r\n0\r\n",  # last-chunk size line, then EOF before the terminating CRLF
            )
            client.shutdown(socket.SHUT_WR)
            response = _recv_until(client, b"\r\n\r\n")
    finally:
        fake_proxy.close()
        fake_proxy_thread.join(timeout=5)

    assert response.startswith(b"HTTP/1.0 400")


def test_parse_args_supports_session_token_file(tmp_path: Path) -> None:
    """The CLI accepts a token file as an alternative to the env-var source."""
    args = _parse_args(
        [
            "--upstream-proxy-url",
            "http://agent-vault:14322",
            "--session-token-file",
            "/agent-vault-work/token",
        ],
    )
    assert args.session_token_file == "/agent-vault-work/token"

    token_path = tmp_path / "token"
    token_path.write_text("  session-token-value\n", encoding="utf-8")
    assert _session_token_from_file(str(token_path)) == "session-token-value"

    empty_path = tmp_path / "empty"
    empty_path.write_text("\n", encoding="utf-8")
    with pytest.raises(ValueError, match="empty"):
        _session_token_from_file(str(empty_path))
    with pytest.raises(FileNotFoundError):
        _session_token_from_file(str(tmp_path / "missing"))


def test_parse_args_rejects_both_token_sources() -> None:
    """Env-var and file token sources are mutually exclusive."""
    with pytest.raises(SystemExit):
        _parse_args(
            [
                "--upstream-proxy-url",
                "http://agent-vault:14322",
                "--session-token-env",
                "TOKEN_ENV",
                "--session-token-file",
                "/agent-vault-work/token",
            ],
        )
