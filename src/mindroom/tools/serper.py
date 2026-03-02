"""Serper tool configuration."""

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
    from agno.tools.serper import SerperTools


@register_tool_with_metadata(
    name="serper",
    display_name="Serper",
    description="Search Google, news, academic papers, and scrape webpages using Serper API",
    category=ToolCategory.RESEARCH,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="FaSearch",
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
            name="location",
            label="Location",
            type="text",
            required=False,
            default="us",
        ),
        ConfigField(
            name="language",
            label="Language",
            type="text",
            required=False,
            default="en",
        ),
        ConfigField(
            name="num_results",
            label="Num Results",
            type="number",
            required=False,
            default=10,
        ),
        ConfigField(
            name="date_range",
            label="Date Range",
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
            name="enable_search_news",
            label="Enable Search News",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_search_scholar",
            label="Enable Search Scholar",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_scrape_webpage",
            label="Enable Scrape Webpage",
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
    dependencies=["requests"],
    docs_url="https://docs.agno.com/tools/toolkits/search/serper",
)
def serper_tools() -> type[SerperTools]:
    """Return Serper tools for Google search, news, academic papers, and web scraping."""
    from agno.tools.serper import SerperTools

    return SerperTools
