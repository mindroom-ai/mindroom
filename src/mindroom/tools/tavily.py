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
        # Authentication
        ConfigField(
            name="api_key",
            label="API Key",
            type="password",
            required=False,
            placeholder="tvly-...",
            description="Tavily API key for web search access (can also be set via TAVILY_API_KEY env var)",
        ),
        # Feature flags
        ConfigField(
            name="search",
            label="Enable Search",
            type="boolean",
            required=False,
            default=True,
            description="Enable web search functionality",
        ),
        ConfigField(
            name="use_search_context",
            label="Use Search Context",
            type="boolean",
            required=False,
            default=False,
            description="Use search context method instead of regular search",
        ),
        # Search configuration
        ConfigField(
            name="max_tokens",
            label="Max Tokens",
            type="number",
            required=False,
            default=6000,
            description="Maximum number of tokens to return in search results",
        ),
        ConfigField(
            name="include_answer",
            label="Include Answer",
            type="boolean",
            required=False,
            default=True,
            description="Include AI-generated answer summary in search results",
        ),
        ConfigField(
            name="search_depth",
            label="Search Depth",
            type="select",
            required=False,
            default="advanced",
            options=[
                {"label": "Basic", "value": "basic"},
                {"label": "Advanced", "value": "advanced"},
            ],
            description="Search depth level for query processing",
        ),
        ConfigField(
            name="format",
            label="Output Format",
            type="select",
            required=False,
            default="markdown",
            options=[
                {"label": "JSON", "value": "json"},
                {"label": "Markdown", "value": "markdown"},
            ],
            description="Format for search result output",
        ),
    ],
    dependencies=["tavily-python"],
    docs_url="https://docs.agno.com/tools/toolkits/search/tavily",
)
def tavily_tools() -> type[TavilyTools]:
    """Return Tavily tools for real-time web search."""
    from agno.tools.tavily import TavilyTools

    return TavilyTools
