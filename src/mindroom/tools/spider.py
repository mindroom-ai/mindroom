"""Spider tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.metadata import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolStatus,
    register_tool_with_metadata,
)

if TYPE_CHECKING:
    from agno.tools.spider import SpiderTools


@register_tool_with_metadata(
    name="spider",
    display_name="Spider",
    description="Web scraper and crawler that returns LLM-ready data",
    category=ToolCategory.RESEARCH,  # Based on web_scrape category in docs
    status=ToolStatus.AVAILABLE,  # API key handled via SPIDER_API_KEY env var
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
        ConfigField(
            name="optional_params",
            label="Optional Params",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="enable_search",
            label="Enable Search",
            type="boolean",
            required=False,
            default=True,
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
            default=True,
        ),
        ConfigField(
            name="all",
            label="All",
            type="boolean",
            required=False,
            default=False,
        ),
    ],
    dependencies=["spider-client"],
    docs_url="https://docs.agno.com/tools/toolkits/web_scrape/spider",
    helper_text="Get your API key from the [Spider dashboard](https://spider.cloud)",
)
def spider_tools() -> type[SpiderTools]:
    """Return Spider tools for web scraping and crawling."""
    from agno.tools.spider import SpiderTools

    return SpiderTools
