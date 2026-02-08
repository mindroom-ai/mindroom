"""Unsplash tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tools_metadata import ConfigField, SetupType, ToolCategory, ToolStatus, register_tool_with_metadata

if TYPE_CHECKING:
    from agno.tools.unsplash import UnsplashTools


@register_tool_with_metadata(
    name="unsplash",
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
            label="Access Key",
            type="password",
            required=True,
            placeholder="Unsplash API access key",
            description="API access key from Unsplash Developer account",
        ),
        ConfigField(
            name="enable_search_photos",
            label="Enable Search Photos",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_get_photo",
            label="Enable Get Photo",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_get_random_photo",
            label="Enable Get Random Photo",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_download_photo",
            label="Enable Download Photo",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="all",
            label="All",
            type="boolean",
            required=False,
            default=False,
        ),
    ],
    dependencies=[],
    docs_url="https://docs.agno.com/tools/toolkits/others/unsplash",
    helper_text="Get a free API key from [Unsplash Developers](https://unsplash.com/developers)",
)
def unsplash_tools() -> type[UnsplashTools]:
    """Return Unsplash tools for image search."""
    from agno.tools.unsplash import UnsplashTools

    return UnsplashTools
