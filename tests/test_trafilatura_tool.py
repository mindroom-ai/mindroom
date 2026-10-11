"""Tests for the Trafilatura toolkit's server-side fetch policy."""

from __future__ import annotations

import gzip
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import TYPE_CHECKING

import agno.tools.trafilatura as agno_trafilatura
import httpx
import pytest
from courlan import UrlStore
from trafilatura import spider

import mindroom.tools.agno_compat_trafilatura as compat
from mindroom.tools.trafilatura import trafilatura_tools

if TYPE_CHECKING:
    from collections.abc import Iterator

_DENIED = "URL is not allowed for server-side fetching"
_ARTICLE_HTML = """
<html>
  <head><title>Matrix bridges</title></head>
  <body>
    <article>
      <h1>Matrix bridges</h1>
      <p>Bridges connect Matrix rooms to other chat networks so people can keep talking.</p>
      <p>Each bridge relays messages in both directions and preserves the conversation history.</p>
      <p><a href="/docs">Documentation</a></p>
    </article>
  </body>
</html>
"""
_DOCS_HTML = """
<html>
  <body>
    <article>
      <h1>Bridge documentation</h1>
      <p>Configure a bridge by registering an application service with the homeserver first.</p>
      <p>Then invite the bridge bot into the portal room and link the remote conversation.</p>
    </article>
  </body>
</html>
"""


def _public_getaddrinfo(_host: str | bytes, port: int, *_args: object, **_kwargs: object) -> list[object]:
    return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", port))]


@pytest.fixture(autouse=True)
def _isolate_trafilatura(monkeypatch: pytest.MonkeyPatch) -> None:
    """Use public test DNS, a fresh crawler URL store, and no crawler politeness delay."""
    monkeypatch.setattr("mindroom.server_fetch_url.socket.getaddrinfo", _public_getaddrinfo)
    monkeypatch.setattr(spider, "URL_STORE", UrlStore(compressed=False, strict=False))
    monkeypatch.setattr(spider, "sleep", lambda _seconds: None)


@pytest.fixture
def connect_attempts(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Record and refuse every outbound socket connection."""
    attempts: list[object] = []

    def refuse(_sock: socket.socket, address: object) -> None:
        attempts.append(address)
        raise ConnectionRefusedError(address)

    monkeypatch.setattr(socket.socket, "connect", refuse)
    return attempts


def _serve(
    monkeypatch: pytest.MonkeyPatch,
    routes: dict[str, httpx.Response],
) -> list[str]:
    """Serve canned responses as unread network streams and record requested URLs."""
    requested_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        requested_urls.append(url)
        route = routes.get(url, httpx.Response(404))
        return httpx.Response(route.status_code, headers=route.headers, stream=httpx.ByteStream(route.content))

    monkeypatch.setattr(compat, "ServerFetchHTTPTransport", lambda: httpx.MockTransport(handler))
    return requested_urls


def _call_all_url_functions(url: str) -> list[str]:
    toolkit = trafilatura_tools()()
    return [
        toolkit.extract_text(url),
        toolkit.extract_metadata_only(url),
        toolkit.extract_batch([url]),
        toolkit.crawl_website(url, extract_content=True),
    ]


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8765/api/config/raw",
        "http://localhost:8765/api/credentials/anthropic/api-key?include_value=true",
        "http://[::1]:8765/api/config/raw",
        "http://169.254.169.254/latest/meta-data/",
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://10.0.0.5/",
        "http://192.168.1.20:8008/_matrix/client/versions",
        "file:///etc/passwd",
    ],
)
def test_trafilatura_rejects_unsafe_targets_without_connecting(url: str, connect_attempts: list[object]) -> None:
    """Every URL-taking function should return the deny error before opening a socket."""
    results = _call_all_url_functions(url)

    assert connect_attempts == []
    for result in results:
        assert _DENIED in result


def test_trafilatura_revalidates_redirects_before_following(monkeypatch: pytest.MonkeyPatch) -> None:
    """A public URL redirecting to the loopback dashboard should be refused at the hop."""
    start_url = "https://example.com/redirect"
    requested_urls = _serve(
        monkeypatch,
        {start_url: httpx.Response(302, headers={"Location": "http://127.0.0.1:8765/api/config/raw"})},
    )

    results = [
        trafilatura_tools()().extract_text(start_url),
        trafilatura_tools()().extract_metadata_only(start_url),
        trafilatura_tools()().extract_batch([start_url]),
    ]

    assert set(requested_urls) == {start_url}
    for result in results:
        assert _DENIED in result


@pytest.fixture
def loopback_server() -> Iterator[tuple[int, list[object]]]:
    """Run a loopback HTTP server that records every accepted connection."""
    connections: list[object] = []

    class RecordingHandler(BaseHTTPRequestHandler):
        def setup(self) -> None:
            connections.append(self.client_address)
            super().setup()

        def do_GET(self) -> None:
            body = b"<html><body><article><p>loopback secret</p></article></body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *_args: object) -> None:  # noqa: A002, ARG002
            return

    server = HTTPServer(("127.0.0.1", 0), RecordingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port, connections
    finally:
        server.shutdown()
        server.server_close()


def test_trafilatura_rejects_dns_rebind_at_connect_time(
    monkeypatch: pytest.MonkeyPatch,
    loopback_server: tuple[int, list[object]],
) -> None:
    """The address dialed must be validated, not only the address seen during URL validation."""
    port, connections = loopback_server
    dns_calls = 0

    def rebinding_getaddrinfo(_host: str | bytes, dns_port: int, *_args: object, **_kwargs: object) -> list[object]:
        nonlocal dns_calls
        dns_calls += 1
        ip_address = "93.184.216.34" if dns_calls == 1 else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip_address, dns_port))]

    monkeypatch.setattr("mindroom.server_fetch_url.socket.getaddrinfo", rebinding_getaddrinfo)

    result = trafilatura_tools()().extract_text(f"http://rebind.test:{port}/api/config/raw")

    assert _DENIED in result
    assert "loopback secret" not in result
    assert connections == []
    assert dns_calls >= 2


def test_trafilatura_extracts_public_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """Public pages should still be downloaded and extracted."""
    start_url = "https://example.com/start"
    article_url = "https://example.com/article"
    requested_urls = _serve(
        monkeypatch,
        {
            start_url: httpx.Response(301, headers={"Location": "/article"}),
            article_url: httpx.Response(200, text=_ARTICLE_HTML),
        },
    )
    toolkit = trafilatura_tools()()

    text = toolkit.extract_text(start_url)
    metadata = json.loads(toolkit.extract_metadata_only(article_url))
    batch = json.loads(toolkit.extract_batch([article_url]))

    assert "Bridges connect Matrix rooms" in text
    assert metadata["title"] == "Matrix bridges"
    assert batch["successful_extractions"] == 1
    assert "Bridges connect Matrix rooms" in batch["results"][article_url]
    assert requested_urls[:2] == [start_url, article_url]


def test_trafilatura_rejects_oversized_download(monkeypatch: pytest.MonkeyPatch) -> None:
    """Responses beyond Trafilatura's size limit should not be buffered or extracted."""
    url = "https://example.com/large"
    _serve(monkeypatch, {url: httpx.Response(200, text=_ARTICLE_HTML)})
    monkeypatch.setattr(compat, "_MAX_FILE_SIZE", 64)

    result = trafilatura_tools()().extract_text(url)

    assert result == f"Error: Could not fetch content from URL: {url}"


def test_trafilatura_rejects_compressed_download(monkeypatch: pytest.MonkeyPatch) -> None:
    """A compressed body could expand past the size limit during decoding, so it is refused unread."""
    url = "https://example.com/bomb"
    requested_encodings: list[str | None] = []
    bomb = gzip.compress(b"\0" * 32 * 1024 * 1024)

    def handler(request: httpx.Request) -> httpx.Response:
        requested_encodings.append(request.headers.get("accept-encoding"))
        return httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=httpx.ByteStream(bomb))

    monkeypatch.setattr(compat, "ServerFetchHTTPTransport", lambda: httpx.MockTransport(handler))

    result = trafilatura_tools()().extract_text(url)

    assert requested_encodings == ["identity"]
    assert result == f"Error: Could not fetch content from URL: {url}"


def _crawl(monkeypatch: pytest.MonkeyPatch, docs_response: httpx.Response) -> tuple[str, list[str]]:
    homepage = "https://example.com/"
    requested_urls = _serve(
        monkeypatch,
        {homepage: httpx.Response(200, text=_ARTICLE_HTML), "https://example.com/docs": docs_response},
    )
    return trafilatura_tools()().crawl_website(homepage, extract_content=True), requested_urls


def test_trafilatura_crawl_extracts_public_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    """Crawling should keep working through the guarded fetchers."""
    result, requested_urls = _crawl(monkeypatch, httpx.Response(200, text=_DOCS_HTML))

    crawl = json.loads(result)
    assert "https://example.com/docs" in crawl["known_links"]
    assert "Configure a bridge" in crawl["extracted_content"]["https://example.com/docs"]
    assert all(url.startswith("https://example.com/") for url in requested_urls)


def test_trafilatura_crawl_rejects_discovered_redirect_to_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Links discovered by the crawler should be revalidated at every redirect hop."""
    result, requested_urls = _crawl(
        monkeypatch,
        httpx.Response(302, headers={"Location": "http://127.0.0.1:8765/api/config/raw"}),
    )

    assert _DENIED in result
    assert "https://example.com/docs" in requested_urls
    assert all(url.startswith("https://example.com/") for url in requested_urls)


def test_trafilatura_factory_installs_guarded_fetchers() -> None:
    """Every downloader reachable from TrafilaturaTools should be the guarded one."""
    trafilatura_tools()

    assert agno_trafilatura.fetch_url is compat._fetch_url
    assert spider.fetch_url is compat._fetch_url
    assert spider.fetch_response is compat._fetch_response
