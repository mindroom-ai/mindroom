"""Firecrawl tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tools_metadata import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolStatus,
    register_tool_with_metadata,
)

if TYPE_CHECKING:
    from agno.tools.firecrawl import FirecrawlTools


@register_tool_with_metadata(
    name="firecrawl",
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
            type="text",
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
            name="search_params",
            label="Search Params",
            type="text",
            required=False,
            default=None,
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
    docs_url="https://docs.agno.com/tools/toolkits/web_scrape/firecrawl",
)
def firecrawl_tools() -> type[FirecrawlTools]:
    """Return Firecrawl tools for web scraping and crawling."""
    from agno.tools.firecrawl import FirecrawlTools

    return FirecrawlTools
