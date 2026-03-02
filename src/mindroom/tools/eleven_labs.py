"""Eleven Labs tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.metadata import ConfigField, SetupType, ToolCategory, ToolStatus, register_tool_with_metadata

if TYPE_CHECKING:
    from agno.tools.eleven_labs import ElevenLabsTools


@register_tool_with_metadata(
    name="eleven_labs",
    display_name="Eleven Labs",
    description="Text-to-speech and sound effect generation using AI voices",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="SiElevenlabs",
    icon_color="text-orange-500",
    config_fields=[
        ConfigField(
            name="voice_id",
            label="Voice ID",
            type="text",
            required=False,
            default="JBFqnCBsd6RMkjVDRZzb",
        ),
        ConfigField(
            name="api_key",
            label="API Key",
            type="password",
            required=False,
            default=None,
        ),
        ConfigField(
            name="target_directory",
            label="Target Directory",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="model_id",
            label="Model ID",
            type="text",
            required=False,
            default="eleven_multilingual_v2",
        ),
        ConfigField(
            name="output_format",
            label="Output Format",
            type="text",
            required=False,
            default="mp3_44100_64",
        ),
        ConfigField(
            name="enable_get_voices",
            label="Enable Get Voices",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_generate_sound_effect",
            label="Enable Generate Sound Effect",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_text_to_speech",
            label="Enable Text To Speech",
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
    dependencies=["elevenlabs"],
    docs_url="https://docs.agno.com/tools/toolkits/others/eleven_labs",
)
def eleven_labs_tools() -> type[ElevenLabsTools]:
    """Return Eleven Labs tools for text-to-speech and sound effect generation."""
    from agno.tools.eleven_labs import ElevenLabsTools

    return ElevenLabsTools
