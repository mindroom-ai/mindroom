"""OpenAI tool configuration."""

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
    from agno.tools.openai import OpenAITools


@register_tool_with_metadata(
    name="openai",
    display_name="OpenAI",
    description="AI-powered tools for transcription, image generation, and speech synthesis",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="SiOpenai",
    icon_color="text-green-600",
    config_fields=[
        # Authentication
        ConfigField(
            name="api_key",
            label="API Key",
            type="password",
            required=False,
            placeholder="sk-...",
            description="OpenAI API key (can also be set via OPENAI_API_KEY env var)",
        ),
        # Feature toggles
        ConfigField(
            name="enable_transcription",
            label="Enable Transcription",
            type="boolean",
            required=False,
            default=True,
            description="Enable audio transcription using Whisper",
        ),
        ConfigField(
            name="enable_image_generation",
            label="Enable Image Generation",
            type="boolean",
            required=False,
            default=True,
            description="Enable image generation using DALL-E",
        ),
        ConfigField(
            name="enable_speech_generation",
            label="Enable Speech Generation",
            type="boolean",
            required=False,
            default=True,
            description="Enable text-to-speech synthesis",
        ),
        # Transcription settings
        ConfigField(
            name="transcription_model",
            label="Transcription Model",
            type="text",
            required=False,
            default="whisper-1",
            placeholder="whisper-1",
            description="Model to use for audio transcription",
        ),
        # Text-to-speech settings
        ConfigField(
            name="text_to_speech_voice",
            label="Text-to-Speech Voice",
            type="text",
            required=False,
            default="alloy",
            placeholder="alloy",
            description="Voice to use for speech generation (alloy, echo, fable, onyx, nova, shimmer)",
        ),
        ConfigField(
            name="text_to_speech_model",
            label="Text-to-Speech Model",
            type="text",
            required=False,
            default="tts-1",
            placeholder="tts-1",
            description="Model to use for text-to-speech (tts-1, tts-1-hd)",
        ),
        ConfigField(
            name="text_to_speech_format",
            label="Text-to-Speech Format",
            type="text",
            required=False,
            default="mp3",
            placeholder="mp3",
            description="Audio format for speech generation (mp3, opus, aac, flac, wav, pcm)",
        ),
        # Image generation settings
        ConfigField(
            name="image_model",
            label="Image Model",
            type="text",
            required=False,
            default="dall-e-3",
            placeholder="dall-e-3",
            description="Model to use for image generation",
        ),
        ConfigField(
            name="image_quality",
            label="Image Quality",
            type="text",
            required=False,
            placeholder="standard",
            description="Quality setting for image generation (standard, hd)",
        ),
        ConfigField(
            name="image_size",
            label="Image Size",
            type="text",
            required=False,
            placeholder="1024x1024",
            description="Size for generated images (256x256, 512x512, 1024x1024, 1792x1024, 1024x1792)",
        ),
        ConfigField(
            name="image_style",
            label="Image Style",
            type="text",
            required=False,
            placeholder="vivid",
            description="Style for generated images (vivid, natural)",
        ),
    ],
    dependencies=["openai"],
    docs_url="https://docs.agno.com/tools/toolkits/models/openai",
)
def openai_tools() -> type[OpenAITools]:
    """Return OpenAI tools for AI-powered transcription, image generation, and speech synthesis."""
    from agno.tools.openai import OpenAITools

    return OpenAITools
