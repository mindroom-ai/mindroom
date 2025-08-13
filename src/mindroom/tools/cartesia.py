"""Cartesia tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tools_metadata import ConfigField, SetupType, ToolCategory, ToolStatus, register_tool_with_metadata

if TYPE_CHECKING:
    from agno.tools.cartesia import CartesiaTools


@register_tool_with_metadata(
    name="cartesia",
    display_name="Cartesia",
    description="Voice AI services including text-to-speech and voice localization",
    category=ToolCategory.DEVELOPMENT,  # others/ → DEVELOPMENT according to mapping
    status=ToolStatus.REQUIRES_CONFIG,  # requires API key
    setup_type=SetupType.API_KEY,  # API key authentication
    icon="VolumeX",  # Voice/sound related icon
    icon_color="text-purple-500",  # Purple for voice AI
    config_fields=[
        # Authentication
        ConfigField(
            name="api_key",
            label="API Key",
            type="password",
            required=False,
            placeholder="your_api_key_here",
            description="Cartesia API key for authentication (can also be set via CARTESIA_API_KEY env var)",
        ),
        # Model configuration
        ConfigField(
            name="model_id",
            label="Model ID",
            type="text",
            required=False,
            default="sonic-2",
            placeholder="sonic-2",
            description="The model ID to use for text-to-speech",
        ),
        ConfigField(
            name="default_voice_id",
            label="Default Voice ID",
            type="text",
            required=False,
            default="78ab82d5-25be-4f7d-82b3-7ad64e5b85b2",
            placeholder="78ab82d5-25be-4f7d-82b3-7ad64e5b85b2",
            description="The default voice ID to use for text-to-speech and localization",
        ),
        # Feature flags
        ConfigField(
            name="text_to_speech_enabled",
            label="Text to Speech Enabled",
            type="boolean",
            required=False,
            default=True,
            description="Enable text-to-speech functionality",
        ),
        ConfigField(
            name="list_voices_enabled",
            label="List Voices Enabled",
            type="boolean",
            required=False,
            default=True,
            description="Enable listing available voices functionality",
        ),
        ConfigField(
            name="voice_localize_enabled",
            label="Voice Localize Enabled",
            type="boolean",
            required=False,
            default=False,
            description="Enable voice localization functionality",
        ),
    ],
    dependencies=["cartesia"],
    docs_url="https://docs.agno.com/tools/toolkits/others/cartesia",
)
def cartesia_tools() -> type[CartesiaTools]:
    """Return Cartesia tools for voice AI services."""
    from agno.tools.cartesia import CartesiaTools

    return CartesiaTools
