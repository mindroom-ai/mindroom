"""Native Serper API toolkit for Google web, news, scholar search, and webpage scraping over httpx."""

from __future__ import annotations

import json
from typing import Any, Optional

import httpx

from mindroom.logging_config import get_logger
from mindroom.tool_system.authoring import Toolkit, tool

logger = get_logger(__name__)

_SERPER_SEARCH_BASE_URL = "https://google.serper.dev"
_SERPER_SCRAPE_URL = "https://scrape.serper.dev"


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

        super().__init__(name="serper_tools")
        if not (all or enable_search):
            self.functions.pop(self.search_web.__name__, None)
        if not (all or enable_search_news):
            self.functions.pop(self.search_news.__name__, None)
        if not (all or enable_search_scholar):
            self.functions.pop(self.search_scholar.__name__, None)
        if not (all or enable_scrape_webpage):
            self.functions.pop(self.scrape_webpage.__name__, None)

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

    def _search(self, endpoint: str, query: str, num_results: int | None, *, empty_error: str) -> str:
        """Validate a search query and dispatch to a Serper search endpoint."""
        if not query:
            return _error_json(empty_error)
        logger.debug("Searching via Serper", endpoint=endpoint, query=query)
        return self._post(endpoint, {"q": query, "num": num_results or self.num_results})

    @tool
    def search_web(
        self,
        query: str,
        num_results: Optional[int] = None,  # noqa: UP045 - Optional keeps null out of the tool schema.
    ) -> str:
        """Searches Google for the provided query using the Serper API.

        Args:
            query (str): The search query to search for on Google.
            num_results (int, optional): Number of search results to retrieve.

        Returns:
            str: A JSON-formatted string containing the search results or an error message if the search fails.

        """
        return self._search("search", query, num_results, empty_error="Please provide a query to search for")

    @tool
    def search_news(
        self,
        query: str,
        num_results: Optional[int] = None,  # noqa: UP045 - Optional keeps null out of the tool schema.
    ) -> str:
        """Searches for news articles using the Serper News API.

        Args:
            query (str): The search query for news articles.
            num_results (int, optional): Number of news results to retrieve.

        Returns:
            str: A JSON-formatted string containing the news search results or an error message.

        """
        return self._search("news", query, num_results, empty_error="Please provide a query to search for news")

    @tool
    def search_scholar(
        self,
        query: str,
        num_results: Optional[int] = None,  # noqa: UP045 - Optional keeps null out of the tool schema.
    ) -> str:
        """Searches for academic papers using Google Scholar via Serper API.

        Args:
            query (str): The search query for academic papers.
            num_results (int, optional): Number of academic papers to retrieve.

        Returns:
            str: A JSON-formatted string containing the scholar search results or an error message.

        """
        return self._search(
            "scholar",
            query,
            num_results,
            empty_error="Please provide a query to search for academic papers",
        )

    @tool
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
