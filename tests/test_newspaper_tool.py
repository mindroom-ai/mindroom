"""Tests for the Newspaper4k toolkit's server-side fetch policy."""

from __future__ import annotations

import json
import socket
from typing import TYPE_CHECKING

import httpx
import newspaper
import pytest

import mindroom.tools.agno_compat_newspaper4k as compat
from mindroom.tools.newspaper4k import newspaper4k_tools
from tests.public_fetch_helpers import serve_routes

if TYPE_CHECKING:
    from pathlib import Path

_ARTICLE_HTML = """
<html>
  <head>
    <title>Matrix bridges</title>
    <meta property="og:image" content="http://169.254.169.254/latest/meta-data/">
  </head>
  <body>
    <article>
      <h1>Matrix bridges</h1>
      <p>Bridges connect Matrix rooms to other chat networks so people can keep talking across every platform.</p>
      <p>Each bridge relays messages in both directions and preserves the conversation history for later readers.</p>
      <p>Operators register the bridge as an application service before inviting its bot into portal rooms.</p>
      <img src="http://127.0.0.1:8765/api/config/raw">
    </article>
  </body>
</html>
"""


def _public_getaddrinfo(_host: str | bytes, port: int, *_args: object, **_kwargs: object) -> list[object]:
    return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", port))]


@pytest.fixture
def connect_attempts(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Resolve every name to a public address, then record and refuse every outbound socket connection."""
    monkeypatch.setattr(socket, "getaddrinfo", _public_getaddrinfo)
    attempts: list[object] = []

    def refuse(_sock: socket.socket, address: object) -> None:
        attempts.append(address)
        raise ConnectionRefusedError(address)

    monkeypatch.setattr(socket.socket, "connect", refuse)
    return attempts


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8765/api/config/raw",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/admin",
        "{article_file}",
        "https://example.com/redirect-to-loopback",
    ],
)
def test_newspaper_rejects_unsafe_targets_without_connecting(
    url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    connect_attempts: list[object],
) -> None:
    """Local files, internal addresses, and public redirects to them are refused before any connection."""
    article_file = tmp_path / "article.html"
    article_file.write_text(_ARTICLE_HTML, encoding="utf-8")
    url = url.format(article_file=article_file.as_uri())
    redirect = httpx.Response(302, headers={"Location": "http://127.0.0.1:8765/api/config/raw"})
    requests = serve_routes(monkeypatch, {"https://example.com/redirect-to-loopback": redirect})

    result = newspaper4k_tools()().read_article(url)

    assert result == f"Error reading article from {url}: No data found."
    assert connect_attempts == []
    assert {str(request.url) for request in requests} <= {"https://example.com/redirect-to-loopback"}


def test_newspaper_extracts_public_article_without_fetching_its_images(
    monkeypatch: pytest.MonkeyPatch,
    connect_attempts: list[object],
) -> None:
    """A public article still extracts, while the image URLs it lists are never requested."""
    url = "https://example.com/article"
    requests = serve_routes(
        monkeypatch,
        {url: httpx.Response(200, headers={"Content-Type": "text/html; charset=utf-8"}, content=_ARTICLE_HTML)},
    )

    result = json.loads(newspaper4k_tools()().read_article(url))

    assert result["title"] == "Matrix bridges"
    assert "relays messages in both directions" in result["text"]
    assert [str(request.url) for request in requests] == [url]
    assert requests[0].headers["user-agent"] == f"newspaper/{newspaper.__version__}"
    assert connect_attempts == []


def test_newspaper_refuses_a_page_over_its_size_limit(
    monkeypatch: pytest.MonkeyPatch,
    connect_attempts: list[object],
) -> None:
    """A page larger than the download limit is refused instead of buffered and parsed."""
    url = "https://example.com/article"
    serve_routes(monkeypatch, {url: httpx.Response(200, headers={"Content-Type": "text/html"}, content=_ARTICLE_HTML)})
    monkeypatch.setattr(compat, "_MAX_PAGE_BYTES", 64)

    result = newspaper4k_tools()().read_article(url)

    assert result == f"Error reading article from {url}: No data found."
    assert connect_attempts == []


@pytest.mark.parametrize(
    ("charset", "encoding"),
    [
        pytest.param("iso-8859-1", "iso-8859-1", id="declared"),
        pytest.param("not-a-charset", "utf-8", id="unknown-falls-back-to-utf-8"),
    ],
)
def test_newspaper_decodes_page_with_charset_declared_only_in_meta(
    charset: str,
    encoding: str,
    monkeypatch: pytest.MonkeyPatch,
    connect_attempts: list[object],
) -> None:
    """A page whose charset appears only in a meta tag is decoded with that charset."""
    url = "https://example.com/artikel"
    page = _ARTICLE_HTML.replace("<head>", f'<head>\n    <meta charset="{charset}">').replace(
        "Bridges connect",
        "Brücken verbinden Räume. Bridges connect",
    )
    serve_routes(
        monkeypatch,
        {url: httpx.Response(200, headers={"Content-Type": "text/html"}, content=page.encode(encoding))},
    )

    result = json.loads(newspaper4k_tools()().read_article(url))

    assert "Brücken verbinden Räume." in result["text"]
    assert connect_attempts == []


def test_newspaper_decodes_page_with_charset_declared_only_in_content_type(
    monkeypatch: pytest.MonkeyPatch,
    connect_attempts: list[object],
) -> None:
    """A page whose charset appears only in its Content-Type header is decoded with that charset."""
    url = "https://example.com/artikel"
    page = _ARTICLE_HTML.replace("Bridges connect", "Brücken verbinden Räume. Bridges connect")
    serve_routes(
        monkeypatch,
        {
            url: httpx.Response(
                200,
                headers={"Content-Type": "text/html; charset=iso-8859-1"},
                content=page.encode("iso-8859-1"),
            ),
        },
    )

    result = json.loads(newspaper4k_tools()().read_article(url))

    assert "Brücken verbinden Räume." in result["text"]
    assert connect_attempts == []
