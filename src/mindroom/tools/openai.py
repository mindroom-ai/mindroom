"""OpenAI tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.metadata import (
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
        ConfigField(
            name="api_key",
            label="API Key",
            type="password",
            required=False,
            default=None,
        ),
        ConfigField(
            name="enable_transcription",
            label="Enable Transcription",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_image_generation",
            label="Enable Image Generation",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_speech_generation",
            label="Enable Speech Generation",
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
        ConfigField(
            name="transcription_model",
            label="Transcription Model",
            type="text",
            required=False,
            default="whisper-1",
        ),
        ConfigField(
            name="text_to_speech_voice",
            label="Text To Speech Voice",
            type="text",
            required=False,
            default="alloy",
        ),
        ConfigField(
            name="text_to_speech_model",
            label="Text To Speech Model",
            type="text",
            required=False,
            default="tts-1",
        ),
        ConfigField(
            name="text_to_speech_format",
            label="Text To Speech Format",
            type="text",
            required=False,
            default="mp3",
        ),
        ConfigField(
            name="image_model",
            label="Image Model",
            type="text",
            required=False,
            default="dall-e-3",
        ),
        ConfigField(
            name="image_quality",
            label="Image Quality",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="image_size",
            label="Image Size",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="image_style",
            label="Image Style",
            type="text",
            required=False,
            default=None,
        ),
    ],
    dependencies=["openai"],
    docs_url="https://docs.agno.com/tools/toolkits/models/openai",
)
def openai_tools() -> type[OpenAITools]:
    """Return OpenAI tools for AI-powered transcription, image generation, and speech synthesis."""
    from agno.tools.openai import OpenAITools

    return OpenAITools
