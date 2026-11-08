"""Tests for the Trafilatura toolkit's server-side fetch policy."""

from __future__ import annotations

import gzip
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import TYPE_CHECKING

import agno.tools.trafilatura as agno_trafilatura
import httpx
import pytest
from trafilatura import spider
from trafilatura.downloads import USER_AGENT

import mindroom.tools.agno_compat_trafilatura as compat
from mindroom import public_fetch
from mindroom.tools.trafilatura import trafilatura_tools
from tests.public_fetch_helpers import TrickleBody, public_dns, serve, serve_routes

if TYPE_CHECKING:
    from collections.abc import Iterator

_DENIED = "The URL is not allowed for a public fetch"
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
# The article page linking to two pages instead of one.
_HOMEPAGE_HTML = _ARTICLE_HTML.replace(
    '<p><a href="/docs">Documentation</a></p>',
    '<p><a href="/docs">Documentation</a> <a href="/guide">Guide</a></p>',
)
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


@pytest.fixture(autouse=True)
def _isolate_trafilatura(monkeypatch: pytest.MonkeyPatch) -> None:
    """Use public test DNS and no crawler politeness delay."""
    public_dns(monkeypatch)
    monkeypatch.setitem(spider.DEFAULT_CONFIG["DEFAULT"], "SLEEP_TIME", "0")


@pytest.fixture
def connect_attempts(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Record and refuse every outbound socket connection."""
    attempts: list[object] = []

    def refuse(_sock: socket.socket, address: object) -> None:
        attempts.append(address)
        raise ConnectionRefusedError(address)

    monkeypatch.setattr(socket.socket, "connect", refuse)
    return attempts


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
    requests = serve_routes(
        monkeypatch,
        {start_url: httpx.Response(302, headers={"Location": "http://127.0.0.1:8765/api/config/raw"})},
    )

    results = [
        trafilatura_tools()().extract_text(start_url),
        trafilatura_tools()().extract_metadata_only(start_url),
        trafilatura_tools()().extract_batch([start_url]),
        trafilatura_tools()().crawl_website(start_url),
    ]

    assert {str(request.url) for request in requests} == {start_url, "https://example.com/robots.txt"}
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


def test_trafilatura_refuses_a_hostname_that_resolves_to_loopback(
    monkeypatch: pytest.MonkeyPatch,
    loopback_server: tuple[int, list[object]],
) -> None:
    """The address a hostname resolves to when the connection is made is checked before it is dialed."""
    port, connections = loopback_server

    def loopback_getaddrinfo(_host: str | bytes, dns_port: int, *_args: object, **_kwargs: object) -> list[object]:
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", dns_port))]

    monkeypatch.setattr("mindroom.server_fetch_url.socket.getaddrinfo", loopback_getaddrinfo)

    result = trafilatura_tools()().extract_text(f"http://rebind.test:{port}/api/config/raw")

    assert _DENIED in result
    assert "loopback secret" not in result
    assert connections == []


def test_trafilatura_extracts_public_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """Public pages should still be downloaded and extracted."""
    start_url = "https://example.com/start"
    article_url = "https://example.com/article"
    requests = serve_routes(
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
    assert [str(request.url) for request in requests[:2]] == [start_url, article_url]
    assert {request.headers["user-agent"] for request in requests} == {USER_AGENT}


def test_trafilatura_rejects_oversized_download(monkeypatch: pytest.MonkeyPatch) -> None:
    """Responses beyond Trafilatura's size limit should not be buffered or extracted."""
    url = "https://example.com/large"
    serve_routes(monkeypatch, {url: httpx.Response(200, text=_ARTICLE_HTML)})
    monkeypatch.setattr(compat, "_MAX_FILE_SIZE", 64)

    result = trafilatura_tools()().extract_text(url)

    assert result == f"Error: Could not fetch content from URL: {url}"


def test_trafilatura_rejects_compressed_download(monkeypatch: pytest.MonkeyPatch) -> None:
    """A compressed body could expand past the size limit during decoding, so it is refused unread."""
    url = "https://example.com/bomb"
    bomb = gzip.compress(b"\0" * 32 * 1024 * 1024)
    requests = serve_routes(monkeypatch, {url: httpx.Response(200, headers={"Content-Encoding": "gzip"}, content=bomb)})

    result = trafilatura_tools()().extract_text(url)

    assert [request.headers["accept-encoding"] for request in requests] == ["identity"]
    assert result == f"Error: Could not fetch content from URL: {url}"


def _crawl(
    monkeypatch: pytest.MonkeyPatch,
    docs_response: httpx.Response,
    homepage_html: str = _ARTICLE_HTML,
) -> tuple[str, list[str]]:
    homepage = "https://example.com/"
    requests = serve_routes(
        monkeypatch,
        {
            homepage: httpx.Response(200, text=homepage_html),
            "https://example.com/docs": docs_response,
            "https://example.com/guide": httpx.Response(200, text=_DOCS_HTML),
        },
    )
    result = trafilatura_tools()().crawl_website(homepage, extract_content=True)
    return result, [str(request.url) for request in requests]


def test_trafilatura_crawl_extracts_public_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    """Crawling should keep working through the guarded fetchers."""
    result, requested_urls = _crawl(monkeypatch, httpx.Response(200, text=_DOCS_HTML))

    crawl = json.loads(result)
    assert "https://example.com/docs" in crawl["known_links"]
    assert "Configure a bridge" in crawl["extracted_content"]["https://example.com/docs"]
    assert all(url.startswith("https://example.com/") for url in requested_urls)


def test_trafilatura_crawl_counts_the_page_a_redirect_ends_on_as_visited(monkeypatch: pytest.MonkeyPatch) -> None:
    """A crawled link that redirects is recorded under the URL its redirect ended on."""
    result, _requested_urls = _crawl(monkeypatch, httpx.Response(301, headers={"Location": "/guide"}))

    assert "https://example.com/guide" in json.loads(result)["known_links"]


@pytest.mark.parametrize(
    "refused_location",
    ["http://127.0.0.1:8765/api/config/raw", "https://user:secret@example.com/private"],
)
def test_trafilatura_crawl_skips_a_discovered_link_it_refuses(
    monkeypatch: pytest.MonkeyPatch,
    refused_location: str,
) -> None:
    """A crawled link refused at a redirect hop is skipped, and the crawl goes on to the next link."""
    result, requested_urls = _crawl(
        monkeypatch,
        httpx.Response(302, headers={"Location": refused_location}),
        homepage_html=_HOMEPAGE_HTML,
    )

    crawl = json.loads(result)
    assert crawl["to_visit"] == []
    assert "Configure a bridge" in crawl["extracted_content"]["https://example.com/guide"]
    assert _DENIED in crawl["extracted_content"]["https://example.com/docs"]
    assert all(url.startswith("https://example.com/") for url in requested_urls)


def test_trafilatura_crawl_of_a_site_crawled_before_starts_with_fresh_crawl_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A repeat crawl visits the site again and reports only the links it finds now, none from the earlier crawl."""
    first, _requested_urls = _crawl(monkeypatch, httpx.Response(200, text=_DOCS_HTML))
    guide_only = _ARTICLE_HTML.replace('href="/docs"', 'href="/guide"')

    second, _requested_urls = _crawl(monkeypatch, httpx.Response(404), homepage_html=guide_only)

    assert "https://example.com/docs" in json.loads(first)["known_links"]
    assert set(json.loads(second)["known_links"]) == {"https://example.com/", "https://example.com/guide"}


def test_trafilatura_crawls_of_one_site_running_at_once_share_no_crawl_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two crawls of one site that run at the same time each report only the pages they found themselves."""
    sections = ("alpha", "beta")
    started = {section: threading.Event() for section in sections}

    def handler(request: httpx.Request) -> httpx.Response:
        section = request.url.path.strip("/")
        if section in started:
            # Each crawl reads its start page only once the other crawl has begun, so both run at once.
            started[section].set()
            (other,) = set(sections) - {section}
            started[other].wait(timeout=10)
            page = _ARTICLE_HTML.replace('href="/docs"', f'href="/{section}/docs"')
        else:
            page = _DOCS_HTML
        return httpx.Response(200, stream=httpx.ByteStream(page.encode()))

    serve(monkeypatch, handler)
    toolkit = trafilatura_tools()()
    results: dict[str, dict[str, object]] = {}

    def crawl(section: str) -> None:
        results[section] = json.loads(toolkit.crawl_website(f"https://example.com/{section}/"))

    threads = [threading.Thread(target=crawl, args=(section,)) for section in sections]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    for section in sections:
        assert set(results[section]["known_links"]) == {
            f"https://example.com/{section}/",
            f"https://example.com/{section}/docs",
        }


def test_trafilatura_crawler_called_outside_the_toolkit_keeps_crawling_with_its_own_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Code that calls Trafilatura's crawler directly, not through the toolkit, still crawls with the crawler's store."""
    trafilatura_tools()
    monkeypatch.setattr(compat, "_process_url_store", compat._new_url_store())
    serve_routes(
        monkeypatch,
        {
            "https://example.com/": httpx.Response(200, text=_ARTICLE_HTML),
            "https://example.com/docs": httpx.Response(200, text=_DOCS_HTML),
        },
    )

    to_visit, known_links = spider.focused_crawler("https://example.com/")

    assert to_visit == []
    assert "https://example.com/docs" in known_links


def test_trafilatura_refuses_a_batch_over_its_url_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """A batch longer than the per-call limit is refused before anything downloads."""
    requests = serve_routes(monkeypatch, {})

    result = trafilatura_tools()().extract_batch([f"https://example.com/{index}" for index in range(11)])

    assert result == "Error: extract_batch takes at most 10 URLs per call; split the list into several calls."
    assert requests == []


def test_trafilatura_batch_stops_downloading_at_its_time_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """A download that keeps trickling ends at the call's time limit, and no later download starts."""
    monkeypatch.setattr(compat, "CALL_SECONDS", 0.3)
    # Each download's own deadline stays well past the call's limit, and short enough to fail in seconds without it.
    monkeypatch.setattr(public_fetch, "_DEADLINE_SECONDS", 10)
    requests = serve(monkeypatch, lambda _request: httpx.Response(200, stream=TrickleBody()))
    urls = [f"https://example.com/{index}" for index in range(3)]
    toolkit = trafilatura_tools()()
    started = time.monotonic()

    batch = json.loads(toolkit.extract_batch(urls))

    assert time.monotonic() - started < 5
    assert batch["failed_urls"] == urls
    assert batch["results"] == {}
    assert len(requests) == 1


def test_trafilatura_crawl_stops_visiting_pages_at_half_its_time_limit_and_extracts_what_it_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A long robots.txt crawl delay ends the crawl's visits at half the time limit, and the other half extracts."""
    monkeypatch.setattr(compat, "CALL_SECONDS", 2.0)
    homepage = "https://example.com/"
    serve_routes(
        monkeypatch,
        {
            "https://example.com/robots.txt": httpx.Response(200, text="User-agent: *\nCrawl-delay: 3600\n"),
            homepage: httpx.Response(200, text=_HOMEPAGE_HTML),
            "https://example.com/docs": httpx.Response(200, text=_DOCS_HTML),
            "https://example.com/guide": httpx.Response(200, text=_DOCS_HTML),
        },
    )
    toolkit = trafilatura_tools()()
    started = time.monotonic()

    crawl = json.loads(toolkit.crawl_website(homepage, extract_content=True))

    assert time.monotonic() - started < 5
    (unvisited,) = crawl["to_visit"]
    assert unvisited in {"https://example.com/docs", "https://example.com/guide"}
    assert "Configure a bridge" in crawl["extracted_content"][unvisited]


def test_trafilatura_factory_installs_guarded_fetchers() -> None:
    """Every downloader reachable from TrafilaturaTools should be the guarded one."""
    trafilatura_tools()

    assert agno_trafilatura.fetch_url is compat._fetch_url
    assert spider.fetch_url is compat._fetch_url
    assert spider.fetch_response is compat._crawl_fetch_response
    assert spider.crawl_page is compat._crawl_page
    assert spider.sleep is compat._pause
