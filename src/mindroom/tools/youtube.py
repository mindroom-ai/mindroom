"""YouTube tool configuration."""

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
    from agno.tools.youtube import YouTubeTools


@register_tool_with_metadata(
    name="youtube",
    display_name="YouTube",
    description="Extract video data, captions, and timestamps from YouTube videos",
    category=ToolCategory.ENTERTAINMENT,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="SiYoutube",
    icon_color="text-red-600",
    config_fields=[
        ConfigField(
            name="enable_get_video_captions",
            label="Enable Get Video Captions",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_get_video_data",
            label="Enable Get Video Data",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_get_video_timestamps",
            label="Enable Get Video Timestamps",
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
        ConfigField(
            name="languages",
            label="Languages",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="proxies",
            label="Proxies",
            type="text",
            required=False,
            default=None,
        ),
    ],
    dependencies=["youtube_transcript_api"],
    docs_url="https://docs.agno.com/tools/toolkits/entertainment/youtube",
)
def youtube_tools() -> type[YouTubeTools]:
    """Return YouTube tools for video data extraction."""
    from agno.tools.youtube import YouTubeTools

    return YouTubeTools
