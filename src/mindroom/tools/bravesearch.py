"""Brave Search tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tools_metadata import ConfigField, SetupType, ToolCategory, ToolStatus, register_tool_with_metadata

if TYPE_CHECKING:
    from agno.tools.bravesearch import BraveSearchTools


@register_tool_with_metadata(
    name="bravesearch",
    display_name="Brave Search",
    description="Search the web using Brave Search API",
    category=ToolCategory.RESEARCH,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="SiBrave",
    icon_color="text-orange-500",
    config_fields=[
        ConfigField(
            name="api_key",
            label="API Key",
            type="password",
            required=True,
            placeholder="Brave Search API key",
            description="API key from Brave Search",
        ),
        ConfigField(
            name="fixed_max_results",
            label="Fixed Max Results",
            type="number",
            required=False,
            default=None,
        ),
        ConfigField(
            name="fixed_language",
            label="Fixed Language",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="all",
            label="All",
            type="boolean",
            required=False,
            default=False,
        ),
    ],
    dependencies=["brave-search"],
    docs_url="https://docs.agno.com/tools/toolkits/search/bravesearch",
    helper_text="Get a free API key from [Brave Search API](https://brave.com/search/api/)",
)
def bravesearch_tools() -> type[BraveSearchTools]:
    """Return Brave Search tools for web search."""
    from agno.tools.bravesearch import BraveSearchTools

    return BraveSearchTools
