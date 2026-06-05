"""Tests for the Agent Vault bridge adapter."""

# ruff: noqa: D101,D102,D103,D105,S106,TC003,SIM117

from __future__ import annotations

import http.client
import json
import socket
import threading
import urllib.error
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Self
from urllib.parse import urlsplit

import pytest

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
    def log_message(self, _format: str, *_args: object) -> None:
        return


def _start_server(handler: type[BaseHTTPRequestHandler], *, host: str = "127.0.0.1", port: int = 0) -> RunningServer:
    httpd = ThreadingHTTPServer((host, port), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return RunningServer(httpd=httpd, thread=thread)


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


def test_adapter_forwards_connect_proxy_authorization() -> None:
    seen_headers: dict[str, str] = {}

    class ConnectProxyHandler(BaseHTTPRequestHandler):
        def do_CONNECT(self) -> None:
            seen_headers.update({key.lower(): value for key, value in self.headers.items()})
            self.send_response(200, "Connection Established")
            self.end_headers()

        def log_message(self, _format: str, *_args: object) -> None:
            return

    fake_proxy = ThreadingHTTPServer(("127.0.0.1", 0), ConnectProxyHandler)
    fake_proxy_thread = threading.Thread(target=fake_proxy.serve_forever, daemon=True)
    fake_proxy_thread.start()
    upstream_proxy_url = f"http://127.0.0.1:{fake_proxy.server_address[1]}"

    try:
        with start_adapter(upstream_proxy_url=upstream_proxy_url, session_token="adapter-session") as adapter:
            with socket.create_connection((adapter.host, adapter.port), timeout=5) as client:
                client.sendall(b"CONNECT api.github.com:443 HTTP/1.1\r\nHost: api.github.com:443\r\n\r\n")
                response = client.recv(1024)
    finally:
        fake_proxy.shutdown()
        fake_proxy.server_close()
        fake_proxy_thread.join(timeout=5)

    assert response.startswith(b"HTTP/1.0 200")
    assert seen_headers["proxy-authorization"] == "Bearer adapter-session"
