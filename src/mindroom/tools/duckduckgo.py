"""DuckDuckGo tool configuration."""

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
    from agno.tools.duckduckgo import DuckDuckGoTools


@register_tool_with_metadata(
    name="duckduckgo",
    display_name="DuckDuckGo",
    description="Search engine for web search and news",
    category=ToolCategory.RESEARCH,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="FaSearch",
    icon_color="text-orange-500",  # DuckDuckGo orange
    config_fields=[
        # Search features
        ConfigField(
            name="search",
            label="Search",
            type="boolean",
            required=False,
            default=True,
            description="Enable DuckDuckGo search function",
        ),
        ConfigField(
            name="news",
            label="News",
            type="boolean",
            required=False,
            default=True,
            description="Enable DuckDuckGo news function",
        ),
        # Search configuration
        ConfigField(
            name="modifier",
            label="Search Modifier",
            type="text",
            required=False,
            default=None,
            placeholder="site:example.com",
            description="A modifier to be used in the search request",
        ),
        ConfigField(
            name="fixed_max_results",
            label="Fixed Max Results",
            type="number",
            required=False,
            default=None,
            placeholder="10",
            description="A fixed number of maximum results",
        ),
        ConfigField(
            name="proxy",
            label="Proxy",
            type="url",
            required=False,
            default=None,
            placeholder="http://proxy.example.com:8080",
            description="Proxy to be used in the search request",
        ),
        ConfigField(
            name="timeout",
            label="Timeout",
            type="number",
            required=False,
            default=10,
            placeholder="10",
            description="The maximum number of seconds to wait for a response",
        ),
        ConfigField(
            name="verify_ssl",
            label="Verify SSL",
            type="boolean",
            required=False,
            default=True,
            description="Verify SSL certificates for secure connections",
        ),
    ],
    dependencies=["ddgs"],
    docs_url="https://docs.agno.com/tools/toolkits/search/duckduckgo",
)
def duckduckgo_tools() -> type[DuckDuckGoTools]:
    """Return DuckDuckGo tools for web search and news."""
    from agno.tools.duckduckgo import DuckDuckGoTools

    return DuckDuckGoTools
