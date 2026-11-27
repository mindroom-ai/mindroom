"""Native Serper API toolkit for Google web, news, scholar search, and webpage scraping over httpx."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Literal

import httpx

from mindroom.logging_config import get_logger
from mindroom.tool_system.authoring import Toolkit
from mindroom.tools.native_helpers import reject_unexpected_args, require_enabled_action, restrict_actions

if TYPE_CHECKING:
    from mindroom.tool_system.tool_function import ToolFunction

logger = get_logger(__name__)

_SERPER_SEARCH_BASE_URL = "https://google.serper.dev"
_SERPER_SCRAPE_URL = "https://scrape.serper.dev"

_SerperAction = Literal["web", "news", "scholar", "scrape"]
_ACTION_FIELDS: dict[str, str] = {
    "web": "query num_results",
    "news": "query num_results",
    "scholar": "query num_results",
    "scrape": "url markdown",
}
_SEARCH_SPECS: dict[str, tuple[str, str]] = {
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

        toggles: dict[_SerperAction, bool] = {
            "web": all or enable_search,
            "news": all or enable_search_news,
            "scholar": all or enable_search_scholar,
            "scrape": all or enable_scrape_webpage,
        }
        self._enabled_actions: tuple[_SerperAction, ...] = tuple(
            action for action, enabled in toggles.items() if enabled
        )
        super().__init__(name="serper_tools", tools=[self.serper] if self._enabled_actions else [])

    def offered_function(self, function: ToolFunction) -> ToolFunction:
        """Narrow ``serper`` schema and parameters to enabled actions."""
        if function.name == "serper":
            return restrict_actions(function, self._enabled_actions, action_fields=_ACTION_FIELDS)
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

    def serper(
        self,
        action: _SerperAction,
        query: str | None = None,
        url: str | None = None,
        num_results: int | None = None,
        markdown: bool | None = None,
    ) -> str:
        """Search Google or scrape a webpage via Serper and return raw JSON.

        - `web`: Google web search for `query` (optional `num_results`).
        - `news`: Recent news search for `query` (optional `num_results`).
        - `scholar`: Google Scholar search for `query` (optional `num_results`).
        - `scrape`: Scrape `url` (optional `markdown=True` for Markdown output).
        """
        if (
            error := require_enabled_action(action, self._enabled_actions)
            or reject_unexpected_args(
                action,
                _ACTION_FIELDS,
                {
                    "query": query is not None,
                    "url": url is not None,
                    "num_results": num_results is not None,
                    "markdown": markdown is not None,
                },
            )
        ) is not None:
            return _error_json(error)
        if action == "scrape":
            if not url:
                logger.warning("No URL provided to scrape")
                return _error_json("Please provide a URL to scrape")
            logger.debug("Scraping webpage via Serper", url=url)
            return self._post("scrape", {"url": url, "includeMarkdown": bool(markdown)})
        endpoint, empty_error = _SEARCH_SPECS[action]
        if not query:
            return _error_json(empty_error)
        logger.debug("Searching via Serper", endpoint=endpoint, query=query)
        return self._post(endpoint, {"q": query, "num": num_results or self.num_results})
