"""Tests for the Agent Vault bridge adapter."""

# ruff: noqa: D101,D102,D103,D105,S105,S106,TC003,SIM117

from __future__ import annotations

import http.client
import io
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Self
from urllib.parse import urlsplit

import pytest

from mindroom.egress import agent_vault_bridge
from mindroom.egress.agent_vault_bridge import start_adapter

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


@dataclass(slots=True)
class RequestBodyHandler:
    headers: dict[str, str]
    rfile: io.BytesIO


@dataclass(slots=True)
class ConnectHandler:
    path: str = "api.example.test:443"
    connection: object = object()
    responses: list[tuple[int, str | None]] | None = None
    errors: list[tuple[int, str | None]] | None = None
    ended_headers: int = 0

    def __post_init__(self) -> None:
        self.responses = []
        self.errors = []

    def send_response(self, code: int, message: str | None = None) -> None:
        assert self.responses is not None
        self.responses.append((code, message))

    def send_error(self, code: int, message: str | None = None) -> None:
        assert self.errors is not None
        self.errors.append((code, message))

    def end_headers(self) -> None:
        self.ended_headers += 1


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
    payload = b"x" * (8 * 1024 * 1024)
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


def test_read_request_body_rejects_invalid_content_length() -> None:
    handler = RequestBodyHandler(
        headers={"Content-Length": "not-an-int"},
        rfile=io.BytesIO(b"body"),
    )

    with pytest.raises(ValueError, match="Invalid Content-Length: not-an-int"):
        agent_vault_bridge._read_request_body(handler)


def test_read_request_body_rejects_negative_chunk_size() -> None:
    handler = RequestBodyHandler(
        headers={"Transfer-Encoding": "chunked"},
        rfile=io.BytesIO(b"-1\r\nbody\r\n0\r\n\r\n"),
    )

    with pytest.raises(ValueError, match="Negative chunk size"):
        agent_vault_bridge._read_request_body(handler)


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

    agent_vault_bridge._forward_connect(
        handler,
        proxy_host="127.0.0.1",
        proxy_port=proxy_port,
        proxy_authorization="Bearer adapter-session",
    )
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
