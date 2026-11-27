"""Native Serper API toolkit for Google web, news, scholar search, and webpage scraping over httpx."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Literal, Optional

import httpx

from mindroom.logging_config import get_logger
from mindroom.tool_system.authoring import Toolkit
from mindroom.tools.native_helpers import require_enabled_action, restrict_actions

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.tool_system.tool_function import ToolFunction

logger = get_logger(__name__)

_SERPER_SEARCH_BASE_URL = "https://google.serper.dev"
_SERPER_SCRAPE_URL = "https://scrape.serper.dev"

_SearchAction = Literal["web", "news", "scholar"]
_SEARCH_SPECS: dict[_SearchAction, tuple[str, str]] = {
    "web": ("search", "Please provide a query to search for"),
    "news": ("news", "Please provide a query to search for news"),
    "scholar": ("scholar", "Please provide a query to search for academic papers"),
}


def _error_json(message: str) -> str:
    """Return a pretty-printed JSON error envelope."""
    return json.dumps({"error": message}, indent=2)


class SerperTools(Toolkit):
    """Search Google, news, and Scholar and scrape webpages via the Serper API."""

    name = "serper_tools"

    def __init__(
        self,
        api_key: str | None = None,
        location: str = "us",
        language: str = "en",
        num_results: int = 10,
        date_range: str | None = None,
        enable_search: bool = True,
        enable_search_news: bool = True,
        enable_search_scholar: bool = True,
        enable_scrape_webpage: bool = True,
        all: bool = False,  # noqa: A002
        timeout: int = 30,
    ) -> None:
        self.api_key = api_key
        if not self.api_key:
            logger.debug("No Serper API key provided")

        self.location = location
        self.language = language
        self.num_results = num_results
        self.date_range = date_range
        self.timeout = timeout

        toggles: dict[_SearchAction, bool] = {
            "web": all or enable_search,
            "news": all or enable_search_news,
            "scholar": all or enable_search_scholar,
        }
        self._search_actions: tuple[_SearchAction, ...] = tuple(action for action in _SEARCH_SPECS if toggles[action])

        tools: list[Callable[..., Any]] = []
        if self._search_actions:
            tools.append(self.serper_search)
        if all or enable_scrape_webpage:
            tools.append(self.scrape_webpage)
        super().__init__(name="serper_tools", tools=tools)

    def offered_function(self, function: ToolFunction) -> ToolFunction:
        """Narrow ``serper_search`` schema to enabled search actions."""
        if function.name == "serper_search":
            return restrict_actions(function, self._search_actions)
        return function

    def _post(self, endpoint: str, params: dict[str, Any]) -> str:
        """Send one POST request to Serper and return raw JSON text or a JSON error."""
        if not self.api_key:
            logger.error("No Serper API key provided")
            return _error_json("Please provide a Serper API key")

        url = _SERPER_SCRAPE_URL if endpoint == "scrape" else f"{_SERPER_SEARCH_BASE_URL}/{endpoint}"
        headers = {"X-API-KEY": self.api_key, "Content-Type": "application/json"}
        payload = dict(params)
        if self.date_range:
            payload["tbs"] = self.date_range
        if self.location:
            payload["gl"] = self.location
        if self.language:
            payload["hl"] = self.language

        try:
            logger.debug("Making Serper API request", url=url, endpoint=endpoint, params=payload)
            response = httpx.post(url, headers=headers, json=payload, timeout=self.timeout)
            response.raise_for_status()
            response.json()
        except Exception as exc:
            logger.exception("Serper API error", endpoint=endpoint)
            return _error_json(str(exc))
        return response.text

    def serper_search(
        self,
        query: str,
        action: _SearchAction,
        num_results: Optional[int] = None,  # noqa: UP045 - Optional keeps null out of the tool schema.
    ) -> str:
        """Search Google via Serper for `query` (optional `num_results`).

        - `web`: Web search.
        - `news`: Recent news articles.
        - `scholar`: Google Scholar academic papers.
        """
        if (disabled := require_enabled_action(action, self._search_actions)) is not None:
            return _error_json(disabled)
        endpoint, empty_error = _SEARCH_SPECS[action]
        if not query:
            return _error_json(empty_error)
        logger.debug("Searching via Serper", endpoint=endpoint, query=query)
        return self._post(endpoint, {"q": query, "num": num_results or self.num_results})

    def scrape_webpage(self, url: str, markdown: bool = False) -> str:
        """Scrapes and extracts content from a webpage using the Serper scraping API.

        Args:
            url (str): The URL of the webpage to scrape.
            markdown (bool, optional): Return content in markdown format (default: False).

        Returns:
            str: A JSON-formatted string containing the scraped webpage content or an error message.

        """
        if not url:
            logger.warning("No URL provided to scrape")
            return _error_json("Please provide a URL to scrape")
        logger.debug("Scraping webpage via Serper", url=url)
        return self._post("scrape", {"url": url, "includeMarkdown": markdown})
