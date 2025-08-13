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
        # Video processing features
        ConfigField(
            name="process_video",
            label="Process Video",
            type="boolean",
            required=False,
            default=True,
            description="Enable the extract_audio tool for extracting audio tracks from video files",
        ),
        ConfigField(
            name="generate_captions",
            label="Generate Captions",
            type="boolean",
            required=False,
            default=True,
            description="Enable the create_srt tool for saving transcriptions to SRT formatted files",
        ),
        ConfigField(
            name="embed_captions",
            label="Embed Captions",
            type="boolean",
            required=False,
            default=True,
            description="Enable the embed_captions tool for creating videos with word-level highlighted captions",
        ),
    ],
    dependencies=["moviepy"],  # From agno requirements
    docs_url="https://docs.agno.com/tools/toolkits/others/moviepy",  # URL from llms.txt but WITHOUT .md extension
)
def moviepy_video_tools() -> type[MoviePyVideoTools]:
    """Return MoviePy Video Tools for video processing, audio extraction, and caption generation."""
    from agno.tools.moviepy_video import MoviePyVideoTools

    return MoviePyVideoTools
