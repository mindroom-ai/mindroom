"""Giphy tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tools_metadata import ConfigField, SetupType, ToolCategory, ToolStatus, register_tool_with_metadata

if TYPE_CHECKING:
    from agno.tools.giphy import GiphyTools


@register_tool_with_metadata(
    name="giphy",
    display_name="Giphy",
    description="GIF search and integration",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="GiGift",
    icon_color="text-purple-500",
    config_fields=[
        ConfigField(
            name="api_key",
            label="API Key",
            type="password",
            required=False,
            default=None,
        ),
        ConfigField(
            name="limit",
            label="Limit",
            type="number",
            required=False,
            default=1,
        ),
        ConfigField(
            name="enable_search_gifs",
            label="Enable Search Gifs",
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
    dependencies=["httpx"],
    docs_url="https://docs.agno.com/tools/toolkits/others/giphy",
)
def giphy_tools() -> type[GiphyTools]:
    """Return Giphy tools for GIF search and integration."""
    from agno.tools.giphy import GiphyTools

    return GiphyTools
