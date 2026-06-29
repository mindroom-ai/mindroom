"""Native Matrix voice-message tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.model_defaults import OPENAI_TTS
from mindroom.tool_system.metadata import ConfigField, SetupType, ToolCategory, ToolStatus, register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.matrix_voice_message import MatrixVoiceMessageTools


@register_tool_with_metadata(
    name="matrix_voice_message",
    display_name="Matrix Voice Message",
    description="Generate speech from text and send it as a Matrix voice message with room/thread context defaults",
    category=ToolCategory.COMMUNICATION,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    requires_room_context=True,
    icon="Mic",
    icon_color="text-green-500",
    config_fields=[
        ConfigField(
            name="api_key",
            label="OpenAI API Key",
            type="password",
            required=False,
            default=None,
        ),
        ConfigField(
            name="model",
            label="TTS Model",
            type="text",
            required=False,
            default=OPENAI_TTS,
        ),
        ConfigField(
            name="voice",
            label="Voice",
            type="text",
            required=False,
            default="alloy",
        ),
    ],
    dependencies=["openai"],
    docs_url="https://github.com/mindroom-ai/mindroom",
    function_names=("matrix_voice_message",),
)
def matrix_voice_message_tools() -> type[MatrixVoiceMessageTools]:
    """Return native Matrix voice-message tools."""
    from mindroom.custom_tools.matrix_voice_message import MatrixVoiceMessageTools

    return MatrixVoiceMessageTools
