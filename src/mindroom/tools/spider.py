"""Spider web search, scraping, and crawling toolkit."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Literal

from mindroom.logging_config import get_logger
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
from mindroom.tools.native_helpers import reject_unexpected_args, require_enabled_action, restrict_actions

if TYPE_CHECKING:
    from mindroom.tool_system.tool_function import ToolFunction

logger = get_logger(__name__)

_SpiderAction = Literal["search", "scrape", "crawl"]
_ACTION_FIELDS: dict[str, str] = {
    "search": "query max_results",
    "scrape": "url",
    "crawl": "url limit",
}


class _SpiderTools(Toolkit):
    """Search, scrape, and crawl websites using Spider."""

    def __init__(
        self,
        max_results: int | None = None,
        url: str | None = None,
        optional_params: dict[str, Any] | None = None,
        enable_search: bool = True,
        enable_scrape: bool = True,
        enable_crawl: bool = True,
        all: bool = False,  # noqa: A002
    ) -> None:
        self.max_results = max_results
        self.url = url
        self.optional_params = optional_params or {}

        toggles: dict[_SpiderAction, bool] = {
            "search": enable_search or all,
            "scrape": enable_scrape or all,
            "crawl": enable_crawl or all,
        }
        self._enabled_actions: tuple[_SpiderAction, ...] = tuple(
            action for action, enabled in toggles.items() if enabled
        )
        super().__init__(name="spider", tools=(self.spider,) if self._enabled_actions else ())

    def offered_function(self, function: ToolFunction) -> ToolFunction:
        """Narrow ``spider`` schema and parameters to enabled actions."""
        if function.name == "spider":
            return restrict_actions(function, self._enabled_actions, action_fields=_ACTION_FIELDS)
        return function

    def spider(
        self,
        action: _SpiderAction,
        query: str | None = None,
        url: str | None = None,
        max_results: int | None = None,
        limit: int | None = None,
    ) -> str:
        """Search, scrape, or crawl the web via Spider and return JSON results.

        - `search`: Search for `query` (optional `max_results`, default toolkit setting or 5).
        - `scrape`: Scrape `url` as Markdown.
        - `crawl`: Crawl `url` as Markdown (optional `limit`, default 10).
        """
        error = require_enabled_action(action, self._enabled_actions) or reject_unexpected_args(
            action,
            _ACTION_FIELDS,
            {
                "query": query is not None,
                "url": url is not None,
                "max_results": max_results is not None,
                "limit": limit is not None,
            },
        )
        if error is not None:
            return f"Error: {error}"
        if action == "search" and not query:
            return "Error: query is required for search."
        if action in {"scrape", "crawl"} and not url:
            return f"Error: url is required for {action}."
        return self._run_action(action, query=query, url=url, max_results=max_results, limit=limit)

    def _run_action(
        self,
        action: _SpiderAction,
        *,
        query: str | None,
        url: str | None,
        max_results: int | None,
        limit: int | None,
    ) -> str:
        from spider import Spider

        app = Spider()
        try:
            if action == "search":
                assert query is not None
                num_results = self.max_results if self.max_results is not None else 5
                options = {"fetch_page_content": False, "num": num_results, **self.optional_params}
                # Applied after the optional_params spread so an explicit call argument always wins.
                if max_results is not None:
                    options["num"] = max_results
                logger.info("spider_search", query=query, max_results=options["num"])
                result = app.search(query, options)
            elif action == "scrape":
                assert url is not None
                logger.info("spider_scrape", url=url)
                result = app.scrape_url(url, {"return_format": "markdown", **self.optional_params})
            else:
                assert url is not None
                logger.info("spider_crawl", url=url)
                options = {"return_format": "markdown", "limit": 10, **self.optional_params}
                if limit is not None:
                    options["limit"] = limit
                result = app.crawl_url(url, options)
            return json.dumps(result)
        except Exception as exc:
            if action == "search":
                logger.exception("spider_search_failed")
                return f"Error fetching results from spider: {exc}"
            logger.exception(f"spider_{action}_failed", url=url)
            return f"Error fetching content from spider: {exc}"


@register_tool_with_metadata(
    name="spider",
    file_access=ToolFileAccess.NONE,
    display_name="Spider",
    description="Web scraper and crawler that returns LLM-ready data",
    category=ToolCategory.RESEARCH,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="FaSpider",
    icon_color="text-red-600",  # Spider red color
    config_fields=[
        ConfigField(
            name="max_results",
            label="Max Results",
            type="number",
            required=False,
            default=None,
        ),
        ConfigField(
            name="url",
            label="URL",
            type="url",
            required=False,
            default=None,
        ),
        bool_config_field("enable_search", "Enable Search"),
        bool_config_field("enable_scrape", "Enable Scrape"),
        bool_config_field("enable_crawl", "Enable Crawl"),
        bool_config_field("all", "All", default=False),
    ],
    dependencies=["spider-client"],
    docs_url="https://docs.mindroom.chat/tools/web-scraping-and-browser/#spider",
    helper_text="Get your API key from the [Spider dashboard](https://spider.cloud)",
    function_names=("spider",),
    action_scoped_grants=("spider",),
)
def spider_tools() -> type[Toolkit]:
    """Return Spider tools for web scraping and crawling."""
    return _SpiderTools
