"""Native DDGS-backed web and news search toolkits."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Literal, get_args

from ddgs import DDGS

from mindroom.logging_config import get_logger
from mindroom.tool_system.authoring import Toolkit
from mindroom.tools.native_helpers import require_enabled_action, restrict_actions

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.tool_system.tool_function import ToolFunction

logger = get_logger(__name__)

_VALID_TIMELIMITS = frozenset({"d", "w", "m", "y"})
_SearchAction = Literal["web", "news"]
_ALL_SEARCH_ACTIONS: tuple[_SearchAction, ...] = get_args(_SearchAction)


class _DDGSSearchTools(Toolkit):
    """Shared DDGS web and news search toolkit parameterized by backend."""

    name = "websearch"

    def __init__(
        self,
        *,
        search_fn: Callable[..., Any],
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

        toggles = {"web": enable_search, "news": enable_news}
        self._enabled_actions: tuple[_SearchAction, ...] = tuple(
            action for action in _ALL_SEARCH_ACTIONS if toggles[action]
        )
        super().__init__(name="websearch", tools=(search_fn,) if self._enabled_actions else ())

    def offered_function(self, function: ToolFunction) -> ToolFunction:
        """Narrow the search action schema to enabled actions."""
        return restrict_actions(function, self._enabled_actions)

    def _dispatch_search(
        self,
        query: str,
        action: _SearchAction,
        max_results: int | None,
    ) -> str:
        if (disabled := require_enabled_action(action, self._enabled_actions)) is not None:
            return json.dumps(
                {"error": "DisabledAction", "message": disabled, "query": query, "results": []},
                indent=2,
                ensure_ascii=False,
            )
        category: Literal["text", "news"] = "text" if action == "web" else "news"
        return self._run_search(category, query, 5 if max_results is None else max_results)

    def _run_search(self, category: Literal["text", "news"], query: str, max_results: int) -> str:
        """Execute a DDGS text or news search and return pretty-printed UTF-8 JSON."""
        actual_max_results = self.fixed_max_results if self.fixed_max_results is not None else max_results
        if actual_max_results <= 0:
            return "[]"

        search_query = f"{self.modifier} {query}" if self.modifier else query
        logger.debug("Searching DDGS", category=category, query=search_query, backend=self.backend)
        search_kwargs: dict[str, Any] = {
            "max_results": actual_max_results,
            "backend": self.backend,
        }
        if self.timelimit is not None:
            search_kwargs["timelimit"] = self.timelimit
        if self.region is not None:
            search_kwargs["region"] = self.region

        try:
            with DDGS(proxy=self.proxy, timeout=self.timeout, verify=self.verify_ssl) as ddgs:
                search = ddgs.text if category == "text" else ddgs.news
                results = search(query=search_query, **search_kwargs)
            return json.dumps(results, indent=2, ensure_ascii=False)
        except Exception as exc:
            logger.exception("DDGS search failed", category=category, query=search_query, backend=self.backend)
            return json.dumps(
                {"error": type(exc).__name__, "message": str(exc), "query": search_query, "results": []},
                indent=2,
                ensure_ascii=False,
            )


class DuckDuckGoTools(_DDGSSearchTools):
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
            search_fn=self.duckduckgo_search,
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

    def duckduckgo_search(
        self,
        query: str,
        action: _SearchAction,
        max_results: int | None = None,
    ) -> str:
        """Search DuckDuckGo for `query` (optional `max_results`, default 5).

        - `web`: Web search.
        - `news`: Recent news articles.
        """
        return self._dispatch_search(query, action, max_results)


class GoogleSearchTools(_DDGSSearchTools):
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
            search_fn=self.google_search,
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

    def google_search(
        self,
        query: str,
        action: _SearchAction,
        max_results: int | None = None,
    ) -> str:
        """Search Google for `query` (optional `max_results`, default 5).

        - `web`: Web search.
        - `news`: Recent news articles.
        """
        return self._dispatch_search(query, action, max_results)
