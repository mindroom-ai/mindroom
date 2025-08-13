"""BrightData tool configuration."""

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
    from agno.tools.brightdata import BrightDataTools


@register_tool_with_metadata(
    name="brightdata",
    display_name="BrightData",
    description="Web scraping, search engine queries, screenshots, and structured data extraction",
    category=ToolCategory.RESEARCH,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="FaSpider",
    icon_color="text-blue-600",
    config_fields=[
        # Authentication
        ConfigField(
            name="api_key",
            label="API Key",
            type="password",
            required=False,
            placeholder="your_brightdata_api_key",
            description="BrightData API key (can also be set via BRIGHT_DATA_API_KEY env var)",
        ),
        # Zone configuration
        ConfigField(
            name="serp_zone",
            label="SERP Zone",
            type="text",
            required=False,
            default="serp_api",
            placeholder="serp_api",
            description="Zone for search engine requests (can be overridden with BRIGHT_DATA_SERP_ZONE env var)",
        ),
        ConfigField(
            name="web_unlocker_zone",
            label="Web Unlocker Zone",
            type="text",
            required=False,
            default="web_unlocker1",
            placeholder="web_unlocker1",
            description="Zone for web scraping requests (can be overridden with BRIGHT_DATA_WEB_UNLOCKER_ZONE env var)",
        ),
        # Feature toggles
        ConfigField(
            name="scrape_as_markdown",
            label="Scrape as Markdown",
            type="boolean",
            required=False,
            default=True,
            description="Enable the scrape_as_markdown tool",
        ),
        ConfigField(
            name="get_screenshot",
            label="Get Screenshot",
            type="boolean",
            required=False,
            default=False,
            description="Enable the get_screenshot tool",
        ),
        ConfigField(
            name="search_engine",
            label="Search Engine",
            type="boolean",
            required=False,
            default=True,
            description="Enable the search_engine tool",
        ),
        ConfigField(
            name="web_data_feed",
            label="Web Data Feed",
            type="boolean",
            required=False,
            default=True,
            description="Enable the web_data_feed tool",
        ),
        # Configuration
        ConfigField(
            name="verbose",
            label="Verbose Logging",
            type="boolean",
            required=False,
            default=False,
            description="Enable verbose logging",
        ),
        ConfigField(
            name="timeout",
            label="Timeout",
            type="number",
            required=False,
            default=600,
            placeholder="600",
            description="Timeout in seconds for web data feed requests",
        ),
    ],
    dependencies=["requests"],
    docs_url="https://docs.agno.com/tools/toolkits/web_scrape/brightdata",
)
def brightdata_tools() -> type[BrightDataTools]:
    """Return BrightData tools for web scraping and data extraction."""
    from agno.tools.brightdata import BrightDataTools

    return BrightDataTools
