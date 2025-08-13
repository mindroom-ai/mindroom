"""ModelsLabs tool configuration."""

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
    from agno.tools.models_labs import ModelsLabTools


@register_tool_with_metadata(
    name="modelslabs",
    display_name="ModelsLabs",
    description="AI model marketplace for generating videos, audio, and GIFs from text prompts",
    category=ToolCategory.DEVELOPMENT,  # From docs URL: /tools/toolkits/others/
    status=ToolStatus.REQUIRES_CONFIG,  # Requires API key
    setup_type=SetupType.API_KEY,  # Uses api_key parameter
    icon="Video",  # React icon for video generation
    icon_color="text-purple-600",  # Purple color for creative/AI tools
    config_fields=[
        # Authentication parameter
        ConfigField(
            name="api_key",
            label="API Key",
            type="password",
            required=False,
            default=None,
            placeholder="your-modelslabs-api-key",
            description="The ModelsLab API key for authentication (can also be set via MODELS_LAB_API_KEY env var)",
        ),
        # Media generation parameters
        ConfigField(
            name="file_type",
            label="File Type",
            type="text",
            required=False,
            # The agno default is FileType.MP4, which has value "mp4"
            default="mp4",
            placeholder="mp4",
            description="The type of file to generate (mp4, gif, or mp3)",
        ),
        # Timing and completion parameters
        ConfigField(
            name="wait_for_completion",
            label="Wait for Completion",
            type="boolean",
            required=False,
            default=False,
            description="Whether to wait for the media generation to complete before returning",
        ),
        ConfigField(
            name="add_to_eta",
            label="Add to ETA",
            type="number",
            required=False,
            default=15,
            placeholder="15",
            description="Time in seconds to add to the ETA to account for the time it takes to fetch the media",
        ),
        ConfigField(
            name="max_wait_time",
            label="Max Wait Time",
            type="number",
            required=False,
            default=60,
            placeholder="60",
            description="Maximum time in seconds to wait for the media to be ready",
        ),
    ],
    dependencies=["requests"],  # Already in pyproject.toml
    docs_url="https://docs.agno.com/tools/toolkits/others/models_labs",
)
def modelslabs_tools() -> type[ModelsLabTools]:
    """Return ModelsLabs tool for AI-powered media generation."""
    from agno.tools.models_labs import ModelsLabTools

    return ModelsLabTools
