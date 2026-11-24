"""MindRoom website tools with stricter page-content extraction."""

from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse, urlsplit, urlunsplit

import httpx
from agno.knowledge.chunking.fixed import FixedSizeChunking
from agno.knowledge.document import Document
from agno.utils.log import log_debug, log_error, log_warning
from bs4 import BeautifulSoup, Tag

from mindroom.public_fetch import PublicFetchError, PublicFetchResult, fetch_public_url_sync
from mindroom.tool_system.authoring import Toolkit

_PREFERRED_CONTENT_SELECTORS = (
    "main",
    "article",
    "[role='main']",
    "#content",
    "#main",
    "#article",
    ".page-body",
    ".main-content",
    ".post-content",
    ".entry-content",
    ".article-body",
    ".content",
)

_UNWANTED_SELECTORS = (
    "script",
    "style",
    "noscript",
    "nav",
    "header",
    "footer",
    "aside",
    "[hidden]",
    "[aria-hidden='true']",
    ".modal",
    ".search-modal",
)

_LOW_VALUE_NAME_PATTERN = re.compile(r"(?:^|[-_])(nav|navbar|menu|search|modal|header|footer|sidebar)(?:$|[-_])")
_FAILED_CRAWL_CONTENT = "Failed to extract any content"
_FAILED_STARTING_URL = "Failed to crawl starting URL"
_MAX_PAGE_BYTES = 2 * 1024 * 1024


def _normalize_text(text: str) -> str:
    """Collapse HTML extraction whitespace into stable plain text."""
    return " ".join(text.split())


def _element_text(element: Tag) -> str:
    """Return normalized visible text for a candidate content element."""
    return _normalize_text(element.get_text(" ", strip=True))


def _has_low_value_name(element: Tag) -> bool:
    """Return whether the element is likely navigation, search, or page chrome."""
    names: list[str] = []
    element_id = element.get("id")
    if isinstance(element_id, str):
        names.append(element_id.lower())
    classes = element.get("class")
    if isinstance(classes, list):
        names.extend(str(class_name).lower() for class_name in classes)
    return any(_LOW_VALUE_NAME_PATTERN.search(name) for name in names)


def _remove_unwanted_elements(soup: BeautifulSoup) -> None:
    """Remove common page chrome before selecting main text."""
    for selector in _UNWANTED_SELECTORS:
        for element in soup.select(selector):
            element.decompose()


def _best_text_candidate(candidates: list[Tag], *, min_chars: int = 40) -> str | None:
    """Return the longest non-chrome text candidate."""
    scored: list[tuple[int, str]] = []
    for candidate in candidates:
        if _has_low_value_name(candidate):
            continue
        text = _element_text(candidate)
        if len(text) < min_chars:
            continue
        scored.append((len(text), text))
    if not scored:
        return None
    return max(scored, key=lambda item: item[0])[1]


def _safe_url_for_log(url: str) -> str:
    """Return a URL safe for logs by dropping credentials, query, and fragment data."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return _safe_malformed_url_for_log(url)

    try:
        host = parts.hostname or ""
    except ValueError:
        netloc = parts.netloc.rsplit("@", maxsplit=1)[-1]
        return urlunsplit((parts.scheme, netloc, parts.path, "", ""))

    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = parts.port
    except ValueError:
        port = None
    netloc = f"{host}:{port}" if port is not None else host
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def _safe_malformed_url_for_log(url: str) -> str:
    """Best-effort URL log sanitizer for strings the standard parser rejects."""
    cleaned_url = url.split("#", maxsplit=1)[0].split("?", maxsplit=1)[0]
    scheme_separator = "://"
    if scheme_separator not in cleaned_url:
        return cleaned_url

    scheme, rest = cleaned_url.split(scheme_separator, maxsplit=1)
    authority, slash, path = rest.partition("/")
    authority = authority.rsplit("@", maxsplit=1)[-1]
    return f"{scheme}{scheme_separator}{authority}{slash}{path}"


def _normalized_hostname(url: str) -> str:
    """Return a URL's hostname for crawl host checks, IDNA-encoded and lowercase as the fetch reports page URLs."""
    try:
        return httpx.URL(url).raw_host.decode("ascii").rstrip(".")
    except httpx.InvalidURL:
        return ""


def _url_matches_crawl_host(url: str, crawl_host: str) -> bool:
    """Return whether a URL belongs to the crawl's exact starting host."""
    host = _normalized_hostname(url)
    if not host or not crawl_host:
        return False
    return host == crawl_host


# Adapted from Agno (https://github.com/agno-agi/agno), Copyright 2025-2026 Agno Inc., Apache License 2.0; modified by MindRoom.
# The crawl budget, the breadth-first queue, the 1-3 second pause between fetches, and the 5000-character page
# documents come from Agno's WebsiteReader: they bound how hard one crawl loads its host and how much text each
# returned document carries.
_MAX_DEPTH = 3
_MAX_LINKS = 10
_PAGE_CHUNKING = FixedSizeChunking(chunk_size=5000)


@dataclass
class _MindRoomWebsiteReader:
    """Crawls one host through the guarded public fetch and extracts each page's main text, skipping page chrome."""

    _visited: set[str] = field(default_factory=set, init=False, repr=False)
    _urls_to_crawl: list[tuple[str, int]] = field(default_factory=list, init=False, repr=False)

    def delay(self) -> None:
        """Pause a random 1-3 seconds between a crawl's fetches so it does not hammer one host."""
        time.sleep(random.uniform(1, 3))  # noqa: S311 - pacing, not security.

    def _queue_links(self, soup: BeautifulSoup, current_url: str, current_depth: int, crawl_host: str) -> None:
        """Queue same-host crawl links from the unmodified page soup."""
        for link in soup.find_all("a", href=True):
            if not isinstance(link, Tag):
                continue

            href_str = str(link["href"])
            full_url = urljoin(current_url, href_str)
            try:
                parsed_url = urlparse(full_url)
            except ValueError:
                continue
            if not _url_matches_crawl_host(full_url, crawl_host) or parsed_url.path.endswith(
                (".pdf", ".jpg", ".png"),
            ):
                continue

            queued = (str(full_url), current_depth + 1)
            if queued[0] not in self._visited and queued not in self._urls_to_crawl:
                self._urls_to_crawl.append(queued)

    def _should_skip_crawl_url(
        self,
        *,
        current_url: str,
        starting_url: str,
        current_depth: int,
        num_links: int,
    ) -> bool:
        """Return whether a queued crawl URL is outside the current crawl budget."""
        return (current_depth > _MAX_DEPTH and current_url != starting_url) or num_links >= _MAX_LINKS

    def _record_current_url(
        self,
        *,
        current_url: str,
        current_depth: int,
        starting_url: str,
        crawler_result: dict[str, str],
        crawl_host: str,
    ) -> int:
        """Fetch one crawl URL and return the number of content pages recorded."""
        safe_current_url = _safe_url_for_log(current_url)
        try:
            log_debug(f"Crawling: {safe_current_url}")
            page = fetch_public_url_sync(current_url, max_bytes=_MAX_PAGE_BYTES)
            # Links resolve against the page's own URL, which can name the requested one differently.
            self._visited.add(page.url)
            if current_url != starting_url and not _url_matches_crawl_host(page.url, crawl_host):
                log_debug(f"Skipping redirected URL outside crawl host: {_safe_url_for_log(page.url)}")
                return 0
            return self._record_response_content(
                page,
                current_depth=current_depth,
                crawl_host=crawl_host,
                crawler_result=crawler_result,
            )
        except PublicFetchError as e:
            log_warning(f"Failed to fetch {safe_current_url} while crawling: {e}")
            if current_url == starting_url and not crawler_result:
                raise
        except Exception as e:
            log_warning(f"Failed to crawl {safe_current_url}: {e.__class__.__name__}")
            if current_url == starting_url and not crawler_result:
                raise httpx.RequestError(_FAILED_STARTING_URL, request=None) from e

        return 0

    def _documents_from_crawl(self, crawler_result: dict[str, str], *, url: str) -> list[Document]:
        """Build one document per crawled page, named by the URL the crawl started from."""
        documents: list[Document] = []
        for crawled_url, crawled_content in crawler_result.items():
            document = Document(
                name=url,
                id=str(crawled_url),
                meta_data={"url": str(crawled_url)},
                content=crawled_content,
            )
            documents.extend(_PAGE_CHUNKING.chunk(document))
        return documents

    def _record_response_content(
        self,
        page: PublicFetchResult,
        *,
        current_depth: int,
        crawl_host: str,
        crawler_result: dict[str, str],
    ) -> bool:
        """Extract content from a response and queue crawl links from the original soup."""
        soup = BeautifulSoup(page.content, "html.parser")
        main_content = self._extract_main_content(soup)
        if main_content:
            crawler_result[page.url] = main_content

        self._queue_links(soup, page.url, current_depth, crawl_host)
        return bool(main_content)

    def crawl(self, url: str) -> dict[str, str]:
        """Crawl a website while logging only sanitized URL forms."""
        starting_url = url.strip()
        crawl_host = _normalized_hostname(starting_url)
        num_links = 0
        crawler_result: dict[str, str] = {}
        self._visited = set()
        self._urls_to_crawl = [(starting_url, 1)]
        while self._urls_to_crawl:
            current_url, current_depth = self._urls_to_crawl.pop(0)
            if current_url in self._visited or self._should_skip_crawl_url(
                current_url=current_url,
                starting_url=starting_url,
                current_depth=current_depth,
                num_links=num_links,
            ):
                continue
            if self._visited:
                self.delay()
            self._visited.add(current_url)
            num_links += self._record_current_url(
                current_url=current_url,
                current_depth=current_depth,
                starting_url=starting_url,
                crawler_result=crawler_result,
                crawl_host=crawl_host,
            )

        if not crawler_result:
            raise httpx.RequestError(_FAILED_CRAWL_CONTENT, request=None)

        return crawler_result

    def read(self, url: str) -> list[Document]:
        """Read a website while logging only sanitized URL forms."""
        log_debug(f"Reading: {_safe_url_for_log(url)}")
        try:
            return self._documents_from_crawl(self.crawl(url), url=url)
        except (PublicFetchError, httpx.RequestError) as e:
            log_error(f"Error reading website {_safe_url_for_log(url)}: {e.__class__.__name__}")
            raise

    def _extract_main_content(self, soup: BeautifulSoup) -> str:
        """Extract the best available page body text."""
        content_soup = BeautifulSoup(str(soup), "html.parser")
        _remove_unwanted_elements(content_soup)

        for selector in _PREFERRED_CONTENT_SELECTORS:
            candidates = [element for element in content_soup.select(selector) if isinstance(element, Tag)]
            best_text = _best_text_candidate(candidates)
            if best_text is not None:
                return best_text

        sections = [element for element in content_soup.find_all("section") if isinstance(element, Tag)]
        best_section = _best_text_candidate(sections)
        if best_section is not None:
            return best_section

        body = content_soup.body if isinstance(content_soup.body, Tag) else content_soup
        return _normalize_text(body.get_text(" ", strip=True))


class WebsiteTools(Toolkit):
    """Website toolkit using MindRoom's fixed extractor."""

    def __init__(self) -> None:
        super().__init__(name="website_tools", tools=[self.read_url])

    def read_url(self, url: str) -> str:
        """Read a URL and return relevant documents from the website."""
        website = _MindRoomWebsiteReader()

        log_debug(f"Reading website: {_safe_url_for_log(url)}")
        relevant_docs: list[Document] = website.read(url=url)
        return json.dumps([doc.to_dict() for doc in relevant_docs])
