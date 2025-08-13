"""Serper tool configuration."""

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
        # Authentication
        ConfigField(
            name="api_key",
            label="API Key",
            type="password",
            required=False,
            placeholder="your-serper-api-key",
            description="Serper API key for authentication (can also be set via SERPER_API_KEY env var)",
        ),
        # Search configuration
        ConfigField(
            name="location",
            label="Location",
            type="text",
            required=False,
            default="us",
            placeholder="us",
            description="Google location code for search results (e.g., 'us', 'uk', 'ca')",
        ),
        ConfigField(
            name="language",
            label="Language",
            type="text",
            required=False,
            default="en",
            placeholder="en",
            description="Language code for search results (e.g., 'en', 'es', 'fr')",
        ),
        ConfigField(
            name="num_results",
            label="Number of Results",
            type="number",
            required=False,
            default=10,
            description="Default number of search results to retrieve",
        ),
        ConfigField(
            name="date_range",
            label="Date Range",
            type="text",
            required=False,
            placeholder="d",
            description="Default date range filter for searches (e.g., 'd' for day, 'w' for week, 'm' for month)",
        ),
    ],
    dependencies=["requests"],
    docs_url="https://docs.agno.com/tools/toolkits/search/serper",
)
def serper_tools() -> type[SerperTools]:
    """Return Serper tools for Google search, news, academic papers, and web scraping."""
    from agno.tools.serper import SerperTools

    return SerperTools
