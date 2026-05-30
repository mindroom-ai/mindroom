"""Tests for MindRoom website scraping tools."""

from __future__ import annotations

import json
import socket
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx
import pytest
from bs4 import BeautifulSoup

from mindroom.custom_tools.website import (
    _MAX_REDIRECTS,
    _TOO_MANY_REDIRECTS,
    WebsiteTools,
    _MindRoomWebsiteReader,
    _safe_url_for_log,
)
from mindroom.server_fetch_url import ServerFetchUrlError
from mindroom.tools.website import website_tools


@pytest.fixture(autouse=True)
def _public_dns_for_website_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep website reader tests focused on crawler behavior instead of real DNS."""
    monkeypatch.setattr(
        "mindroom.server_fetch_url.socket.getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443)),
        ],
    )


def test_website_reader_prefers_content_body_over_search_modal() -> None:
    """A hidden or early search section should not replace the page body."""
    html = """
    <html>
      <body>
        <aside id="search" aria-hidden="true">
          <section class="search-header">
            <a>Search</a>
          </section>
        </aside>
        <main class="page-body">
          <section>
            <h1>Biography</h1>
            <p>Bas Nijholt builds open source scientific software and AI tooling.</p>
          </section>
        </main>
      </body>
    </html>
    """
    soup = BeautifulSoup(html, "html.parser")

    content = _MindRoomWebsiteReader()._extract_main_content(soup)

    assert "Biography" in content
    assert "open source scientific software" in content
    assert "Search" not in content


def test_website_read_url_crawls_links_from_chrome_without_returning_chrome_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Page chrome can contain crawl links without becoming extracted content."""
    root_url = "https://example.test/"
    docs_url = "https://example.test/docs"
    pages = {
        root_url: """
        <html>
          <body>
            <nav><a href="/docs">Docs Navigation</a></nav>
            <main><h1>Home</h1><p>Welcome to the product guide.</p></main>
          </body>
        </html>
        """,
        docs_url: """
        <html>
          <body>
            <main><h1>Docs</h1><p>Detailed reference material lives here.</p></main>
          </body>
        </html>
        """,
    }

    def fake_get(url: str, *, timeout: int, follow_redirects: bool) -> httpx.Response:
        assert timeout == 10
        assert follow_redirects is False
        request = httpx.Request("GET", url)
        return httpx.Response(200, content=pages[url].encode(), request=request)

    def no_delay(_reader: _MindRoomWebsiteReader, min_seconds: int = 1, max_seconds: int = 3) -> None:
        assert min_seconds == 1
        assert max_seconds == 3

    monkeypatch.setattr("mindroom.custom_tools.website._server_fetch_get", fake_get)
    monkeypatch.setattr(_MindRoomWebsiteReader, "delay", no_delay)

    documents = json.loads(WebsiteTools().read_url(root_url))
    content_by_url = {document["meta_data"]["url"]: document["content"] for document in documents}

    assert list(content_by_url) == [root_url, docs_url]
    assert "Welcome to the product guide" in content_by_url[root_url]
    assert "Docs Navigation" not in content_by_url[root_url]
    assert "Detailed reference material" in content_by_url[docs_url]


def test_website_reader_rejects_private_starting_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Agent-callable website reads should not fetch private network URLs."""
    requested_urls: list[str] = []

    def fake_get(url: str, **_kwargs: object) -> httpx.Response:
        requested_urls.append(url)
        msg = "private URL should be rejected before an HTTP request is made"
        raise AssertionError(msg)

    monkeypatch.setattr("mindroom.custom_tools.website._server_fetch_get", fake_get)

    with pytest.raises(ValueError, match="URL is not allowed"):
        _MindRoomWebsiteReader().read("http://127.0.0.1:8000/private")

    assert requested_urls == []


def test_website_reader_rejects_unsupported_schemes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Agent-callable website reads should only allow HTTP(S) URLs."""
    requested_urls: list[str] = []

    def fake_get(url: str, **_kwargs: object) -> httpx.Response:
        requested_urls.append(url)
        msg = "unsupported URL schemes should be rejected before a request is made"
        raise AssertionError(msg)

    monkeypatch.setattr("mindroom.custom_tools.website._server_fetch_get", fake_get)

    with pytest.raises(ValueError, match="URL is not allowed"):
        _MindRoomWebsiteReader().read("file:///etc/passwd")

    assert requested_urls == []


def test_website_reader_revalidates_redirect_targets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirects to private network URLs should be blocked before following the hop."""
    start_url = "https://example.com/redirect"
    requested_urls: list[str] = []

    def fake_get(url: str, *, timeout: int, follow_redirects: bool) -> httpx.Response:
        requested_urls.append(url)
        assert timeout == 10
        assert follow_redirects is False
        request = httpx.Request("GET", url)
        if url == start_url:
            return httpx.Response(302, headers={"Location": "http://127.0.0.1/admin"}, request=request)
        msg = "private redirect target should be rejected before fetching"
        raise AssertionError(msg)

    def no_delay(_reader: _MindRoomWebsiteReader, min_seconds: int = 1, max_seconds: int = 3) -> None:
        assert min_seconds == 1
        assert max_seconds == 3

    monkeypatch.setattr("mindroom.custom_tools.website._server_fetch_get", fake_get)
    monkeypatch.setattr(_MindRoomWebsiteReader, "delay", no_delay)

    with pytest.raises(ValueError, match="URL is not allowed"):
        _MindRoomWebsiteReader().read(start_url)

    assert requested_urls == [start_url]


def test_website_reader_follows_allowed_redirect_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    """Allowed redirect chains should be followed to their final response."""
    start_url = "https://example.com/redirect"
    intermediate_url = "https://example.com/intermediate"
    final_url = "https://example.com/final"
    requested_urls: list[str] = []

    def fake_get(url: str, *, timeout: int, follow_redirects: bool) -> httpx.Response:
        requested_urls.append(url)
        assert timeout == 10
        assert follow_redirects is False
        request = httpx.Request("GET", url)
        if url == start_url:
            return httpx.Response(302, headers={"Location": intermediate_url}, request=request)
        if url == intermediate_url:
            return httpx.Response(301, headers={"Location": "/final"}, request=request)
        assert url == final_url
        return httpx.Response(200, text="ok", request=request)

    monkeypatch.setattr("mindroom.custom_tools.website._server_fetch_get", fake_get)

    response, fetched_url = _MindRoomWebsiteReader()._get_validated_response(start_url)

    assert requested_urls == [start_url, intermediate_url, final_url]
    assert response.status_code == 200
    assert response.text == "ok"
    assert fetched_url == final_url


def test_website_reader_records_safe_public_cross_host_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    """Safe public redirects should be usable even when they change host."""
    start_url = "https://example.com/"
    final_url = "https://www.example.com/"
    html = """
    <html>
      <body>
        <main>
          <h1>Docs</h1>
          <p>Public redirected content with enough text to keep the final page.</p>
        </main>
      </body>
    </html>
    """
    requested_urls: list[str] = []

    def fake_get(url: str, *, timeout: int, follow_redirects: bool) -> httpx.Response:
        requested_urls.append(url)
        assert timeout == 10
        assert follow_redirects is False
        request = httpx.Request("GET", url)
        if url == start_url:
            return httpx.Response(301, headers={"Location": final_url}, request=request)
        assert url == final_url
        return httpx.Response(200, content=html.encode(), request=request)

    def no_delay(_reader: _MindRoomWebsiteReader, min_seconds: int = 1, max_seconds: int = 3) -> None:
        assert min_seconds == 1
        assert max_seconds == 3

    monkeypatch.setattr("mindroom.custom_tools.website._server_fetch_get", fake_get)
    monkeypatch.setattr(_MindRoomWebsiteReader, "delay", no_delay)

    documents = _MindRoomWebsiteReader().read(start_url)

    assert requested_urls == [start_url, final_url]
    assert [document.meta_data["url"] for document in documents] == [final_url]
    assert "Public redirected content" in documents[0].content


def test_website_reader_respects_max_redirects(monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect loops should stop at the website reader redirect limit."""
    start_url = "https://example.com/redirect"
    requested_urls: list[str] = []

    def fake_get(url: str, *, timeout: int, follow_redirects: bool) -> httpx.Response:
        requested_urls.append(url)
        assert timeout == 10
        assert follow_redirects is False
        request = httpx.Request("GET", url)
        return httpx.Response(302, headers={"Location": start_url}, request=request)

    monkeypatch.setattr("mindroom.custom_tools.website._server_fetch_get", fake_get)

    with pytest.raises(httpx.TooManyRedirects, match=_TOO_MANY_REDIRECTS):
        _MindRoomWebsiteReader()._get_validated_response(start_url)

    assert len(requested_urls) == _MAX_REDIRECTS + 1


def test_website_reader_rejects_dns_rebind_at_connect_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """The address validated for a hostname must be the address used for the outbound connection."""

    class RebindHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = b"<html><body><main><p>Private local server content fetched after DNS rebinding.</p></main></body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *_args: object) -> None:  # noqa: A002, ARG002
            return

    server = HTTPServer(("127.0.0.1", 0), RebindHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    dns_calls = 0

    def fake_getaddrinfo(host: str | bytes, port: int, *_args: object, **_kwargs: object) -> list[object]:
        nonlocal dns_calls
        if host in ("rebind.test", b"rebind.test"):
            dns_calls += 1
            ip_address = "93.184.216.34" if dns_calls == 1 else "127.0.0.1"
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip_address, port))]
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", port))]

    def no_delay(_reader: _MindRoomWebsiteReader, min_seconds: int = 1, max_seconds: int = 3) -> None:
        assert min_seconds == 1
        assert max_seconds == 3

    monkeypatch.setattr("socket.getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(_MindRoomWebsiteReader, "delay", no_delay)

    try:
        with pytest.raises(ServerFetchUrlError) as exc_info:
            _MindRoomWebsiteReader(timeout=2).read(f"http://rebind.test:{server.server_port}/")
    finally:
        server.shutdown()
        server.server_close()

    assert exc_info.value.reason == "private_address"
    assert dns_calls >= 2


def test_website_reader_does_not_record_cross_host_redirect_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same-host crawl links that redirect to another host should not be recorded."""
    start_url = "https://example.com/"
    redirect_url = "https://example.com/redirect"
    offsite_url = "https://other.example/final"
    requested_urls: list[str] = []
    pages = {
        start_url: """
        <html>
          <body>
            <nav><a href="/redirect">Redirect</a></nav>
            <main>
              <h1>Start</h1>
              <p>Reference material with enough text to keep the starting page.</p>
            </main>
          </body>
        </html>
        """,
        offsite_url: """
        <html>
          <body>
            <main>
              <h1>Offsite</h1>
              <p>Cross-host redirected material should not be recorded.</p>
            </main>
          </body>
        </html>
        """,
    }

    def fake_get(url: str, *, timeout: int, follow_redirects: bool) -> httpx.Response:
        requested_urls.append(url)
        assert timeout == 10
        assert follow_redirects is False
        request = httpx.Request("GET", url)
        if url == redirect_url:
            return httpx.Response(302, headers={"Location": offsite_url}, request=request)
        return httpx.Response(200, content=pages[url].encode(), request=request)

    def no_delay(_reader: _MindRoomWebsiteReader, min_seconds: int = 1, max_seconds: int = 3) -> None:
        assert min_seconds == 1
        assert max_seconds == 3

    monkeypatch.setattr("mindroom.custom_tools.website._server_fetch_get", fake_get)
    monkeypatch.setattr(_MindRoomWebsiteReader, "delay", no_delay)

    documents = _MindRoomWebsiteReader().read(start_url)
    content_by_url = {document.meta_data["url"]: document.content for document in documents}

    assert requested_urls == [start_url, redirect_url, offsite_url]
    assert list(content_by_url) == [start_url]
    assert "Reference material" in content_by_url[start_url]
    assert offsite_url not in content_by_url


def test_website_reader_skips_discovered_redirect_to_private_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bad discovered redirect should not discard content that was already collected."""
    start_url = "https://example.test/docs"
    redirect_url = "https://example.test/redirect"
    private_url = "http://127.0.0.1/private"
    requested_urls: list[str] = []
    pages = {
        start_url: """
        <html>
          <body>
            <nav><a href="/redirect">Redirect</a></nav>
            <main>
              <h1>Start</h1>
              <p>Reference material with enough text to keep the starting page.</p>
            </main>
          </body>
        </html>
        """,
    }

    def fake_get(url: str, *, timeout: int, follow_redirects: bool) -> httpx.Response:
        requested_urls.append(url)
        assert timeout == 10
        assert follow_redirects is False
        request = httpx.Request("GET", url)
        if url == redirect_url:
            return httpx.Response(302, headers={"Location": private_url}, request=request)
        return httpx.Response(200, content=pages[url].encode(), request=request)

    def no_delay(_reader: _MindRoomWebsiteReader, min_seconds: int = 1, max_seconds: int = 3) -> None:
        assert min_seconds == 1
        assert max_seconds == 3

    monkeypatch.setattr("mindroom.custom_tools.website._server_fetch_get", fake_get)
    monkeypatch.setattr(_MindRoomWebsiteReader, "delay", no_delay)

    documents = _MindRoomWebsiteReader().read(start_url)

    assert requested_urls == [start_url, redirect_url]
    assert [document.meta_data["url"] for document in documents] == [start_url]
    assert "Reference material" in documents[0].content


def test_website_reader_skips_discovered_url_with_invalid_port(monkeypatch: pytest.MonkeyPatch) -> None:
    """A malformed discovered URL should not discard content that was already collected."""
    start_url = "https://example.test/docs"
    bad_url = "https://example.test:99999/bad"
    requested_urls: list[str] = []
    html = f"""
    <html>
      <body>
        <nav><a href="{bad_url}">Bad link</a></nav>
        <main>
          <h1>Start</h1>
          <p>Reference material with enough text to keep the starting page.</p>
        </main>
      </body>
    </html>
    """

    def fake_get(url: str, *, timeout: int, follow_redirects: bool) -> httpx.Response:
        requested_urls.append(url)
        assert url == start_url
        assert timeout == 10
        assert follow_redirects is False
        request = httpx.Request("GET", url)
        return httpx.Response(200, content=html.encode(), request=request)

    def no_delay(_reader: _MindRoomWebsiteReader, min_seconds: int = 1, max_seconds: int = 3) -> None:
        assert min_seconds == 1
        assert max_seconds == 3

    monkeypatch.setattr("mindroom.custom_tools.website._server_fetch_get", fake_get)
    monkeypatch.setattr(_MindRoomWebsiteReader, "delay", no_delay)

    documents = _MindRoomWebsiteReader().read(start_url)

    assert requested_urls == [start_url]
    assert [document.meta_data["url"] for document in documents] == [start_url]
    assert "Reference material" in documents[0].content


def test_website_reader_crawls_starting_url_with_port(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same-domain checks should ignore URL ports when crawling."""
    start_url = "https://example.test:8443/docs"
    html = """
    <html>
      <body>
        <main>
          <h1>Docs</h1>
          <p>Reference material with enough text to exercise ported URL crawling.</p>
        </main>
      </body>
    </html>
    """

    def fake_get(url: str, *, timeout: int, follow_redirects: bool) -> httpx.Response:
        assert url == start_url
        assert timeout == 10
        assert follow_redirects is False
        request = httpx.Request("GET", url)
        return httpx.Response(200, content=html.encode(), request=request)

    def no_delay(_reader: _MindRoomWebsiteReader, min_seconds: int = 1, max_seconds: int = 3) -> None:
        assert min_seconds == 1
        assert max_seconds == 3

    monkeypatch.setattr("mindroom.custom_tools.website._server_fetch_get", fake_get)
    monkeypatch.setattr(_MindRoomWebsiteReader, "delay", no_delay)

    documents = _MindRoomWebsiteReader().read(start_url)

    assert documents[0].meta_data["url"] == start_url
    assert "ported URL crawling" in documents[0].content


def test_website_reader_rejects_domain_suffix_lookalikes() -> None:
    """Same-host checks should not treat badexample.com as example.com."""
    reader = _MindRoomWebsiteReader()

    assert reader._should_skip_crawl_url(
        current_url="https://badexample.com/private",
        starting_url="https://example.com/",
        current_depth=1,
        num_links=0,
        crawl_host="example.com",
    )


def test_website_reader_rejects_public_suffix_sibling_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same-host crawling should not treat unrelated public-suffix siblings as in scope."""
    start_url = "https://service.co.uk/"
    attacker_url = "https://attacker.co.uk/private"
    requested_urls: list[str] = []
    pages = {
        start_url: """
        <html>
          <body>
            <nav><a href="https://attacker.co.uk/private">Attacker</a></nav>
            <main>
              <h1>Service docs</h1>
              <p>Reference material with enough text to keep the starting page.</p>
            </main>
          </body>
        </html>
        """,
        attacker_url: """
        <html>
          <body>
            <main>
              <h1>Private</h1>
              <p>Unrelated sibling host content that must never be crawled.</p>
            </main>
          </body>
        </html>
        """,
    }

    def fake_get(url: str, *, timeout: int, follow_redirects: bool) -> httpx.Response:
        requested_urls.append(url)
        assert timeout == 10
        assert follow_redirects is False
        request = httpx.Request("GET", url)
        return httpx.Response(200, content=pages[url].encode(), request=request)

    def no_delay(_reader: _MindRoomWebsiteReader, min_seconds: int = 1, max_seconds: int = 3) -> None:
        assert min_seconds == 1
        assert max_seconds == 3

    monkeypatch.setattr("mindroom.custom_tools.website._server_fetch_get", fake_get)
    monkeypatch.setattr(_MindRoomWebsiteReader, "delay", no_delay)

    documents = _MindRoomWebsiteReader().read(start_url)

    assert requested_urls == [start_url]
    assert [document.meta_data["url"] for document in documents] == [start_url]
    assert "starting page" in documents[0].content
    assert "sibling host content" not in documents[0].content


def test_safe_url_for_log_strips_userinfo_query_and_fragment() -> None:
    """Website logs should keep origin/path context without exposing secrets."""
    assert (
        _safe_url_for_log("https://user:pass@example.test:8443/docs?token=secret#private")
        == "https://example.test:8443/docs"
    )


def test_safe_url_for_log_handles_malformed_urls_without_secrets() -> None:
    """Website log sanitization should not raise on malformed caller URLs."""
    assert _safe_url_for_log("https://user:pass@example.test:99999/docs?token=secret") == "https://example.test/docs"
    assert _safe_url_for_log("http://user:pass@[not-ip]/docs?token=secret") == "http://[not-ip]/docs"


def test_website_reader_logs_sanitized_urls_without_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Website reads should not leak caller URL secrets through wrapper or reader logs."""
    full_url = "https://user:secret@example.test/docs?token=query-secret#frag-secret"
    logged_messages: list[str] = []
    html = """
    <html>
      <body>
        <main>
          <h1>Docs</h1>
          <p>Reference material with enough page text to exercise the reader.</p>
        </main>
      </body>
    </html>
    """

    class FakeKnowledge:
        def insert(self, *, url: str, reader: object) -> None:
            assert url == full_url
            assert isinstance(reader, _MindRoomWebsiteReader)

    def fake_log_debug(message: str) -> None:
        logged_messages.append(message)

    def fake_get(url: str, *, timeout: int, follow_redirects: bool) -> httpx.Response:
        assert url == full_url
        assert timeout == 10
        assert follow_redirects is False
        request = httpx.Request("GET", url)
        return httpx.Response(200, content=html.encode(), request=request)

    def no_delay(_reader: _MindRoomWebsiteReader, min_seconds: int = 1, max_seconds: int = 3) -> None:
        assert min_seconds == 1
        assert max_seconds == 3

    monkeypatch.setattr("mindroom.custom_tools.website.log_debug", fake_log_debug)
    monkeypatch.setattr("agno.knowledge.reader.website_reader.log_debug", fake_log_debug)
    monkeypatch.setattr("mindroom.custom_tools.website._server_fetch_get", fake_get)
    monkeypatch.setattr(_MindRoomWebsiteReader, "delay", no_delay)

    WebsiteTools().read_url(full_url)
    WebsiteTools(knowledge=FakeKnowledge()).add_website_to_knowledge(full_url)

    assert logged_messages == [
        "Reading website: https://example.test/docs",
        "Reading: https://example.test/docs",
        "Crawling: https://example.test/docs",
        "Adding to knowledge base: https://example.test/docs",
    ]
    joined_messages = " ".join(logged_messages)
    assert full_url not in joined_messages
    assert "user:secret" not in joined_messages
    assert "query-secret" not in joined_messages
    assert "frag-secret" not in joined_messages


def test_website_tool_adds_urls_to_knowledge_with_mindroom_reader() -> None:
    """Knowledge ingestion should use the same fixed website reader as read_url."""

    @dataclass
    class CapturedInsert:
        url: str
        reader: object

    class FakeKnowledge:
        def __init__(self) -> None:
            self.captured: CapturedInsert | None = None

        def insert(self, *, url: str, reader: object) -> None:
            self.captured = CapturedInsert(url=url, reader=reader)

    knowledge = FakeKnowledge()

    result = WebsiteTools(knowledge=knowledge).add_website_to_knowledge("https://example.test")

    assert result == "Success"
    assert knowledge.captured is not None
    assert knowledge.captured.url == "https://example.test"
    assert isinstance(knowledge.captured.reader, _MindRoomWebsiteReader)


def test_website_tool_factory_uses_mindroom_reader() -> None:
    """The configured website toolkit should use MindRoom's fixed extractor."""
    assert website_tools().__module__ == "mindroom.custom_tools.website"


def test_website_docs_describe_mindroom_reader() -> None:
    """Website docs should describe the reader users actually get."""
    docs = Path("docs/tools/web-scraping-and-browser.md").read_text(encoding="utf-8")

    assert "MindRoom's WebsiteReader variant" in docs
