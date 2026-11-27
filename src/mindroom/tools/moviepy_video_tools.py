"""MoviePy Video Tools configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import (
    SetupType,
    ToolCategory,
    ToolFileAccess,
    ToolManagedInitArg,
    ToolStatus,
    bool_config_field,
)
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.moviepy_video import MoviePyVideoTools


@register_tool_with_metadata(
    name="moviepy_video_tools",
    file_access=ToolFileAccess.AGENT,
    display_name="MoviePy Video Tools",
    description="Process videos, extract audio, generate SRT caption files, and embed rich word-highlighted captions",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="FaVideo",
    icon_color="text-purple-600",
    config_fields=[
        bool_config_field("enable_process_video", "Enable Process Video"),
        bool_config_field("enable_generate_captions", "Enable Generate Captions"),
        bool_config_field("enable_embed_captions", "Enable Embed Captions"),
        bool_config_field("all", "All", default=False),
    ],
    managed_init_args=(ToolManagedInitArg.TOOL_OUTPUT_WORKSPACE_ROOT, ToolManagedInitArg.FILE_ACCESS),
    dependencies=["moviepy"],
    docs_url="https://docs.mindroom.chat/tools/media-and-content/#moviepy_video_tools",
    function_names=(
        "create_srt",
        "embed_captions",
        "extract_audio",
    ),
)
def moviepy_video_tools() -> type[MoviePyVideoTools]:
    """Return MoviePy Video Tools whose media paths follow the agent's file_access."""
    from mindroom.custom_tools.moviepy_video import MoviePyVideoTools

    return MoviePyVideoTools
