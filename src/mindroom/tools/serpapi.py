"""SerpApi tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolFileAccess,
    ToolStatus,
    bool_config_field,
)
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.serpapi import SerpApiTools


@register_tool_with_metadata(
    name="serpapi",
    file_access=ToolFileAccess.NONE,
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
            env=("SERP_API_KEY",),
            label="API Key",
            type="password",
            required=False,
            default=None,
        ),
        bool_config_field("enable_search_google", "Enable Search Google"),
        bool_config_field("enable_search_youtube", "Enable Search Youtube", default=False),
        bool_config_field("all", "All", default=False),
    ],
    dependencies=["google-search-results"],
    docs_url="https://docs.mindroom.chat/tools/web-search/#serpapi",
    function_names=("search_google", "search_youtube"),
)
def serpapi_tools() -> type[SerpApiTools]:
    """Return SerpApi tools for Google and YouTube search."""
    from mindroom.custom_tools.serpapi import SerpApiTools

    return SerpApiTools
