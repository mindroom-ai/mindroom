"""Eleven Labs tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tools_metadata import ConfigField, SetupType, ToolCategory, ToolStatus, register_tool_with_metadata

if TYPE_CHECKING:
    from agno.tools.eleven_labs import ElevenLabsTools


@register_tool_with_metadata(
    name="eleven_labs",
    display_name="Eleven Labs",
    description="Text-to-speech and sound effect generation using AI voices",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="FaVolumeUp",
    icon_color="text-orange-500",
    config_fields=[
        # Authentication
        ConfigField(
            name="api_key",
            label="API Key",
            type="password",
            required=False,
            placeholder="sk-...",
            description="Eleven Labs API key for authentication (can also be set via ELEVEN_LABS_API_KEY env var)",
        ),
        # Voice Configuration
        ConfigField(
            name="voice_id",
            label="Voice ID",
            type="text",
            required=False,
            default="JBFqnCBsd6RMkjVDRZzb",
            placeholder="JBFqnCBsd6RMkjVDRZzb",
            description="The voice ID to use for audio generation",
        ),
        ConfigField(
            name="model_id",
            label="Model ID",
            type="text",
            required=False,
            default="eleven_multilingual_v2",
            placeholder="eleven_multilingual_v2",
            description="The model's ID to use for audio generation",
        ),
        # Output Configuration
        ConfigField(
            name="output_format",
            label="Output Format",
            type="text",
            required=False,
            default="mp3_44100_64",
            placeholder="mp3_44100_64",
            description="The output format for audio generation (e.g., mp3_44100_64, pcm_44100)",
        ),
        ConfigField(
            name="target_directory",
            label="Target Directory",
            type="text",
            required=False,
            placeholder="audio_generations",
            description="Directory to save generated audio files (optional)",
        ),
    ],
    dependencies=["elevenlabs"],
    docs_url="https://docs.agno.com/tools/toolkits/others/eleven_labs",
)
def eleven_labs_tools() -> type[ElevenLabsTools]:
    """Return Eleven Labs tools for text-to-speech and sound effect generation."""
    from agno.tools.eleven_labs import ElevenLabsTools

    return ElevenLabsTools
