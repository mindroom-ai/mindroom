"""Spider web search, scraping, and crawling toolkit."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

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

if TYPE_CHECKING:
    from collections.abc import Callable

logger = get_logger(__name__)


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

        tools: list[Callable[..., Any]] = []
        if enable_search or all:
            tools.append(self.search_web)
        if enable_scrape or all:
            tools.append(self.scrape)
        if enable_crawl or all:
            tools.append(self.crawl)

        super().__init__(name="spider", tools=tools)

    def search_web(self, query: str, max_results: int | None = None) -> str:
        """Use this function to search the web.

        Args:
            query: The query to search the web with.
            max_results: The maximum number of results to return. Defaults to the value set on the toolkit, or 5.

        Returns:
            The results of the search.

        """
        from spider import Spider

        app = Spider()
        try:
            num_results = self.max_results if self.max_results is not None else 5
            options = {"fetch_page_content": False, "num": num_results, **self.optional_params}
            # Applied after the optional_params spread so an explicit call argument always wins.
            if max_results is not None:
                options["num"] = max_results
            logger.info("spider_search", query=query, max_results=options["num"])
            results = app.search(query, options)
            return json.dumps(results)
        except Exception as exc:
            logger.exception("spider_search_failed")
            return f"Error fetching results from spider: {exc}"

    def scrape(self, url: str) -> str:
        """Use this function to scrape the content of a webpage.

        Args:
            url: The URL of the webpage to scrape.

        Returns:
            Markdown of the webpage.

        """
        from spider import Spider

        app = Spider()
        logger.info("spider_scrape", url=url)
        try:
            options = {"return_format": "markdown", **self.optional_params}
            results = app.scrape_url(url, options)
            return json.dumps(results)
        except Exception as exc:
            logger.exception("spider_scrape_failed", url=url)
            return f"Error fetching content from spider: {exc}"

    def crawl(self, url: str, limit: int | None = None) -> str:
        """Use this function to crawl the web.

        Args:
            url: The URL of the webpage to crawl.
            limit: The maximum number of pages to crawl. Defaults to 10.

        Returns:
            The results of the crawl.

        """
        from spider import Spider

        app = Spider()
        logger.info("spider_crawl", url=url)
        try:
            options = {"return_format": "markdown", "limit": 10, **self.optional_params}
            # Applied after the optional_params spread so an explicit call argument always wins.
            if limit is not None:
                options["limit"] = limit
            results = app.crawl_url(url, options)
            return json.dumps(results)
        except Exception as exc:
            logger.exception("spider_crawl_failed", url=url)
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
    function_names=("crawl", "scrape", "search_web"),
)
def spider_tools() -> type[Toolkit]:
    """Return Spider tools for web scraping and crawling."""
    return _SpiderTools
