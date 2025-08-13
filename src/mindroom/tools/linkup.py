"""Linkup tool configuration."""

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
    from agno.tools.linkup import LinkupTools


@register_tool_with_metadata(
    name="linkup",
    display_name="Linkup",
    description="Web search using Linkup API for real-time information",
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
            placeholder="linkup_...",
            description="Linkup API key for authentication (can also be set via LINKUP_API_KEY env var)",
        ),
        # Search configuration
        ConfigField(
            name="depth",
            label="Search Depth",
            type="text",
            required=False,
            default="standard",
            placeholder="standard",
            description="Depth of the search. Use 'standard' for fast and affordable web search or 'deep' for comprehensive, in-depth web search",
        ),
        ConfigField(
            name="output_type",
            label="Output Type",
            type="text",
            required=False,
            default="searchResults",
            placeholder="searchResults",
            description="Type of output. 'sourcedAnswer' provides a comprehensive natural language answer with citations, 'searchResults' returns raw search context data",
        ),
    ],
    dependencies=["linkup-sdk"],
    docs_url="https://docs.agno.com/tools/toolkits/search/linkup",
)
def linkup_tools() -> type[LinkupTools]:
    """Return Linkup tools for web search."""
    from agno.tools.linkup import LinkupTools

    return LinkupTools
