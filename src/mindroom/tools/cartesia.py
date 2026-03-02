"""Cartesia tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.metadata import ConfigField, SetupType, ToolCategory, ToolStatus, register_tool_with_metadata

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
        ConfigField(
            name="api_key",
            label="API Key",
            type="password",
            required=False,
            default=None,
        ),
        ConfigField(
            name="model_id",
            label="Model ID",
            type="text",
            required=False,
            default="sonic-2",
        ),
        ConfigField(
            name="default_voice_id",
            label="Default Voice ID",
            type="text",
            required=False,
            default="78ab82d5-25be-4f7d-82b3-7ad64e5b85b2",
        ),
        ConfigField(
            name="enable_text_to_speech",
            label="Enable Text To Speech",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_list_voices",
            label="Enable List Voices",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_localize_voice",
            label="Enable Localize Voice",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="all",
            label="All",
            type="boolean",
            required=False,
            default=False,
        ),
    ],
    dependencies=["cartesia"],
    docs_url="https://docs.agno.com/tools/toolkits/others/cartesia",
)
def cartesia_tools() -> type[CartesiaTools]:
    """Return Cartesia tools for voice AI services."""
    from agno.tools.cartesia import CartesiaTools

    return CartesiaTools
