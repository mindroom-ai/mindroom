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
        ConfigField(
            name="api_key",
            label="API Key",
            type="password",
            required=False,
            default=None,
        ),
        ConfigField(
            name="enable_scrape_markdown",
            label="Enable Scrape Markdown",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_screenshot",
            label="Enable Screenshot",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_search_engine",
            label="Enable Search Engine",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_web_data_feed",
            label="Enable Web Data Feed",
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
        ConfigField(
            name="serp_zone",
            label="Serp Zone",
            type="text",
            required=False,
            default="serp_api",
        ),
        ConfigField(
            name="web_unlocker_zone",
            label="Web Unlocker Zone",
            type="text",
            required=False,
            default="web_unlocker1",
        ),
        ConfigField(
            name="verbose",
            label="Verbose",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="timeout",
            label="Timeout",
            type="number",
            required=False,
            default=600,
        ),
    ],
    dependencies=["requests"],
    docs_url="https://docs.agno.com/tools/toolkits/web_scrape/brightdata",
)
def brightdata_tools() -> type[BrightDataTools]:
    """Return BrightData tools for web scraping and data extraction."""
    from agno.tools.brightdata import BrightDataTools

    return BrightDataTools
