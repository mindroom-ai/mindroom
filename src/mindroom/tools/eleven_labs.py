"""Eleven Labs tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.model_defaults import ELEVENLABS_TTS
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
    from mindroom.custom_tools.eleven_labs import ElevenLabsTools


@register_tool_with_metadata(
    name="eleven_labs",
    file_access=ToolFileAccess.NONE,
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
            env=("ELEVEN_LABS_API_KEY",),
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
            default=ELEVENLABS_TTS,
        ),
        ConfigField(
            name="output_format",
            label="Output Format",
            type="text",
            required=False,
            default="mp3_44100_64",
        ),
        bool_config_field("enable_get_voices", "Enable Get Voices"),
        bool_config_field("enable_generate_sound_effect", "Enable Generate Sound Effect"),
        bool_config_field("enable_text_to_speech", "Enable Text To Speech"),
        bool_config_field("all", "All", default=False),
    ],
    dependencies=["elevenlabs"],
    docs_url="https://docs.mindroom.chat/tools/ai-and-generation/#eleven_labs",
    function_names=("eleven_labs_generate_sound_effect", "eleven_labs_get_voices", "eleven_labs_text_to_speech"),
)
def eleven_labs_tools() -> type[ElevenLabsTools]:
    """Return Eleven Labs tools for text-to-speech and sound effect generation."""
    from mindroom.custom_tools.eleven_labs import ElevenLabsTools

    return ElevenLabsTools
