"""Newspaper4k article extraction toolkit."""

from __future__ import annotations

import json
import re
from email.message import Message
from functools import cache
from typing import TYPE_CHECKING, Any

from mindroom.logging_config import get_logger
from mindroom.public_fetch import fetch_public_url_sync
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.declarations import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolFileAccess,
    ToolStatus,
    bool_config_field,
)
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from collections.abc import Callable

logger = get_logger(__name__)

_MAX_PAGE_BYTES = 10 * 1024 * 1024
# The HTML standard requires a charset declaration within the first 1024 bytes; scanning only those keeps
# the declaration regexes linear on hostile pages.
_DECLARED_CHARSET_PREFIX_BYTES = 1024
_META_CHARSET_RE = re.compile(r'<meta.*?charset=["\']*(.+?)["\'>]', flags=re.IGNORECASE)
_META_PRAGMA_RE = re.compile(r'<meta.*?content=["\']*;?charset=(.+?)["\'>]', flags=re.IGNORECASE)
_XML_ENCODING_RE = re.compile(r'^<\?xml.*?encoding=["\']*(.+?)["\'>]')


def _declared_charsets(prefix: str) -> list[str]:
    return _META_CHARSET_RE.findall(prefix) + _META_PRAGMA_RE.findall(prefix) + _XML_ENCODING_RE.findall(prefix)


def _decode_page(body: bytes, content_type: str | None) -> str:
    """Decode a page using its Content-Type charset, else its declared meta/XML charset, else UTF-8."""
    header = Message()
    header["Content-Type"] = content_type or ""
    charset = header.get_content_charset()
    if not charset:
        prefix = body[:_DECLARED_CHARSET_PREFIX_BYTES].decode("utf-8", errors="replace")
        charset = next(iter(_declared_charsets(prefix)), "utf-8")
    try:
        return body.decode(charset, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


@cache
def _text_only_article_cls(article_cls: type[Any]) -> type[Any]:
    """Return a cached Article subclass whose parse skips image downloads."""

    class _TextOnlyArticle(article_cls):
        """Article whose parse skips image extraction, which would otherwise fetch arbitrary image URLs."""

        def fetch_images(self) -> None:
            """Leave image fields empty; the toolkit returns only text fields."""

    return _TextOnlyArticle


def _guarded_article(url: str) -> Any:  # noqa: ANN401 - optional newspaper.Article instance
    """Download and parse one article through the shared SSRF guard, skipping image downloads."""
    import newspaper

    page = fetch_public_url_sync(url, max_bytes=_MAX_PAGE_BYTES, user_agent=f"newspaper/{newspaper.__version__}")
    article = _text_only_article_cls(newspaper.Article)(url)
    article.download(input_html=_decode_page(page.content, page.content_type))
    article.parse()
    return article


class _Newspaper4kTools(Toolkit):
    """Extract article text and metadata from public HTTP(S) URLs using Newspaper4k."""

    def __init__(
        self,
        article_length: int | None = None,
        enable_read_article: bool = True,
        all: bool = False,  # noqa: A002
    ) -> None:
        import newspaper  # noqa: F401 - warm optional import at toolkit init

        self.article_length = article_length

        tools: list[Callable[..., Any]] = []
        if all or enable_read_article:
            tools.append(self.newspaper_read_article)

        super().__init__(name="newspaper4k_tools", tools=tools)

    def get_article_data(self, url: str) -> dict[str, Any] | None:
        """Read and get article data from a URL.

        Args:
            url: The URL of the article.

        Returns:
            The extracted article data dictionary, or None on failure.

        """
        try:
            article = _guarded_article(url)
            article_data: dict[str, Any] = {}
            if article.title:
                article_data["title"] = article.title
            if article.authors:
                article_data["authors"] = article.authors
            if article.text:
                article_data["text"] = article.text

            try:
                if article.publish_date:
                    article_data["publish_date"] = article.publish_date.isoformat()
            except Exception:
                logger.debug("newspaper_publish_date_parse_failed", url=url)
        except Exception as exc:
            logger.warning("newspaper_read_article_failed", url=url, error=str(exc))
            return None
        else:
            return article_data

    def newspaper_read_article(self, url: str) -> str:
        """Use this function to read an article from a URL.

        Args:
            url: The URL of the article.

        Returns:
            JSON containing the article author, publish date, and text.

        """
        try:
            logger.debug("newspaper_read_article", url=url)
            article_data = self.get_article_data(url)
            if not article_data:
                return f"Error reading article from {url}: No data found."

            if self.article_length and "text" in article_data:
                article_data["text"] = article_data["text"][: self.article_length]

            return json.dumps(article_data, indent=2)
        except Exception as exc:
            return f"Error reading article from {url}: {exc}"


@register_tool_with_metadata(
    name="newspaper",
    file_access=ToolFileAccess.NONE,
    display_name="Newspaper",
    description="Read and extract content from news articles using advanced web scraping",
    category=ToolCategory.RESEARCH,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="FaNewspaper",
    icon_color="text-blue-600",
    config_fields=[
        ConfigField(
            name="article_length",
            label="Article Length",
            type="number",
            required=False,
            default=None,
        ),
        bool_config_field("enable_read_article", "Enable Read Article"),
        bool_config_field("all", "All", default=False),
    ],
    dependencies=["newspaper4k", "lxml_html_clean"],
    docs_url="https://docs.mindroom.chat/tools/web-scraping-and-browser/#newspaper",
    function_names=("newspaper_read_article",),
)
def newspaper4k_tools() -> type[Toolkit]:
    """Return Newspaper4k tools whose downloads follow the server-fetch policy."""
    return _Newspaper4kTools
