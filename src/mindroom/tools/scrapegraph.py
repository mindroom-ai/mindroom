"""ScrapeGraphAI structured extraction, markdown conversion, search, and crawl toolkit."""

from __future__ import annotations

import json
import time
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


class _ScrapeGraphTools(Toolkit):
    """Web scraping, structured extraction, search, and crawl via the ScrapeGraphAI API."""

    def __init__(
        self,
        api_key: str | None = None,
        enable_smartscraper: bool = True,
        enable_markdownify: bool = False,
        enable_searchscraper: bool = False,
        enable_crawl: bool = False,
        enable_scrape: bool = False,
        render_heavy_js: bool = False,
        headers: dict[str, str] | None = None,
        crawl_poll_interval: int = 3,
        crawl_max_wait: int = 180,
        all: bool = False,  # noqa: A002
    ) -> None:
        from scrapegraph_py import ScrapeGraphAI

        self.api_key = api_key
        if not self.api_key:
            logger.error("scrapegraph_api_key_missing")

        self.client = ScrapeGraphAI(api_key=self.api_key)
        self.render_heavy_js = render_heavy_js
        self.headers = headers
        self.crawl_poll_interval = crawl_poll_interval
        self.crawl_max_wait = crawl_max_wait

        tools: list[Callable[..., Any]] = []
        if all or enable_smartscraper:
            tools.append(self.scrapegraph_smartscraper)
        if all or enable_markdownify:
            tools.append(self.scrapegraph_markdownify)
        if all or enable_searchscraper:
            tools.append(self.scrapegraph_searchscraper)
        if all or enable_crawl:
            tools.append(self.scrapegraph_crawl)
        if all or enable_scrape:
            tools.append(self.scrapegraph_scrape)

        super().__init__(name="scrapegraph_tools", tools=tools)

    def _fetch_config(self) -> Any:  # noqa: ANN401
        from scrapegraph_py import FetchConfig

        config_kwargs: dict[str, Any] = {}
        if self.render_heavy_js:
            config_kwargs["mode"] = "js"
        if self.headers:
            config_kwargs["headers"] = self.headers
        return FetchConfig(**config_kwargs) if config_kwargs else None

    def scrapegraph_smartscraper(self, url: str, prompt: str) -> str:
        """Extract structured data from a webpage using an AI prompt.

        Args:
            url: The URL to scrape.
            prompt: Natural language prompt describing what to extract.

        Returns:
            JSON string with the extracted data, or an error message.

        """
        try:
            logger.debug("scrapegraph_smartscraper", url=url)
            response = self.client.extract(prompt=prompt, url=url, fetch_config=self._fetch_config())
            if response.status != "success" or response.data is None:
                return f"Error extracting from {url}: {response.error or 'unknown error'}"
            payload = response.data.json_data if response.data.json_data is not None else response.data.raw
            return json.dumps(payload)
        except Exception as error:
            return f"Error extracting from {url}: {type(error).__name__}: {error}"

    def scrapegraph_markdownify(self, url: str) -> str:
        """Convert a webpage to markdown.

        Args:
            url: The URL to convert.

        Returns:
            The markdown content of the webpage, or an error message.

        """
        try:
            from scrapegraph_py import MarkdownFormatConfig

            logger.debug("scrapegraph_markdownify", url=url)
            response = self.client.scrape(
                url,
                formats=[MarkdownFormatConfig()],
                fetch_config=self._fetch_config(),
            )
            if response.status != "success" or response.data is None:
                return f"Error converting {url} to markdown: {response.error or 'unknown error'}"
            markdown_field = response.data.results.get("markdown", {})
            if isinstance(markdown_field, dict):
                return str(markdown_field.get("data", ""))
            return str(markdown_field)
        except Exception as error:
            return f"Error converting {url} to markdown: {type(error).__name__}: {error}"

    def scrapegraph_searchscraper(self, query: str) -> str:
        """Search the web and extract information from the top results.

        Args:
            query: The search query.

        Returns:
            JSON string with the search results, or an error message.

        """
        try:
            logger.debug("scrapegraph_searchscraper", query_length=len(query))
            response = self.client.search(query, fetch_config=self._fetch_config())
            if response.status != "success" or response.data is None:
                return f"Error searching: {response.error or 'unknown error'}"
            return response.data.model_dump_json(by_alias=True)
        except Exception as error:
            return f"Error searching: {type(error).__name__}: {error}"

    def scrapegraph_crawl(
        self,
        url: str,
        prompt: str,
        schema: dict[str, Any],
        max_depth: int = 2,
        max_pages: int = 2,
    ) -> str:
        """Crawl a website and extract structured data across multiple pages.

        Starts a crawl job upstream and polls until it completes or ``crawl_max_wait`` elapses.

        Args:
            url: The URL to crawl.
            prompt: Natural language prompt describing what to extract.
            schema: JSON schema for extraction.
            max_depth: Max crawl depth. Defaults to 2.
            max_pages: Max number of pages to crawl. Defaults to 2.

        Returns:
            JSON string with the crawl result, or an error message.

        """
        try:
            from scrapegraph_py import JsonFormatConfig

            logger.debug("scrapegraph_crawl_start", url=url)
            start_response = self.client.crawl.start(
                url,
                formats=[JsonFormatConfig(prompt=prompt, schema=schema)],
                max_depth=max_depth,
                max_pages=max_pages,
                fetch_config=self._fetch_config(),
            )
            if start_response.status != "success" or start_response.data is None:
                return f"Error starting crawl of {url}: {start_response.error or 'unknown error'}"

            crawl_data = start_response.data
            crawl_id = crawl_data.id
            deadline = time.monotonic() + self.crawl_max_wait
            while crawl_data.status == "running":
                if time.monotonic() > deadline:
                    return f"Error: crawl timed out after {self.crawl_max_wait}s (id={crawl_id})"
                time.sleep(self.crawl_poll_interval)
                status_response = self.client.crawl.get(crawl_id)
                if status_response.status != "success" or status_response.data is None:
                    return f"Error polling crawl {crawl_id}: {status_response.error or 'unknown error'}"
                crawl_data = status_response.data

            return crawl_data.model_dump_json(by_alias=True)
        except Exception as error:
            return f"Error crawling {url}: {type(error).__name__}: {error}"

    def scrapegraph_scrape(self, url: str) -> str:
        """Get raw HTML content from a webpage.

        Args:
            url: The URL to scrape.

        Returns:
            JSON string with the scrape result, or an error message.

        """
        try:
            from scrapegraph_py import HtmlFormatConfig

            logger.debug("scrapegraph_scrape", url=url)
            response = self.client.scrape(
                url,
                formats=[HtmlFormatConfig()],
                fetch_config=self._fetch_config(),
            )
            if response.status != "success" or response.data is None:
                return f"Error scraping {url}: {response.error or 'unknown error'}"
            return response.data.model_dump_json(by_alias=True)
        except Exception as error:
            return f"Error scraping {url}: {type(error).__name__}: {error}"


@register_tool_with_metadata(
    name="scrapegraph",
    file_access=ToolFileAccess.NONE,
    display_name="ScrapeGraph",
    description="Extract structured data from webpages using AI and natural language prompts",
    category=ToolCategory.RESEARCH,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="FaGlobe",
    icon_color="text-blue-600",
    config_fields=[
        ConfigField(
            name="api_key",
            env=("SGAI_API_KEY",),
            label="API Key",
            type="password",
            required=False,
            default=None,
        ),
        bool_config_field("enable_smartscraper", "Enable Smartscraper"),
        bool_config_field("enable_markdownify", "Enable Markdownify", default=False),
        bool_config_field("enable_crawl", "Enable Crawl", default=False),
        bool_config_field("enable_searchscraper", "Enable Searchscraper", default=False),
        bool_config_field("enable_scrape", "Enable Scrape", default=False),
        bool_config_field("render_heavy_js", "Render Heavy Js", default=False),
        ConfigField(
            name="crawl_poll_interval",
            label="Crawl Poll Interval",
            type="number",
            required=False,
            default=3,
        ),
        ConfigField(
            name="crawl_max_wait",
            label="Crawl Max Wait",
            type="number",
            required=False,
            default=180,
        ),
        bool_config_field("all", "All", default=False),
    ],
    dependencies=["scrapegraph-py"],
    docs_url="https://docs.mindroom.chat/tools/web-scraping-and-browser/#scrapegraph",
    function_names=(
        "scrapegraph_crawl",
        "scrapegraph_markdownify",
        "scrapegraph_scrape",
        "scrapegraph_searchscraper",
        "scrapegraph_smartscraper",
    ),
)
def scrapegraph_tools() -> type[Toolkit]:
    """Return ScrapeGraph tools for web data extraction."""
    return _ScrapeGraphTools
