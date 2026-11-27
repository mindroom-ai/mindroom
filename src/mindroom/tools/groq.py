"""Groq tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.model_defaults import GROQ_TRANSCRIPTION, GROQ_TTS
from mindroom.tool_system.declarations import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolFileAccess,
    ToolManagedInitArg,
    ToolStatus,
    bool_config_field,
)
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.groq_media import GroqTools


@register_tool_with_metadata(
    name="groq",
    file_access=ToolFileAccess.AGENT,
    display_name="Groq",
    description="Fast AI inference for audio transcription, translation, and text-to-speech",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="TbBrain",
    icon_color="text-orange-500",
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
            default=GROQ_TRANSCRIPTION,
        ),
        ConfigField(
            name="translation_model",
            label="Translation Model",
            type="text",
            required=False,
            default=GROQ_TRANSCRIPTION,
        ),
        ConfigField(
            name="tts_model",
            label="Tts Model",
            type="text",
            required=False,
            default=GROQ_TTS,
        ),
        ConfigField(
            name="tts_voice",
            label="Tts Voice",
            type="text",
            required=False,
            default="troy",
        ),
        bool_config_field("enable_transcribe_audio", "Enable Transcribe Audio"),
        bool_config_field("enable_translate_audio", "Enable Translate Audio"),
        bool_config_field("enable_generate_speech", "Enable Generate Speech"),
        bool_config_field("all", "All", default=False),
    ],
    managed_init_args=(ToolManagedInitArg.TOOL_OUTPUT_WORKSPACE_ROOT, ToolManagedInitArg.FILE_ACCESS),
    dependencies=["groq"],
    docs_url="https://docs.mindroom.chat/tools/ai-and-generation/#groq",
    function_names=("generate_speech", "transcribe_audio", "translate_audio"),
)
def groq_tools() -> type[GroqTools]:
    """Return Groq audio tools whose local audio sources follow the agent's file_access."""
    from mindroom.custom_tools.groq_media import GroqTools

    return GroqTools
