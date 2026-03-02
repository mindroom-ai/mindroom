"""ScrapeGraph tool configuration."""

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
        ConfigField(
            name="api_key",
            label="API Key",
            type="password",
            required=False,
            default=None,
        ),
        ConfigField(
            name="enable_smartscraper",
            label="Enable Smartscraper",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_markdownify",
            label="Enable Markdownify",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="enable_crawl",
            label="Enable Crawl",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="enable_searchscraper",
            label="Enable Searchscraper",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="enable_agentic_crawler",
            label="Enable Agentic Crawler",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="enable_scrape",
            label="Enable Scrape",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="render_heavy_js",
            label="Render Heavy Js",
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
    ],
    dependencies=["scrapegraph-py"],
    docs_url="https://docs.agno.com/tools/toolkits/web_scrape/scrapegraph",
)
def scrapegraph_tools() -> type[ScrapeGraphTools]:
    """Return ScrapeGraph tools for web data extraction."""
    from agno.tools.scrapegraph import ScrapeGraphTools

    return ScrapeGraphTools
