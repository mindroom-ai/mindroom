"""Route Newspaper4k toolkit downloads through MindRoom's server-fetch policy."""

from __future__ import annotations

import httpx
import newspaper
from requests.utils import get_encodings_from_content

from mindroom.bounded_bytes import collect_bounded_bytes_sync
from mindroom.server_fetch_url import (
    ServerFetchHTTPTransport,
    validate_server_fetch_redirect_url,
    validate_server_fetch_url,
)

_DOWNLOAD_TIMEOUT_SECONDS = 7  # Newspaper4k's default request timeout.
_MAX_REDIRECTS = 10
_MAX_PAGE_BYTES = 10 * 1024 * 1024
_HEADERS = {"User-Agent": f"newspaper/{newspaper.__version__}", "Accept-Encoding": "identity"}
# The HTML standard requires a charset declaration within the first 1024 bytes; scanning only those keeps
# the declaration regexes linear on hostile pages.
_DECLARED_CHARSET_PREFIX_BYTES = 1024


class _TextOnlyArticle(newspaper.Article):
    """Article whose parse skips image extraction, which downloads image URLs listed in the page."""

    def fetch_images(self) -> None:
        """Leave image fields empty; the toolkit returns only text fields."""


def _decode_page(body: bytes, header_charset: str | None) -> str:
    """Decode a page as newspaper4k does: the Content-Type charset, else the charset the page declares, else UTF-8."""
    charset = header_charset
    if not charset:
        prefix = body[:_DECLARED_CHARSET_PREFIX_BYTES].decode("utf-8", errors="replace")
        charset = next(iter(get_encodings_from_content(prefix)), "utf-8")
    try:
        return body.decode(charset, errors="replace")
    except LookupError:
        # Python has no codec for the named charset; newspaper4k falls back to UTF-8 the same way.
        return body.decode("utf-8", errors="replace")


def _download_html(url: str) -> str:
    """Download one page after validating the URL, every redirect hop, and each dialed address.

    Bodies are requested uncompressed and read raw, so the size limit bounds memory before any decoding.
    """
    request_url = validate_server_fetch_url(url)
    with httpx.Client(
        transport=ServerFetchHTTPTransport(),
        headers=_HEADERS,
        timeout=_DOWNLOAD_TIMEOUT_SECONDS,
    ) as client:
        for _redirect_count in range(_MAX_REDIRECTS + 1):
            with client.stream("GET", request_url) as response:
                if not response.is_redirect:
                    response.raise_for_status()
                    if response.headers.get("content-encoding", "identity").strip().lower() not in ("", "identity"):
                        msg = "The page must use identity content encoding."
                        raise httpx.DecodingError(msg, request=response.request)
                    body = collect_bounded_bytes_sync(response.iter_raw(), max_bytes=_MAX_PAGE_BYTES)
                    return _decode_page(body, response.charset_encoding)
                location = response.headers.get("location")
            request_url = validate_server_fetch_redirect_url(request_url, location)
    msg = "Newspaper4k download stopped after too many redirects."
    raise httpx.TooManyRedirects(msg)


def _guarded_article(url: str) -> newspaper.Article:
    """Download and parse one article without any download newspaper4k would make itself."""
    article = _TextOnlyArticle(url)
    article.download(input_html=_download_html(url))
    article.parse()
    return article


def install_server_fetch_guard() -> None:
    """Replace the article downloader Newspaper4kTools calls with the guarded one."""
    # AGNO_COMPAT: Newspaper4kTools downloads through newspaper4k's unguarded article helper.
    # Reason: Agno 3.0.9 get_article_data calls newspaper.article(url), which opens file:// paths,
    # follows redirects to any address, and downloads image URLs listed in the page, so model-chosen
    # URLs and fetched pages reached local files and loopback, private, and metadata targets from
    # the MindRoom process.
    # Upstream issue: Tracking gap; no matching issue identified on October 1, 2026.
    # Upstream PR: None identified for an injectable Newspaper4kTools fetcher.
    # Remove when: Newspaper4kTools accepts caller-supplied HTML or a fetcher and can skip image
    # downloads; retain validation of each URL, redirect hop, and dialed address.
    # Coverage: tests/test_newspaper_tool.py::test_newspaper_rejects_unsafe_targets_without_connecting,
    # ::test_newspaper_revalidates_redirects_before_following,
    # ::test_newspaper_extracts_public_article_without_fetching_its_images,
    # ::test_newspaper_decodes_page_with_charset_declared_only_in_meta, and
    # tests/test_tools_metadata.py::test_local_url_fetch_tools_do_not_contact_loopback_targets.
    newspaper.article = _guarded_article  # ty: ignore[invalid-assignment]
