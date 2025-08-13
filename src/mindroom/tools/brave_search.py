"""Brave Search tool configuration."""

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
    from agno.tools.bravesearch import BraveSearchTools


@register_tool_with_metadata(
    name="brave_search",
    display_name="Brave Search",
    description="Privacy-focused web search using the Brave search engine",
    category=ToolCategory.RESEARCH,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="FaSearch",
    icon_color="text-orange-500",
    config_fields=[
        # Authentication
        ConfigField(
            name="api_key",
            label="API Key",
            type="password",
            required=False,
            placeholder="BSA...",
            description="Brave Search API key (can also be set via BRAVE_API_KEY env var)",
        ),
        # Search configuration
        ConfigField(
            name="fixed_max_results",
            label="Fixed Max Results",
            type="number",
            required=False,
            default=None,
            placeholder="10",
            description="Optional fixed maximum number of results to return for all searches",
        ),
        ConfigField(
            name="fixed_language",
            label="Fixed Language",
            type="text",
            required=False,
            default=None,
            placeholder="en",
            description="Optional fixed language for all search results (e.g., 'en', 'es', 'fr')",
        ),
    ],
    dependencies=["brave-search"],
    docs_url="https://docs.agno.com/tools/toolkits/search/bravesearch",
)
def brave_search_tools() -> type[BraveSearchTools]:
    """Return Brave Search tools for privacy-focused web search."""
    from agno.tools.bravesearch import BraveSearchTools

    return BraveSearchTools
