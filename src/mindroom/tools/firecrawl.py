"""Firecrawl web scraping, crawling, mapping, and search toolkit."""

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


def _serialize(value: Any) -> str:  # noqa: ANN401
    """Serialize a Firecrawl response or Pydantic model to JSON."""
    payload = value.model_dump() if hasattr(value, "model_dump") else value
    return json.dumps(payload, default=str)


class _FirecrawlTools(Toolkit):
    """Scrape, crawl, map, and search websites using Firecrawl."""

    def __init__(
        self,
        api_key: str | None = None,
        enable_scrape: bool = True,
        enable_crawl: bool = False,
        enable_mapping: bool = False,
        enable_search: bool = False,
        all: bool = False,  # noqa: A002
        formats: list[str] | None = None,
        limit: int = 10,
        poll_interval: int = 30,
        search_params: dict[str, Any] | None = None,
        api_url: str | None = "https://api.firecrawl.dev",
    ) -> None:
        from firecrawl import FirecrawlApp

        self.api_key = api_key
        if not self.api_key:
            logger.error("firecrawl_api_key_missing")

        self.formats = formats
        self.limit = limit
        self.poll_interval = poll_interval
        firecrawl_app: Any = FirecrawlApp
        self.app: Any = firecrawl_app(api_key=self.api_key, api_url=api_url)
        self.search_params = search_params

        tools: list[Callable[..., Any]] = []
        if all or enable_scrape:
            tools.append(self.firecrawl_scrape_website)
        if all or enable_crawl:
            tools.append(self.firecrawl_crawl_website)
        if all or enable_mapping:
            tools.append(self.firecrawl_map_website)
        if all or enable_search:
            tools.append(self.firecrawl_search_web)

        super().__init__(name="firecrawl_tools", tools=tools)

    def firecrawl_scrape_website(self, url: str) -> str:
        """Use this function to scrape a website using Firecrawl.

        Args:
            url: The URL to scrape.

        """
        params: dict[str, Any] = {}
        if self.formats:
            params["formats"] = self.formats
        scrape_result = self.app.scrape(url, **params)
        return _serialize(scrape_result)

    def firecrawl_crawl_website(self, url: str, limit: int | None = None) -> str:
        """Use this function to crawl a website using Firecrawl.

        Args:
            url: The URL to crawl.
            limit: The maximum number of pages to crawl. Defaults to the limit set on the toolkit.

        Returns:
            The results of the crawling.

        """
        from firecrawl.types import ScrapeOptions

        params: dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        elif self.limit is not None:
            params["limit"] = self.limit
        if self.formats:
            scrape_options_cls: Any = ScrapeOptions
            params["scrape_options"] = scrape_options_cls(formats=self.formats)
        params["poll_interval"] = self.poll_interval

        crawl_result = self.app.crawl(url, **params)
        return _serialize(crawl_result)

    def firecrawl_map_website(self, url: str) -> str:
        """Use this function to map a website using Firecrawl.

        Args:
            url: The URL to map.

        """
        map_result = self.app.map(url)
        return _serialize(map_result)

    def firecrawl_search_web(self, query: str, limit: int | None = None) -> str:
        """Use this function to search the web using Firecrawl.

        Args:
            query: The query to search for.
            limit: The maximum number of results to return. Defaults to the limit set on the toolkit.

        """
        from firecrawl.types import ScrapeOptions

        params: dict[str, Any] = {}
        if self.limit is not None:
            params["limit"] = self.limit
        if self.formats:
            scrape_options_cls: Any = ScrapeOptions
            params["scrape_options"] = scrape_options_cls(formats=self.formats)
        if self.search_params:
            params.update(self.search_params)
        # Applied after search_params so an explicit call argument always wins.
        if limit is not None:
            params["limit"] = limit

        search_result = self.app.search(query, **params)
        if hasattr(search_result, "success"):
            if search_result.success:
                return _serialize(search_result.data)
            return f"Error searching with the Firecrawl tool: {search_result.error}"
        return _serialize(search_result)


@register_tool_with_metadata(
    name="firecrawl",
    file_access=ToolFileAccess.NONE,
    display_name="Firecrawl",
    description="Web scraping and crawling tool for extracting content from websites",
    category=ToolCategory.RESEARCH,  # Web scraping tool for research
    status=ToolStatus.REQUIRES_CONFIG,  # Requires API key
    setup_type=SetupType.API_KEY,  # API key authentication
    icon="FaSpider",  # Web crawler icon
    icon_color="text-orange-500",  # Orange color for fire/crawling theme
    config_fields=[
        ConfigField(
            name="api_key",
            env=("FIRECRAWL_API_KEY",),
            label="API Key",
            type="password",
            required=False,
            default=None,
        ),
        bool_config_field("enable_scrape", "Enable Scrape"),
        bool_config_field("enable_crawl", "Enable Crawl", default=False),
        bool_config_field("enable_mapping", "Enable Mapping", default=False),
        bool_config_field("enable_search", "Enable Search", default=False),
        bool_config_field("all", "All", default=False),
        ConfigField(
            name="formats",
            label="Formats",
            type="string[]",
            required=False,
            default=None,
        ),
        ConfigField(
            name="limit",
            label="Limit",
            type="number",
            required=False,
            default=10,
        ),
        ConfigField(
            name="poll_interval",
            label="Poll Interval",
            type="number",
            required=False,
            default=30,
        ),
        ConfigField(
            name="api_url",
            label="API URL",
            type="url",
            required=False,
            default="https://api.firecrawl.dev",
        ),
    ],
    dependencies=["firecrawl-py"],
    docs_url="https://docs.mindroom.chat/tools/web-scraping-and-browser/#firecrawl",
    function_names=(
        "firecrawl_crawl_website",
        "firecrawl_map_website",
        "firecrawl_scrape_website",
        "firecrawl_search_web",
    ),
)
def firecrawl_tools() -> type[Toolkit]:
    """Return Firecrawl tools for web scraping and crawling."""
    return _FirecrawlTools
