"""Groq tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.metadata import ConfigField, SetupType, ToolCategory, ToolStatus, register_tool_with_metadata

if TYPE_CHECKING:
    from agno.tools.models.groq import GroqTools


@register_tool_with_metadata(
    name="groq",
    display_name="Groq",
    description="Fast AI inference for audio transcription, translation, and text-to-speech",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="TbBrain",
    icon_color="text-orange-500",  # Groq brand orange
    config_fields=[
        ConfigField(
            name="api_key",
            label="API Key",
            type="password",
            required=False,
            default=None,
        ),
        ConfigField(
            name="transcription_model",
            label="Transcription Model",
            type="text",
            required=False,
            default="whisper-large-v3",
        ),
        ConfigField(
            name="translation_model",
            label="Translation Model",
            type="text",
            required=False,
            default="whisper-large-v3",
        ),
        ConfigField(
            name="tts_model",
            label="Tts Model",
            type="text",
            required=False,
            default="playai-tts",
        ),
        ConfigField(
            name="tts_voice",
            label="Tts Voice",
            type="text",
            required=False,
            default="Chip-PlayAI",
        ),
        ConfigField(
            name="enable_transcribe_audio",
            label="Enable Transcribe Audio",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_translate_audio",
            label="Enable Translate Audio",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_generate_speech",
            label="Enable Generate Speech",
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
    dependencies=["groq"],
    docs_url="https://docs.agno.com/tools/toolkits/models/groq",
)
def groq_tools() -> type[GroqTools]:
    """Return Groq AI tools for fast audio transcription, translation, and text-to-speech."""
    from agno.tools.models.groq import GroqTools

    return GroqTools
