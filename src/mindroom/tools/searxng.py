"""Searxng tool configuration."""

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
    from agno.tools.searxng import Searxng


@register_tool_with_metadata(
    name="searxng",
    display_name="SearxNG",
    description="Open source search engine for web, images, news, science, and specialized content",
    category=ToolCategory.RESEARCH,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.NONE,
    icon="FaSearch",
    icon_color="text-blue-600",
    config_fields=[
        # Connection configuration
        ConfigField(
            name="host",
            label="Host URL",
            type="url",
            required=True,
            placeholder="http://localhost:8080",
            description="The host for the SearxNG instance connection",
        ),
        ConfigField(
            name="engines",
            label="Search Engines",
            type="text",
            required=False,
            default="",
            placeholder="google,bing,duckduckgo",
            description="Comma-separated list of search engines to use (leave empty for default)",
        ),
        ConfigField(
            name="fixed_max_results",
            label="Fixed Max Results",
            type="number",
            required=False,
            placeholder="10",
            description="Optional parameter to specify the fixed maximum number of results",
        ),
        # Content type enablement flags
        ConfigField(
            name="images",
            label="Enable Image Search",
            type="boolean",
            required=False,
            default=False,
            description="Enable searching for images",
        ),
        ConfigField(
            name="it",
            label="Enable IT Search",
            type="boolean",
            required=False,
            default=False,
            description="Enable searching for IT-related content",
        ),
        ConfigField(
            name="map",
            label="Enable Map Search",
            type="boolean",
            required=False,
            default=False,
            description="Enable searching for maps",
        ),
        ConfigField(
            name="music",
            label="Enable Music Search",
            type="boolean",
            required=False,
            default=False,
            description="Enable searching for music",
        ),
        ConfigField(
            name="news",
            label="Enable News Search",
            type="boolean",
            required=False,
            default=False,
            description="Enable searching for news",
        ),
        ConfigField(
            name="science",
            label="Enable Science Search",
            type="boolean",
            required=False,
            default=False,
            description="Enable searching for science-related content",
        ),
        ConfigField(
            name="videos",
            label="Enable Video Search",
            type="boolean",
            required=False,
            default=False,
            description="Enable searching for videos",
        ),
    ],
    dependencies=[],  # httpx already included in main dependencies
    docs_url="https://docs.agno.com/tools/toolkits/search/searxng",
)
def searxng_tools() -> type[Searxng]:
    """Return SearxNG search tools for web, images, news, and specialized content search."""
    from agno.tools.searxng import Searxng

    return Searxng
