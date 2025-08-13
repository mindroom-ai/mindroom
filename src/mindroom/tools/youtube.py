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
    icon="FaYoutube",
    icon_color="text-red-600",
    config_fields=[
        # Feature toggles - main functionality
        ConfigField(
            name="get_video_captions",
            label="Get Video Captions",
            type="boolean",
            required=False,
            default=True,
            description="Enable extracting captions/transcripts from YouTube videos",
        ),
        ConfigField(
            name="get_video_data",
            label="Get Video Data",
            type="boolean",
            required=False,
            default=True,
            description="Enable extracting video metadata (title, author, thumbnail, etc.)",
        ),
        ConfigField(
            name="get_video_timestamps",
            label="Get Video Timestamps",
            type="boolean",
            required=False,
            default=True,
            description="Enable generating timestamped transcripts from video captions",
        ),
        # Configuration options
        ConfigField(
            name="languages",
            label="Languages",
            type="text",
            required=False,
            default=None,
            placeholder="en,es,fr",
            description="Comma-separated list of preferred language codes for captions (e.g., 'en,es,fr'). If not specified, defaults to English.",
        ),
        ConfigField(
            name="proxies",
            label="Proxies",
            type="text",
            required=False,
            default=None,
            placeholder='{"http": "http://proxy:8080"}',
            description="JSON string of proxy configuration for network requests (e.g., for bypassing geo-restrictions)",
        ),
    ],
    dependencies=["youtube_transcript_api"],
    docs_url="https://docs.agno.com/tools/toolkits/entertainment/youtube",
)
def youtube_tools() -> type[YouTubeTools]:
    """Return YouTube tools for video data extraction."""
    from agno.tools.youtube import YouTubeTools

    return YouTubeTools
