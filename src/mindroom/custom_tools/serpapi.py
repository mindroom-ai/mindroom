"""Native SerpApi toolkit for Google and YouTube search."""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, quote_plus

import serpapi

from mindroom.logging_config import get_logger
from mindroom.tool_system.authoring import Toolkit, tool

if TYPE_CHECKING:
    from collections.abc import Callable

logger = get_logger(__name__)

_GOOGLE_RESULT_KEYS: tuple[tuple[str, str], ...] = (
    ("search_results", "organic_results"),
    ("recipes_results", "recipes_results"),
    ("shopping_results", "shopping_results"),
    ("knowledge_graph", "knowledge_graph"),
    ("related_questions", "related_questions"),
)
_YOUTUBE_RESULT_KEYS: tuple[tuple[str, str], ...] = (
    ("video_results", "video_results"),
    ("movie_results", "movie_results"),
    ("channel_results", "channel_results"),
)


class SerpApiTools(Toolkit):
    """Search Google and YouTube using the SerpApi client."""

    name = "serpapi_tools"

    def __init__(
        self,
        api_key: str | None = None,
        enable_search_google: bool = True,
        enable_search_youtube: bool = False,
        all: bool = False,  # noqa: A002
    ) -> None:
        self.api_key = api_key or os.getenv("SERP_API_KEY")
        if not self.api_key:
            logger.warning("No Serpapi API key provided")

        super().__init__(name="serpapi_tools")
        if not (all or enable_search_google):
            self.functions.pop(self.search_google.__name__, None)
        if not (all or enable_search_youtube):
            self.functions.pop(self.search_youtube.__name__, None)

    def _redact_api_key(self, text: str) -> str:
        """Remove raw or URL-encoded API key values from returned error text."""
        if not self.api_key:
            return text
        redacted = text
        variants = sorted({self.api_key, quote(self.api_key, safe=""), quote_plus(self.api_key)}, key=len, reverse=True)
        for variant in variants:
            if variant:
                redacted = redacted.replace(variant, "[REDACTED]")
        return redacted

    def _run_search(
        self,
        engine: str,
        query: str,
        search_cls: Callable[[dict[str, Any]], Any],
        params: dict[str, Any],
        result_keys: tuple[tuple[str, str], ...],
    ) -> str:
        """Execute one SerpApi search and project the documented result keys to JSON."""
        if not self.api_key:
            return "Please provide an API key"
        if not query:
            return "Please provide a query to search for"

        try:
            logger.info("Searching via SerpApi", engine=engine, query=query)
            results = search_cls({**params, "api_key": self.api_key}).get_dict()
            filtered_results = {output_key: results.get(source_key, "") for output_key, source_key in result_keys}
        except Exception as exc:
            return f"Error searching for the query {query}: {self._redact_api_key(str(exc))}"
        return json.dumps(filtered_results)

    @tool
    def search_google(self, query: str, num_results: int = 10) -> str:
        """Search Google using the Serpapi API. Returns the search results.

        Args:
            query(str): The query to search for.
            num_results(int): The number of results to return.

        Returns:
            str: The search results from Google.
                Keys:
                    - 'search_results': List of organic search results.
                    - 'recipes_results': List of recipes search results.
                    - 'shopping_results': List of shopping search results.
                    - 'knowledge_graph': The knowledge graph.
                    - 'related_questions': List of related questions.

        """
        return self._run_search(
            "google",
            query,
            serpapi.GoogleSearch,
            {"q": query, "num": num_results},
            _GOOGLE_RESULT_KEYS,
        )

    @tool
    def search_youtube(self, query: str) -> str:
        """Search Youtube using the Serpapi API. Returns the search results.

        Args:
            query(str): The query to search for.

        Returns:
            str: The video search results from Youtube.
                Keys:
                    - 'video_results': List of video results.
                    - 'movie_results': List of movie results.
                    - 'channel_results': List of channel results.

        """
        return self._run_search(
            "youtube",
            query,
            serpapi.YoutubeSearch,
            {"search_query": query},
            _YOUTUBE_RESULT_KEYS,
        )
