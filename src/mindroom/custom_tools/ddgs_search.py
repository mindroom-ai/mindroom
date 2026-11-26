"""Native DDGS-backed web and news search toolkits."""

from __future__ import annotations

import json
from typing import Any, Literal

from ddgs import DDGS

from mindroom.logging_config import get_logger
from mindroom.tool_system.authoring import Toolkit, tool

logger = get_logger(__name__)

_VALID_TIMELIMITS = frozenset({"d", "w", "m", "y"})


class DDGSSearchTools(Toolkit):
    """Shared DDGS web and news search toolkit parameterized by backend."""

    name = "websearch"

    def __init__(
        self,
        enable_search: bool = True,
        enable_news: bool = True,
        backend: str = "auto",
        modifier: str | None = None,
        fixed_max_results: int | None = None,
        proxy: str | None = None,
        timeout: int | None = 10,
        verify_ssl: bool = True,
        timelimit: Literal["d", "w", "m", "y"] | None = None,
        region: str | None = None,
    ) -> None:
        if timelimit is not None and timelimit not in _VALID_TIMELIMITS:
            msg = f"Invalid timelimit '{timelimit}'. Must be one of: 'd' (day), 'w' (week), 'm' (month), 'y' (year)."
            raise ValueError(msg)

        self.proxy = proxy
        self.timeout = timeout
        self.fixed_max_results = fixed_max_results
        self.modifier = modifier
        self.verify_ssl = verify_ssl
        self.backend = backend
        self.timelimit = timelimit
        self.region = region

        super().__init__(name="websearch")
        if not enable_search:
            self.functions.pop(self.web_search.__name__, None)
        if not enable_news:
            self.functions.pop(self.search_news.__name__, None)

    def _run_search(self, category: Literal["text", "news"], query: str, max_results: int) -> str:
        """Execute a DDGS text or news search and return pretty-printed UTF-8 JSON."""
        actual_max_results = self.fixed_max_results or max_results
        search_kwargs: dict[str, Any] = {
            "max_results": actual_max_results,
            "backend": self.backend,
        }
        if self.timelimit is not None:
            search_kwargs["timelimit"] = self.timelimit
        if self.region is not None:
            search_kwargs["region"] = self.region

        with DDGS(proxy=self.proxy, timeout=self.timeout, verify=self.verify_ssl) as ddgs:
            results = (
                ddgs.text(query=query, **search_kwargs)
                if category == "text"
                else ddgs.news(query=query, **search_kwargs)
            )
        return json.dumps(results, indent=2, ensure_ascii=False)

    @tool
    def web_search(self, query: str, max_results: int = 5) -> str:
        """Use this function to search the web for a query.

        Args:
            query(str): The query to search for.
            max_results (optional, default=5): The maximum number of results to return.

        Returns:
            The search results from the web.

        """
        search_query = f"{self.modifier} {query}" if self.modifier else query
        logger.debug("Searching web", query=search_query, backend=self.backend)
        return self._run_search("text", search_query, max_results)

    @tool
    def search_news(self, query: str, max_results: int = 5) -> str:
        """Use this function to get the latest news from the web.

        Args:
            query(str): The query to search for.
            max_results (optional, default=5): The maximum number of results to return.

        Returns:
            The latest news from the web.

        """
        logger.debug("Searching web news", query=query, backend=self.backend)
        return self._run_search("news", query, max_results)


class DuckDuckGoTools(DDGSSearchTools):
    """DDGS web and news search toolkit defaulting to the DuckDuckGo backend."""

    def __init__(
        self,
        enable_search: bool = True,
        enable_news: bool = True,
        modifier: str | None = None,
        fixed_max_results: int | None = None,
        proxy: str | None = None,
        timeout: int | None = 10,
        verify_ssl: bool = True,
        timelimit: Literal["d", "w", "m", "y"] | None = None,
        region: str | None = None,
        backend: str | None = None,
    ) -> None:
        super().__init__(
            enable_search=enable_search,
            enable_news=enable_news,
            backend=backend or "duckduckgo",
            modifier=modifier,
            fixed_max_results=fixed_max_results,
            proxy=proxy,
            timeout=timeout,
            verify_ssl=verify_ssl,
            timelimit=timelimit,
            region=region,
        )


class GoogleSearchTools(DDGSSearchTools):
    """DDGS web and news search toolkit requesting the Google backend."""

    def __init__(
        self,
        enable_search: bool = True,
        enable_news: bool = True,
        modifier: str | None = None,
        fixed_max_results: int | None = None,
        proxy: str | None = None,
        timeout: int | None = 10,
        verify_ssl: bool = True,
        timelimit: Literal["d", "w", "m", "y"] | None = None,
        region: str | None = None,
    ) -> None:
        super().__init__(
            enable_search=enable_search,
            enable_news=enable_news,
            backend="google",
            modifier=modifier,
            fixed_max_results=fixed_max_results,
            proxy=proxy,
            timeout=timeout,
            verify_ssl=verify_ssl,
            timelimit=timelimit,
            region=region,
        )
