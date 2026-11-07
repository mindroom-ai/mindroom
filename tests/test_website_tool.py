"""Tests for MindRoom website scraping tools."""

from __future__ import annotations

import gzip
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest
from bs4 import BeautifulSoup

from mindroom.config.main import Config, ConfigRuntimeValidationError
from mindroom.constants import resolve_runtime_paths
from mindroom.custom_tools.website import (
    _MAX_PAGE_BYTES,
    WebsiteTools,
    _MindRoomWebsiteReader,
    _safe_url_for_log,
    _url_matches_crawl_host,
)
from mindroom.public_fetch import PublicFetchError
from mindroom.tool_system.metadata import (
    ToolConfigOverrideError,
    get_tool_by_name,
    validate_authored_tool_entry_overrides,
)
from mindroom.tool_system.registry import builtin_tool_registry
from mindroom.tools.website import website_tools

if TYPE_CHECKING:
    from collections.abc import Callable


@pytest.fixture(autouse=True)
def _public_dns_for_website_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep website reader tests focused on crawler behavior instead of real DNS."""
    monkeypatch.setattr(
        "mindroom.server_fetch_url.socket.getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443)),
        ],
    )


def _serve(monkeypatch: pytest.MonkeyPatch, respond: Callable[[str], httpx.Response]) -> list[str]:
    """Answer each request MindRoom's guarded transports let through, by URL, and record the URLs requested."""
    requested: list[str] = []

    async def handle(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return respond(str(request.url))

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", handle)
    return requested


def _page(html: str | bytes, headers: dict[str, str] | None = None) -> httpx.Response:
    body = html.encode() if isinstance(html, str) else html
    return httpx.Response(200, headers=headers, stream=httpx.ByteStream(body))


def _redirect(location: str) -> httpx.Response:
    return httpx.Response(302, headers={"Location": location})


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

    def no_delay(_reader: _MindRoomWebsiteReader) -> None:
        pass

    _serve(monkeypatch, lambda url: _page(pages[url]))
    monkeypatch.setattr(_MindRoomWebsiteReader, "delay", no_delay)

    documents = json.loads(WebsiteTools().read_url(root_url))
    content_by_url = {document["meta_data"]["url"]: document["content"] for document in documents}

    assert list(content_by_url) == [root_url, docs_url]
    assert "Welcome to the product guide" in content_by_url[root_url]
    assert "Docs Navigation" not in content_by_url[root_url]
    assert "Detailed reference material" in content_by_url[docs_url]


def test_website_reader_pauses_only_between_fetches(monkeypatch: pytest.MonkeyPatch) -> None:
    """A single-page read fetches at once; a crawl pauses before each later fetch so it does not hammer the host."""
    root_url = "https://example.test/"
    docs_url = "https://example.test/docs"
    pages = {
        root_url: '<html><body><main><p>Home page.</p><a href="/docs">Docs</a></main></body></html>',
        docs_url: "<html><body><main><p>Docs page.</p></main></body></html>",
    }
    steps: list[str] = []

    def fetch(url: str) -> httpx.Response:
        steps.append(f"fetch {url}")
        return _page(pages[url])

    def record_delay(_reader: _MindRoomWebsiteReader) -> None:
        steps.append("pause")

    _serve(monkeypatch, fetch)
    monkeypatch.setattr(_MindRoomWebsiteReader, "delay", record_delay)

    _MindRoomWebsiteReader().read(docs_url)
    assert steps == [f"fetch {docs_url}"]

    steps.clear()
    _MindRoomWebsiteReader().read(root_url)
    assert steps == [f"fetch {root_url}", "pause", f"fetch {docs_url}"]


def test_website_reader_rejects_private_starting_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Agent-callable website reads should not fetch private network URLs."""
    requested_urls = _serve(monkeypatch, lambda _url: _page("private"))

    with pytest.raises(PublicFetchError, match="URL is not allowed"):
        _MindRoomWebsiteReader().read("http://127.0.0.1:8000/private")

    assert requested_urls == []


def test_website_reader_rejects_unsupported_schemes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Agent-callable website reads should only allow HTTP(S) URLs."""
    requested_urls = _serve(monkeypatch, lambda _url: _page("file"))

    with pytest.raises(PublicFetchError, match="URL is not allowed"):
        _MindRoomWebsiteReader().read("file:///etc/passwd")

    assert requested_urls == []


def test_website_reader_revalidates_redirect_targets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirects to private network URLs should be blocked before following the hop."""
    start_url = "https://example.com/redirect"

    def no_delay(_reader: _MindRoomWebsiteReader) -> None:
        pass

    requested_urls = _serve(monkeypatch, lambda _url: _redirect("http://127.0.0.1/admin"))
    monkeypatch.setattr(_MindRoomWebsiteReader, "delay", no_delay)

    with pytest.raises(PublicFetchError, match="URL is not allowed"):
        _MindRoomWebsiteReader().read(start_url)

    assert requested_urls == [start_url]


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

    def no_delay(_reader: _MindRoomWebsiteReader) -> None:
        pass

    requested_urls = _serve(monkeypatch, lambda url: _redirect(final_url) if url == start_url else _page(html))
    monkeypatch.setattr(_MindRoomWebsiteReader, "delay", no_delay)

    documents = _MindRoomWebsiteReader().read(start_url)

    assert requested_urls == [start_url, final_url]
    assert [document.meta_data["url"] for document in documents] == [final_url]
    assert "Public redirected content" in documents[0].content


def test_website_reader_refuses_a_hostname_that_resolves_to_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    """The address a hostname resolves to when the connection is made is checked, so DNS cannot reach a local server."""
    served: list[str] = []

    class LocalHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            served.append(self.path)
            body = b"<html><body><main><p>Private local server content reached through DNS.</p></main></body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *_args: object) -> None:  # noqa: A002, ARG002
            return

    server = HTTPServer(("127.0.0.1", 0), LocalHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def resolve_to_loopback(_host: str | bytes, port: int, *_args: object, **_kwargs: object) -> list[object]:
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", port))]

    monkeypatch.setattr("socket.getaddrinfo", resolve_to_loopback)

    try:
        with pytest.raises(PublicFetchError, match="private_address") as exc_info:
            _MindRoomWebsiteReader().read(f"http://rebind.test:{server.server_port}/")
    finally:
        server.shutdown()
        server.server_close()

    assert exc_info.value.code == "url_not_allowed"
    assert served == []


def test_website_reader_crawls_from_a_start_url_with_surrounding_whitespace(monkeypatch: pytest.MonkeyPatch) -> None:
    """Whitespace around the start URL neither ends the crawl at its first page nor fetches that page twice."""
    root_url = "https://example.test/"
    docs_url = "https://example.test/docs"
    pages = {
        root_url: '<main><p>Home page with enough reference text.</p><a href="/docs">Docs</a><a href="/">Home</a></main>',
        docs_url: "<main><p>Docs page with enough reference text to keep it as a document.</p></main>",
    }

    def no_delay(_reader: _MindRoomWebsiteReader) -> None:
        pass

    requested_urls = _serve(monkeypatch, lambda url: _page(pages[url]))
    monkeypatch.setattr(_MindRoomWebsiteReader, "delay", no_delay)

    documents = _MindRoomWebsiteReader().read(f"  {root_url}\n")

    assert requested_urls == [root_url, docs_url]
    assert [document.meta_data["url"] for document in documents] == [root_url, docs_url]


def test_website_reader_crawls_an_internationalized_domain(monkeypatch: pytest.MonkeyPatch) -> None:
    """A host written in Unicode and its punycode form are one crawl host, and the start page is fetched once."""
    start_url = "https://xn--bcher-kva.example/"
    relative_url = "https://xn--bcher-kva.example/relative"
    absolute_url = "https://xn--bcher-kva.example/absolute"
    pages = {
        start_url: """
        <main>
          <p>Start page with enough reference text to keep it as a document.</p>
          <a href="/relative">Relative</a>
          <a href="https://b\u00fccher.example/absolute">Absolute</a>
          <a href="/">Home</a>
        </main>
        """,
        relative_url: "<main><p>Relative page with enough reference text to keep it as a document.</p></main>",
        absolute_url: "<main><p>Absolute page with enough reference text to keep it as a document.</p></main>",
    }

    def no_delay(_reader: _MindRoomWebsiteReader) -> None:
        pass

    requested_urls = _serve(monkeypatch, lambda url: _page(pages[url]))
    monkeypatch.setattr(_MindRoomWebsiteReader, "delay", no_delay)

    documents = _MindRoomWebsiteReader().read("https://b\u00fccher.example/")

    assert requested_urls == [start_url, relative_url, absolute_url]
    assert [document.meta_data["url"] for document in documents] == [start_url, relative_url, absolute_url]


def test_website_reader_does_not_record_cross_host_redirect_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same-host crawl links that redirect to another host should not be recorded."""
    start_url = "https://example.com/"
    redirect_url = "https://example.com/redirect"
    offsite_url = "https://other.example/final"
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

    def no_delay(_reader: _MindRoomWebsiteReader) -> None:
        pass

    requested_urls = _serve(
        monkeypatch,
        lambda url: _redirect(offsite_url) if url == redirect_url else _page(pages[url]),
    )
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

    def no_delay(_reader: _MindRoomWebsiteReader) -> None:
        pass

    requested_urls = _serve(
        monkeypatch,
        lambda url: _redirect(private_url) if url == redirect_url else _page(pages[url]),
    )
    monkeypatch.setattr(_MindRoomWebsiteReader, "delay", no_delay)

    documents = _MindRoomWebsiteReader().read(start_url)

    assert requested_urls == [start_url, redirect_url]
    assert [document.meta_data["url"] for document in documents] == [start_url]
    assert "Reference material" in documents[0].content


def test_website_reader_skips_discovered_url_with_invalid_port(monkeypatch: pytest.MonkeyPatch) -> None:
    """A malformed discovered URL should not discard content that was already collected."""
    start_url = "https://example.test/docs"
    bad_url = "https://example.test:99999/bad"
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

    def no_delay(_reader: _MindRoomWebsiteReader) -> None:
        pass

    requested_urls = _serve(monkeypatch, lambda _url: _page(html))
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

    def no_delay(_reader: _MindRoomWebsiteReader) -> None:
        pass

    requested_urls = _serve(monkeypatch, lambda _url: _page(html))
    monkeypatch.setattr(_MindRoomWebsiteReader, "delay", no_delay)

    documents = _MindRoomWebsiteReader().read(start_url)

    assert requested_urls == [start_url]
    assert documents[0].meta_data["url"] == start_url
    assert "ported URL crawling" in documents[0].content


def test_website_reader_rejects_domain_suffix_lookalikes() -> None:
    """Same-host checks should not treat badexample.com as example.com."""
    assert not _url_matches_crawl_host("https://badexample.com/private", "example.com")


def test_website_reader_rejects_public_suffix_sibling_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same-host crawling should not treat unrelated public-suffix siblings as in scope."""
    start_url = "https://service.co.uk/"
    attacker_url = "https://attacker.co.uk/private"
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

    def no_delay(_reader: _MindRoomWebsiteReader) -> None:
        pass

    requested_urls = _serve(monkeypatch, lambda url: _page(pages[url]))
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
    """A read refused for the credentials in its URL logs the URL without its credentials, query, or fragment."""
    full_url = "https://user:secret@example.test/docs?token=query-secret#frag-secret"
    logged_messages: list[str] = []

    def record(message: str) -> None:
        logged_messages.append(message)

    for log_function in ("log_debug", "log_warning", "log_error"):
        monkeypatch.setattr(f"mindroom.custom_tools.website.{log_function}", record)
    requested_urls = _serve(monkeypatch, lambda _url: _page("<main><p>Docs</p></main>"))

    with pytest.raises(PublicFetchError, match="credentials_in_url"):
        WebsiteTools().read_url(full_url)

    assert requested_urls == []
    assert logged_messages == [
        "Reading website: https://example.test/docs",
        "Reading: https://example.test/docs",
        "Crawling: https://example.test/docs",
        "Failed to fetch https://example.test/docs while crawling: "
        "The URL is not allowed for a public fetch (credentials_in_url).",
        "Error reading website https://example.test/docs: PublicFetchError",
    ]
    assert "secret" not in " ".join(logged_messages)


def test_website_tool_always_offers_read_url(tmp_path: Path) -> None:
    """A configured toolkit reads URLs; no authored option swaps read_url for a knowledge-base insert."""
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "storage")

    tool = get_tool_by_name(
        "website",
        runtime_paths,
        runs_in_worker=False,
        worker_target=None,
        tools=builtin_tool_registry(),
    )

    assert list(tool.functions) == ["read_url"]
    with pytest.raises(ToolConfigOverrideError, match="unknown authored override field"):
        validate_authored_tool_entry_overrides(
            "website",
            {"knowledge": "docs"},
            tool_metadata=builtin_tool_registry().entries,
        )


def test_a_retired_knowledge_option_fails_config_load_and_says_to_remove_it(tmp_path: Path) -> None:
    """A website entry that still sets ``knowledge`` fails config load, naming the option and saying to remove it."""
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "storage")

    with pytest.raises(ConfigRuntimeValidationError) as exc_info:
        Config.validate_with_runtime(
            {
                "models": {"default": {"provider": "openai", "id": "test-model"}},
                "agents": {"researcher": {"display_name": "Researcher", "tools": [{"website": {"knowledge": "docs"}}]}},
            },
            runtime_paths,
        )

    message = str(exc_info.value)
    assert "website.knowledge: unknown authored override field(s): knowledge; remove or correct them." in message


def test_website_tool_factory_uses_mindroom_reader() -> None:
    """The configured website toolkit should use MindRoom's fixed extractor."""
    assert website_tools().__module__ == "mindroom.custom_tools.website"


def test_website_docs_describe_mindroom_reader() -> None:
    """Website docs should describe the reader users actually get."""
    docs = Path("docs/tools/web-scraping-and-browser.md").read_text(encoding="utf-8")

    assert "MindRoom's WebsiteReader variant" in docs


@pytest.mark.parametrize(
    ("headers", "body", "code"),
    [
        pytest.param(
            {"Content-Encoding": "gzip"},
            gzip.compress(b"a" * 10_000_000),
            "content_encoding",
            id="compressed",
        ),
        pytest.param({}, b"a" * (_MAX_PAGE_BYTES + 1), "too_large", id="oversized"),
        pytest.param({}, b"a" * _MAX_PAGE_BYTES, None, id="at-the-limit"),
    ],
)
def test_website_reader_reads_only_uncompressed_pages_within_the_limit(
    monkeypatch: pytest.MonkeyPatch,
    headers: dict[str, str],
    body: bytes,
    code: str | None,
) -> None:
    """A 2 MiB page is read, while a larger or compressed one fails instead of being inflated or buffered whole."""
    _serve(monkeypatch, lambda _url: _page(body, headers))

    if code is None:
        documents = _MindRoomWebsiteReader().read("https://example.com/")
        assert sum(len(document.content) for document in documents) == _MAX_PAGE_BYTES
        return
    with pytest.raises(PublicFetchError) as exc_info:
        _MindRoomWebsiteReader().read("https://example.com/")
    assert exc_info.value.code == code
