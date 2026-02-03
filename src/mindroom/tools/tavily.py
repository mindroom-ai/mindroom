"""Tavily tool configuration."""

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
    from agno.tools.tavily import TavilyTools


@register_tool_with_metadata(
    name="tavily",
    display_name="Tavily",
    description="Real-time web search API for retrieving current information",
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
            name="api_base_url",
            label="API Base URL",
            type="url",
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
            name="enable_search_context",
            label="Enable Search Context",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="enable_extract",
            label="Enable Extract",
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
        ConfigField(
            name="max_tokens",
            label="Max Tokens",
            type="number",
            required=False,
            default=6000,
        ),
        ConfigField(
            name="include_answer",
            label="Include Answer",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="search_depth",
            label="Search Depth",
            type="text",
            required=False,
            default="advanced",
        ),
        ConfigField(
            name="extract_depth",
            label="Extract Depth",
            type="text",
            required=False,
            default="basic",
        ),
        ConfigField(
            name="include_images",
            label="Include Images",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="include_favicon",
            label="Include Favicon",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="extract_timeout",
            label="Extract Timeout",
            type="number",
            required=False,
            default=None,
        ),
        ConfigField(
            name="extract_format",
            label="Extract Format",
            type="text",
            required=False,
            default="markdown",
        ),
        ConfigField(
            name="format",
            label="Format",
            type="text",
            required=False,
            default="markdown",
        ),
    ],
    dependencies=["tavily-python"],
    docs_url="https://docs.agno.com/tools/toolkits/search/tavily",
)
def tavily_tools() -> type[TavilyTools]:
    """Return Tavily tools for real-time web search."""
    from agno.tools.tavily import TavilyTools

    return TavilyTools
