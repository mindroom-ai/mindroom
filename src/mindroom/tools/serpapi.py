"""SerpApi tool configuration."""

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
        ConfigField(
            name="api_key",
            label="API Key",
            type="password",
            required=False,
            default=None,
        ),
        ConfigField(
            name="enable_search_google",
            label="Enable Search Google",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_search_youtube",
            label="Enable Search Youtube",
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
    dependencies=["google-search-results"],
    docs_url="https://docs.agno.com/tools/toolkits/search/serpapi",
)
def serpapi_tools() -> type[SerpApiTools]:
    """Return SerpApi tools for Google and YouTube search."""
    from agno.tools.serpapi import SerpApiTools

    return SerpApiTools
