"""Serper tool configuration."""

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
    from mindroom.custom_tools.serper import SerperTools


@register_tool_with_metadata(
    name="serper",
    file_access=ToolFileAccess.NONE,
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
            env=("SERPER_API_KEY",),
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
        bool_config_field("enable_search", "Enable Search"),
        bool_config_field("enable_search_news", "Enable Search News"),
        bool_config_field("enable_search_scholar", "Enable Search Scholar"),
        bool_config_field("enable_scrape_webpage", "Enable Scrape Webpage"),
        bool_config_field("all", "All", default=False),
        ConfigField(
            name="timeout",
            label="Timeout",
            type="number",
            required=False,
            default=30,
        ),
    ],
    dependencies=["httpx"],
    docs_url="https://docs.mindroom.chat/tools/web-search/#serper",
    function_names=("serper_scrape_webpage", "serper_search"),
    action_scoped_grants=("serper_search",),
)
def serper_tools() -> type[SerperTools]:
    """Return Serper tools for Google search, news, academic papers, and web scraping."""
    from mindroom.custom_tools.serper import SerperTools

    return SerperTools
