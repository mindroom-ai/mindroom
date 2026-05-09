"""MindRoom website tools with stricter page-content extraction."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlparse, urlsplit, urlunsplit

import httpx
from agno.knowledge.document import Document
from agno.knowledge.reader.website_reader import WebsiteReader
from agno.tools import Toolkit
from agno.utils.log import log_debug, log_error, log_warning
from bs4 import BeautifulSoup, Tag

if TYPE_CHECKING:
    from agno.knowledge.knowledge import Knowledge

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
    parts = urlsplit(url)
    host = parts.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = f"{host}:{parts.port}" if parts.port is not None else host
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def _normalized_hostname(url: str) -> str:
    """Return the normalized hostname component for crawl domain checks."""
    return (urlparse(url).hostname or "").rstrip(".").lower()


def _url_matches_crawl_host(url: str, crawl_host: str) -> bool:
    """Return whether a URL belongs to the crawl's exact starting host."""
    host = _normalized_hostname(url)
    if not host or not crawl_host:
        return False
    return host == crawl_host


class _MindRoomWebsiteReader(WebsiteReader):
    """WebsiteReader variant that avoids selecting early search/navigation sections."""

    def _queue_links(self, soup: BeautifulSoup, current_url: str, current_depth: int, crawl_host: str) -> None:
        """Queue same-host crawl links from the unmodified page soup."""
        for link in soup.find_all("a", href=True):
            if not isinstance(link, Tag):
                continue

            href_str = str(link["href"])
            full_url = urljoin(current_url, href_str)
            parsed_url = urlparse(full_url)
            if not _url_matches_crawl_host(full_url, crawl_host) or parsed_url.path.endswith(
                (".pdf", ".jpg", ".png"),
            ):
                continue

            full_url_str = str(full_url)
            if full_url_str not in self._visited and (full_url_str, current_depth + 1) not in self._urls_to_crawl:
                self._urls_to_crawl.append((full_url_str, current_depth + 1))

    def _should_skip_crawl_url(
        self,
        *,
        current_url: str,
        starting_url: str,
        current_depth: int,
        num_links: int,
        crawl_host: str,
    ) -> bool:
        """Return whether a queued crawl URL is outside the current crawl budget."""
        return (
            current_url in self._visited
            or not _url_matches_crawl_host(current_url, crawl_host)
            or (current_depth > self.max_depth and current_url != starting_url)
            or num_links >= self.max_links
        )

    def _log_http_status_error(self, *, safe_current_url: str, error: httpx.HTTPStatusError) -> None:
        """Log HTTP crawl failures without including provider exception text."""
        if 300 <= error.response.status_code < 400:
            log_debug(f"Redirect encountered for {safe_current_url}, skipping")
            return

        log_warning(f"HTTP status error while crawling {safe_current_url}: {error.response.status_code}")

    def _documents_from_crawl(
        self,
        crawler_result: dict[str, str],
        *,
        url: str,
        name: str | None,
    ) -> list[Document]:
        """Build Agno documents from crawled page content."""
        documents: list[Document] = []
        for crawled_url, crawled_content in crawler_result.items():
            document = Document(
                name=name or url,
                id=str(crawled_url),
                meta_data={"url": str(crawled_url)},
                content=crawled_content,
            )
            if self.chunk:
                documents.extend(self.chunk_document(document))
            else:
                documents.append(document)
        return documents

    def _record_response_content(
        self,
        response: httpx.Response,
        *,
        current_url: str,
        current_depth: int,
        crawl_host: str,
        crawler_result: dict[str, str],
    ) -> bool:
        """Extract content from a response and queue crawl links from the original soup."""
        soup = BeautifulSoup(response.content, "html.parser")
        main_content = self._extract_main_content(soup)
        if main_content:
            crawler_result[current_url] = main_content

        self._queue_links(soup, current_url, current_depth, crawl_host)
        return bool(main_content)

    def crawl(self, url: str, starting_depth: int = 1) -> dict[str, str]:
        """Crawl a website while logging only sanitized URL forms."""
        num_links = 0
        crawler_result: dict[str, str] = {}
        crawl_host = _normalized_hostname(url)

        self._visited = set()
        self._urls_to_crawl = [(url, starting_depth)]
        while self._urls_to_crawl:
            current_url, current_depth = self._urls_to_crawl.pop(0)
            if self._should_skip_crawl_url(
                current_url=current_url,
                starting_url=url,
                current_depth=current_depth,
                num_links=num_links,
                crawl_host=crawl_host,
            ):
                continue

            self._visited.add(current_url)
            self.delay()
            safe_current_url = _safe_url_for_log(current_url)

            try:
                log_debug(f"Crawling: {safe_current_url}")
                response = (
                    httpx.get(current_url, timeout=self.timeout, proxy=self.proxy, follow_redirects=True)
                    if self.proxy
                    else httpx.get(current_url, timeout=self.timeout, follow_redirects=True)
                )
                response.raise_for_status()
                num_links += self._record_response_content(
                    response,
                    current_url=current_url,
                    current_depth=current_depth,
                    crawl_host=crawl_host,
                    crawler_result=crawler_result,
                )
            except httpx.HTTPStatusError as e:
                self._log_http_status_error(safe_current_url=safe_current_url, error=e)
                if current_url == url and not crawler_result and not (300 <= e.response.status_code < 400):
                    raise
            except httpx.RequestError as e:
                log_warning(f"Request error while crawling {safe_current_url}: {e.__class__.__name__}")
                if current_url == url and not crawler_result:
                    raise
            except Exception as e:
                log_warning(f"Failed to crawl {safe_current_url}: {e.__class__.__name__}")
                if current_url == url and not crawler_result:
                    raise httpx.RequestError(_FAILED_STARTING_URL, request=None) from e

        if not crawler_result:
            raise httpx.RequestError(_FAILED_CRAWL_CONTENT, request=None)

        return crawler_result

    def read(self, url: str, name: str | None = None) -> list[Document]:
        """Read a website while logging only sanitized URL forms."""
        log_debug(f"Reading: {_safe_url_for_log(url)}")
        try:
            return self._documents_from_crawl(self.crawl(url), url=url, name=name)
        except (httpx.HTTPStatusError, httpx.RequestError) as e:
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
    """Agno-compatible website toolkit using MindRoom's fixed extractor."""

    def __init__(
        self,
        knowledge: Knowledge | None = None,
        **kwargs: Any,  # noqa: ANN401 - mirrors Agno Toolkit passthrough kwargs.
    ) -> None:
        self.knowledge = knowledge

        tools: list[Any] = []
        if self.knowledge is not None:
            tools.append(self.add_website_to_knowledge)
        else:
            tools.append(self.read_url)

        super().__init__(name="website_tools", tools=tools, **kwargs)

    def add_website_to_knowledge(self, url: str) -> str:
        """Add a website's content to the configured knowledge base."""
        if self.knowledge is None:
            return "Knowledge base not provided"

        log_debug(f"Adding to knowledge base: {_safe_url_for_log(url)}")
        self.knowledge.insert(url=url, reader=_MindRoomWebsiteReader())
        return "Success"

    def read_url(self, url: str) -> str:
        """Read a URL and return relevant documents from the website."""
        website = _MindRoomWebsiteReader()

        log_debug(f"Reading website: {_safe_url_for_log(url)}")
        relevant_docs: list[Document] = website.read(url=url)
        return json.dumps([doc.to_dict() for doc in relevant_docs])
