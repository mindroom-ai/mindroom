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
        ConfigField(
            name="api_key",
            label="API Key",
            type="password",
            required=False,
            default=None,
        ),
        ConfigField(
            name="depth",
            label="Depth",
            type="text",
            required=False,
            default="standard",
        ),
        ConfigField(
            name="output_type",
            label="Output Type",
            type="text",
            required=False,
            default="searchResults",
        ),
        ConfigField(
            name="enable_web_search_with_linkup",
            label="Enable Web Search With Linkup",
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
    dependencies=["linkup-sdk"],
    docs_url="https://docs.agno.com/tools/toolkits/search/linkup",
)
def linkup_tools() -> type[LinkupTools]:
    """Return Linkup tools for web search."""
    from agno.tools.linkup import LinkupTools

    return LinkupTools
