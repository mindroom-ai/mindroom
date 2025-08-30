"""ScrapeGraph tool configuration."""

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
    from agno.tools.scrapegraph import ScrapeGraphTools


@register_tool_with_metadata(
    name="scrapegraph",
    display_name="ScrapeGraph",
    description="Extract structured data from webpages using AI and natural language prompts",
    category=ToolCategory.RESEARCH,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="FaGlobe",
    icon_color="text-blue-600",
    config_fields=[
        # Authentication
        ConfigField(
            name="api_key",
            label="API Key",
            type="password",
            required=False,
            placeholder="sgai_...",
            description="ScrapeGraph API key for enhanced services (can also be set via SGAI_API_KEY env var)",
        ),
        # Tool features
        ConfigField(
            name="smartscraper",
            label="Smart Scraper",
            type="boolean",
            required=False,
            default=True,
            description="Enable AI-powered data extraction using natural language prompts",
        ),
        ConfigField(
            name="markdownify",
            label="Markdownify",
            type="boolean",
            required=False,
            default=False,
            description="Enable webpage to markdown conversion",
        ),
        ConfigField(
            name="crawl",
            label="Crawl",
            type="boolean",
            required=False,
            default=False,
            description="Enable website crawling and structured data extraction",
        ),
        ConfigField(
            name="searchscraper",
            label="Search Scraper",
            type="boolean",
            required=False,
            default=False,
            description="Enable web search and information extraction",
        ),
        ConfigField(
            name="agentic_crawler",
            label="Agentic Crawler",
            type="boolean",
            required=False,
            default=False,
            description="Enable automated browser actions and optional AI extraction",
        ),
    ],
    dependencies=["scrapegraph-py"],
    docs_url="https://docs.agno.com/tools/toolkits/web_scrape/scrapegraph",
)
def scrapegraph_tools() -> type[ScrapeGraphTools]:
    """Return ScrapeGraph tools for web data extraction."""
    from agno.tools.scrapegraph import ScrapeGraphTools

    return ScrapeGraphTools
