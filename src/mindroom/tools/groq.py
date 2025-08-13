"""Groq tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tools_metadata import ConfigField, SetupType, ToolCategory, ToolStatus, register_tool_with_metadata

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
        # Authentication
        ConfigField(
            name="api_key",
            label="API Key",
            type="password",
            required=False,
            placeholder="gsk_...",
            description="Groq API key for authentication (can also be set via GROQ_API_KEY env var)",
        ),
        # Model Configuration
        ConfigField(
            name="transcription_model",
            label="Transcription Model",
            type="text",
            required=False,
            default="whisper-large-v3",
            placeholder="whisper-large-v3",
            description="Whisper model to use for audio transcription",
        ),
        ConfigField(
            name="translation_model",
            label="Translation Model",
            type="text",
            required=False,
            default="whisper-large-v3",
            placeholder="whisper-large-v3",
            description="Whisper model to use for audio translation to English",
        ),
        ConfigField(
            name="tts_model",
            label="Text-to-Speech Model",
            type="text",
            required=False,
            default="playai-tts",
            placeholder="playai-tts",
            description="Model to use for text-to-speech generation",
        ),
        ConfigField(
            name="tts_voice",
            label="TTS Voice",
            type="text",
            required=False,
            default="Chip-PlayAI",
            placeholder="Chip-PlayAI",
            description="Voice to use for text-to-speech generation",
        ),
    ],
    dependencies=["groq"],
    docs_url="https://docs.agno.com/tools/toolkits/models/groq",
)
def groq_tools() -> type[GroqTools]:
    """Return Groq AI tools for fast audio transcription, translation, and text-to-speech."""
    from agno.tools.models.groq import GroqTools

    return GroqTools
