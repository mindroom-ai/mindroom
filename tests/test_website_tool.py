"""Tests for MindRoom website scraping tools."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
from bs4 import BeautifulSoup

from mindroom.custom_tools.website import WebsiteTools, _MindRoomWebsiteReader, _safe_url_for_log
from mindroom.tools.website import website_tools

if TYPE_CHECKING:
    import pytest


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
        assert follow_redirects is True
        request = httpx.Request("GET", url)
        return httpx.Response(200, content=pages[url].encode(), request=request)

    def no_delay(_reader: _MindRoomWebsiteReader, min_seconds: int = 1, max_seconds: int = 3) -> None:
        assert min_seconds == 1
        assert max_seconds == 3

    monkeypatch.setattr("agno.knowledge.reader.website_reader.httpx.get", fake_get)
    monkeypatch.setattr(_MindRoomWebsiteReader, "delay", no_delay)

    documents = json.loads(WebsiteTools().read_url(root_url))
    content_by_url = {document["meta_data"]["url"]: document["content"] for document in documents}

    assert list(content_by_url) == [root_url, docs_url]
    assert "Welcome to the product guide" in content_by_url[root_url]
    assert "Docs Navigation" not in content_by_url[root_url]
    assert "Detailed reference material" in content_by_url[docs_url]


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
        assert follow_redirects is True
        request = httpx.Request("GET", url)
        return httpx.Response(200, content=html.encode(), request=request)

    def no_delay(_reader: _MindRoomWebsiteReader, min_seconds: int = 1, max_seconds: int = 3) -> None:
        assert min_seconds == 1
        assert max_seconds == 3

    monkeypatch.setattr("mindroom.custom_tools.website.httpx.get", fake_get)
    monkeypatch.setattr(_MindRoomWebsiteReader, "delay", no_delay)

    documents = _MindRoomWebsiteReader().read(start_url)

    assert documents[0].meta_data["url"] == start_url
    assert "ported URL crawling" in documents[0].content


def test_website_reader_rejects_domain_suffix_lookalikes() -> None:
    """Same-domain checks should not treat badexample.com as example.com."""
    reader = _MindRoomWebsiteReader()

    primary_domain = reader._get_primary_domain("https://example.com/")

    assert reader._should_skip_crawl_url(
        current_url="https://badexample.com/private",
        starting_url="https://example.com/",
        current_depth=1,
        num_links=0,
        primary_domain=primary_domain,
    )


def test_safe_url_for_log_strips_userinfo_query_and_fragment() -> None:
    """Website logs should keep origin/path context without exposing secrets."""
    assert (
        _safe_url_for_log("https://user:pass@example.test:8443/docs?token=secret#private")
        == "https://example.test:8443/docs"
    )


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
        assert follow_redirects is True
        request = httpx.Request("GET", url)
        return httpx.Response(200, content=html.encode(), request=request)

    def no_delay(_reader: _MindRoomWebsiteReader, min_seconds: int = 1, max_seconds: int = 3) -> None:
        assert min_seconds == 1
        assert max_seconds == 3

    monkeypatch.setattr("mindroom.custom_tools.website.log_debug", fake_log_debug)
    monkeypatch.setattr("agno.knowledge.reader.website_reader.log_debug", fake_log_debug)
    monkeypatch.setattr("agno.knowledge.reader.website_reader.httpx.get", fake_get)
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
