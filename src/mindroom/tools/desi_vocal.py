"""DesiVocal tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tools_metadata import ConfigField, SetupType, ToolCategory, ToolStatus, register_tool_with_metadata

if TYPE_CHECKING:
    from agno.tools.desi_vocal import DesiVocalTools


@register_tool_with_metadata(
    name="desi_vocal",
    display_name="DesiVocal",
    description="Hindi and Indian language text-to-speech synthesis",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="FaMicrophone",
    icon_color="text-orange-600",
    config_fields=[
        ConfigField(
            name="api_key",
            label="API Key",
            type="password",
            required=True,
            description="DesiVocal API key (falls back to DESI_VOCAL_API_KEY env var)",
        ),
        ConfigField(
            name="voice_id",
            label="Voice ID",
            type="text",
            required=False,
            default="f27d74e5-ea71-4697-be3e-f04bbd80c1a8",
            description="Default voice ID to use for speech synthesis",
        ),
        ConfigField(
            name="enable_get_voices",
            label="Enable Get Voices",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_text_to_speech",
            label="Enable Text to Speech",
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
    dependencies=["requests"],
    docs_url="https://docs.agno.com/tools/toolkits/others/desi_vocal",
    helper_text="Get an API key from [DesiVocal](https://desivocal.com/)",
)
def desi_vocal_tools() -> type[DesiVocalTools]:
    """Return DesiVocal tools for text-to-speech."""
    from agno.tools.desi_vocal import DesiVocalTools

    return DesiVocalTools
