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
        # Authentication
        ConfigField(
            name="api_key",
            label="API Key",
            type="password",
            required=False,
            placeholder="dc6zaTOxFJmzC",
            description="Giphy API key (can also be set via GIPHY_API_KEY env var)",
        ),
        # Search configuration
        ConfigField(
            name="limit",
            label="GIF Limit",
            type="number",
            required=False,
            default=1,
            description="Number of GIFs to return in search results",
        ),
    ],
    dependencies=["httpx"],
    docs_url="https://docs.agno.com/tools/toolkits/others/giphy",
)
def giphy_tools() -> type[GiphyTools]:
    """Return Giphy tools for GIF search and integration."""
    from agno.tools.giphy import GiphyTools

    return GiphyTools
