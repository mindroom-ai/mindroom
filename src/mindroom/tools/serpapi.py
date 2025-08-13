"""SerpApi tool configuration."""

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
    from agno.tools.serpapi import SerpApiTools


@register_tool_with_metadata(
    name="serpapi",
    display_name="SerpApi",
    description="Google and YouTube search using SerpApi",
    category=ToolCategory.RESEARCH,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="FaGoogle",
    icon_color="text-blue-500",
    config_fields=[
        # Authentication
        ConfigField(
            name="api_key",
            label="API Key",
            type="password",
            required=False,
            placeholder="YOUR_SERPAPI_KEY",
            description="SerpApi API key for authentication (can also be set via SERP_API_KEY env var)",
        ),
        # Search features
        ConfigField(
            name="search_youtube",
            label="Search YouTube",
            type="boolean",
            required=False,
            default=False,
            description="Enable YouTube search functionality",
        ),
    ],
    dependencies=["google-search-results"],
    docs_url="https://docs.agno.com/tools/toolkits/search/serpapi",
)
def serpapi_tools() -> type[SerpApiTools]:
    """Return SerpApi tools for Google and YouTube search."""
    from agno.tools.serpapi import SerpApiTools

    return SerpApiTools
