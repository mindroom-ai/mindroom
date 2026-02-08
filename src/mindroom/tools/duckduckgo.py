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
    icon="SiDuckduckgo",
    icon_color="text-orange-500",  # DuckDuckGo orange
    config_fields=[
        ConfigField(
            name="enable_search",
            label="Enable Search",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_news",
            label="Enable News",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="modifier",
            label="Modifier",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="fixed_max_results",
            label="Fixed Max Results",
            type="number",
            required=False,
            default=None,
        ),
        ConfigField(
            name="proxy",
            label="Proxy",
            type="url",
            required=False,
            default=None,
        ),
        ConfigField(
            name="timeout",
            label="Timeout",
            type="number",
            required=False,
            default=10,
        ),
        ConfigField(
            name="verify_ssl",
            label="Verify Ssl",
            type="boolean",
            required=False,
            default=True,
        ),
    ],
    dependencies=["ddgs"],
    docs_url="https://docs.agno.com/tools/toolkits/search/duckduckgo",
)
def duckduckgo_tools() -> type[DuckDuckGoTools]:
    """Return DuckDuckGo tools for web search and news."""
    from agno.tools.duckduckgo import DuckDuckGoTools

    return DuckDuckGoTools
