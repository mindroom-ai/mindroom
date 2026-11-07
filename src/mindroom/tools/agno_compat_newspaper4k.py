"""Route Newspaper4k toolkit downloads through MindRoom's server-fetch policy."""

from __future__ import annotations

from email.message import Message

import newspaper
from requests.utils import get_encodings_from_content

from mindroom.public_fetch import fetch_public_url_sync

_MAX_PAGE_BYTES = 10 * 1024 * 1024
_USER_AGENT = f"newspaper/{newspaper.__version__}"
# The HTML standard requires a charset declaration within the first 1024 bytes; scanning only those keeps
# the declaration regexes linear on hostile pages.
_DECLARED_CHARSET_PREFIX_BYTES = 1024


class _TextOnlyArticle(newspaper.Article):
    """Article whose parse skips image extraction, which downloads image URLs listed in the page."""

    def fetch_images(self) -> None:
        """Leave image fields empty; the toolkit returns only text fields."""


def _decode_page(body: bytes, content_type: str | None) -> str:
    """Decode a page as newspaper4k does: the Content-Type charset, else the charset the page declares, else UTF-8."""
    header = Message()
    header["Content-Type"] = content_type or ""
    charset = header.get_content_charset()
    if not charset:
        prefix = body[:_DECLARED_CHARSET_PREFIX_BYTES].decode("utf-8", errors="replace")
        charset = next(iter(get_encodings_from_content(prefix)), "utf-8")
    try:
        return body.decode(charset, errors="replace")
    except LookupError:
        # Python has no codec for the named charset; newspaper4k falls back to UTF-8 the same way.
        return body.decode("utf-8", errors="replace")


def _guarded_article(url: str) -> newspaper.Article:
    """Download and parse one article without any download newspaper4k would make itself."""
    page = fetch_public_url_sync(url, max_bytes=_MAX_PAGE_BYTES, user_agent=_USER_AGENT)
    article = _TextOnlyArticle(url)
    article.download(input_html=_decode_page(page.content, page.content_type))
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
    # whose redirect case is
    # ::test_newspaper_rejects_unsafe_targets_without_connecting[https://example.com/redirect-to-loopback],
    # ::test_newspaper_extracts_public_article_without_fetching_its_images,
    # ::test_newspaper_decodes_page_with_charset_declared_only_in_meta, and
    # tests/test_tools_metadata.py::test_local_url_fetch_tools_do_not_contact_loopback_targets.
    newspaper.article = _guarded_article  # ty: ignore[invalid-assignment]
