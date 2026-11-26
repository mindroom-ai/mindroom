"""Firecrawl web scraping, crawling, mapping, and search toolkit."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from mindroom.logging_config import get_logger
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.declarations import ConfigField, SetupType, ToolCategory, ToolFileAccess, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from collections.abc import Callable

logger = get_logger(__name__)


def _require_firecrawl() -> tuple[type[Any], type[Any]]:
    """Import and return Firecrawl SDK classes, raising a helpful ImportError when absent."""
    try:
        from firecrawl import FirecrawlApp
        from firecrawl.types import ScrapeOptions
    except ImportError as exc:
        msg = "`firecrawl-py` not installed. Please install using `pip install firecrawl-py`"
        raise ImportError(msg) from exc
    return FirecrawlApp, ScrapeOptions


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
        firecrawl_app_cls, _ = _require_firecrawl()
        self.api_key = api_key
        if not self.api_key:
            logger.error("firecrawl_api_key_missing")

        self.formats = formats
        self.limit = limit
        self.poll_interval = poll_interval
        self.app = firecrawl_app_cls(api_key=self.api_key, api_url=api_url)
        self.search_params = search_params

        tools: list[Callable[..., Any]] = []
        if all or enable_scrape:
            tools.append(self.scrape_website)
        if all or enable_crawl:
            tools.append(self.crawl_website)
        if all or enable_mapping:
            tools.append(self.map_website)
        if all or enable_search:
            tools.append(self.search_web)

        super().__init__(name="firecrawl_tools", tools=tools)

    def scrape_website(self, url: str) -> str:
        """Use this function to scrape a website using Firecrawl.

        Args:
            url: The URL to scrape.

        """
        params: dict[str, Any] = {}
        if self.formats:
            params["formats"] = self.formats
        scrape_result = self.app.scrape(url, **params)
        return _serialize(scrape_result)

    def crawl_website(self, url: str, limit: int | None = None) -> str:
        """Use this function to crawl a website using Firecrawl.

        Args:
            url: The URL to crawl.
            limit: The maximum number of pages to crawl. Defaults to the limit set on the toolkit.

        Returns:
            The results of the crawling.

        """
        _, scrape_options_cls = _require_firecrawl()
        params: dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        elif self.limit is not None:
            params["limit"] = self.limit
        if self.formats:
            params["scrape_options"] = scrape_options_cls(formats=self.formats)
        params["poll_interval"] = self.poll_interval

        crawl_result = self.app.crawl(url, **params)
        return _serialize(crawl_result)

    def map_website(self, url: str) -> str:
        """Use this function to map a website using Firecrawl.

        Args:
            url: The URL to map.

        """
        map_result = self.app.map(url)
        return _serialize(map_result)

    def search_web(self, query: str, limit: int | None = None) -> str:
        """Use this function to search the web using Firecrawl.

        Args:
            query: The query to search for.
            limit: The maximum number of results to return. Defaults to the limit set on the toolkit.

        """
        _, scrape_options_cls = _require_firecrawl()
        params: dict[str, Any] = {}
        if self.limit is not None:
            params["limit"] = self.limit
        if self.formats:
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
        ConfigField(
            name="enable_scrape",
            label="Enable Scrape",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_crawl",
            label="Enable Crawl",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="enable_mapping",
            label="Enable Mapping",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="enable_search",
            label="Enable Search",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="all",
            label="All",
            type="boolean",
            required=False,
            default=False,
        ),
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
    function_names=("crawl_website", "map_website", "scrape_website", "search_web"),
)
def firecrawl_tools() -> type[Toolkit]:
    """Return Firecrawl tools for web scraping and crawling."""
    _require_firecrawl()
    return _FirecrawlTools
