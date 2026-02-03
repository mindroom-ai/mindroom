"""MoviePy Video Tools configuration."""

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
    from agno.tools.moviepy_video import MoviePyVideoTools


@register_tool_with_metadata(
    name="moviepy_video_tools",
    display_name="MoviePy Video Tools",
    description="Process videos, extract audio, generate SRT caption files, and embed rich word-highlighted captions",
    category=ToolCategory.DEVELOPMENT,  # Derived from docs URL (/others/)
    status=ToolStatus.AVAILABLE,  # No authentication required
    setup_type=SetupType.NONE,  # No authentication needed
    icon="FaVideo",  # React icon name for video
    icon_color="text-purple-600",  # Purple color for video processing
    config_fields=[
        ConfigField(
            name="enable_process_video",
            label="Enable Process Video",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_generate_captions",
            label="Enable Generate Captions",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_embed_captions",
            label="Enable Embed Captions",
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
    dependencies=["moviepy"],  # From agno requirements
    docs_url="https://docs.agno.com/tools/toolkits/others/moviepy",  # URL from llms.txt but WITHOUT .md extension
)
def moviepy_video_tools() -> type[MoviePyVideoTools]:
    """Return MoviePy Video Tools for video processing, audio extraction, and caption generation."""
    from agno.tools.moviepy_video import MoviePyVideoTools

    return MoviePyVideoTools
