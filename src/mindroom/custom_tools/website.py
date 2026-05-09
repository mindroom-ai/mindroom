"""MindRoom website tools with stricter page-content extraction."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit

from agno.knowledge.reader.website_reader import WebsiteReader
from agno.tools import Toolkit
from agno.utils.log import log_debug
from bs4 import BeautifulSoup, Tag

if TYPE_CHECKING:
    from agno.knowledge.document import Document
    from agno.knowledge.knowledge import Knowledge
else:
    type Document = Any
    type Knowledge = Any

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
    """Return a URL safe for logs by dropping query and fragment data."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


class _MindRoomWebsiteReader(WebsiteReader):
    """WebsiteReader variant that avoids selecting early search/navigation sections."""

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
