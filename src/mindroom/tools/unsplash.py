"""Unsplash tool configuration."""

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
    from mindroom.custom_tools.unsplash import UnsplashTools


@register_tool_with_metadata(
    name="unsplash",
    file_access=ToolFileAccess.NONE,
    display_name="Unsplash",
    description="Search and retrieve high-quality, royalty-free images from Unsplash",
    category=ToolCategory.RESEARCH,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="SiUnsplash",
    icon_color="text-gray-800",
    config_fields=[
        ConfigField(
            name="access_key",
            env=("UNSPLASH_ACCESS_KEY",),
            label="Access Key",
            type="password",
            required=True,
            placeholder="Unsplash API access key",
            description="API access key from Unsplash Developer account",
        ),
        bool_config_field("enable_search_photos", "Enable Search Photos"),
        bool_config_field("enable_get_photo", "Enable Get Photo"),
        bool_config_field("enable_get_random_photo", "Enable Get Random Photo"),
        bool_config_field("enable_download_photo", "Enable Download Photo", default=False),
        bool_config_field("all", "All", default=False),
        ConfigField(
            name="timeout",
            label="Timeout",
            type="number",
            required=False,
            default=30,
        ),
    ],
    dependencies=[],
    docs_url="https://docs.mindroom.chat/tools/media-and-content/#unsplash",
    helper_text="Get a free API key from [Unsplash Developers](https://unsplash.com/developers)",
    function_names=("unsplash",),
    action_scoped_grants=("unsplash",),
)
def unsplash_tools() -> type[UnsplashTools]:
    """Return Unsplash tools for image search."""
    from mindroom.custom_tools.unsplash import UnsplashTools

    return UnsplashTools
