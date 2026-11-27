"""Giphy tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolFileAccess,
    ToolStatus,
    bool_config_field,
)
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.giphy import GiphyTools


@register_tool_with_metadata(
    name="giphy",
    file_access=ToolFileAccess.NONE,
    display_name="Giphy",
    description="GIF search and integration",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="SiGiphy",
    icon_color="text-purple-500",
    config_fields=[
        ConfigField(
            name="api_key",
            env=("GIPHY_API_KEY",),
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
        bool_config_field("enable_search_gifs", "Enable Search Gifs"),
        bool_config_field("all", "All", default=False),
    ],
    dependencies=["httpx"],
    docs_url="https://docs.mindroom.chat/tools/media-and-content/#giphy",
    function_names=("search_gifs",),
)
def giphy_tools() -> type[GiphyTools]:
    """Return Giphy tools for GIF search and integration."""
    from mindroom.custom_tools.giphy import GiphyTools

    return GiphyTools
