"""YouTube tool configuration."""

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
    from mindroom.custom_tools.youtube import YouTubeTools


@register_tool_with_metadata(
    name="youtube",
    file_access=ToolFileAccess.NONE,
    display_name="YouTube",
    description="Extract video data, captions, and timestamps from YouTube videos",
    category=ToolCategory.ENTERTAINMENT,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="SiYoutube",
    icon_color="text-red-600",
    config_fields=[
        bool_config_field("enable_get_video_captions", "Enable Get Video Captions"),
        bool_config_field("enable_get_video_data", "Enable Get Video Data"),
        bool_config_field("enable_get_video_timestamps", "Enable Get Video Timestamps"),
        bool_config_field("all", "All", default=False),
        ConfigField(
            name="languages",
            label="Languages",
            type="string[]",
            required=False,
            default=None,
        ),
        ConfigField(
            name="timeout",
            label="Timeout",
            type="number",
            required=False,
            default=30,
        ),
    ],
    dependencies=["youtube_transcript_api"],
    docs_url="https://docs.mindroom.chat/tools/media-and-content/#youtube",
    function_names=("youtube_video",),
    action_scoped_grants=("youtube_video",),
)
def youtube_tools() -> type[YouTubeTools]:
    """Return YouTube tools for video data extraction."""
    from mindroom.custom_tools.youtube import YouTubeTools

    return YouTubeTools
