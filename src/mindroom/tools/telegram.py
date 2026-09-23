"""Telegram tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import ConfigField, SetupType, ToolCategory, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from agno.tools.telegram import TelegramTools


@register_tool_with_metadata(
    name="telegram",
    display_name="Telegram",
    description="Send messages and media, manage messages, and read chat details via a Telegram bot",
    category=ToolCategory.COMMUNICATION,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="SiTelegram",
    icon_color="text-blue-500",  # Telegram blue
    config_fields=[
        ConfigField(
            name="chat_id",
            label="Chat ID",
            type="text",
            required=True,
        ),
        ConfigField(
            name="token",
            label="Token",
            type="password",
            required=False,
            default=None,
        ),
        ConfigField(
            name="output_directory",
            label="Output Directory",
            type="text",
            required=False,
            default=None,
            description="Directory where downloaded Telegram files are saved when save_downloads is enabled",
        ),
        ConfigField(
            name="save_downloads",
            label="Save Downloads",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="enable_send_message",
            label="Enable Send Message",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_send_photo",
            label="Enable Send Photo",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="enable_send_document",
            label="Enable Send Document",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="enable_send_video",
            label="Enable Send Video",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="enable_send_audio",
            label="Enable Send Audio",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="enable_send_animation",
            label="Enable Send Animation",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="enable_send_sticker",
            label="Enable Send Sticker",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="enable_edit_message",
            label="Enable Edit Message",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="enable_delete_message",
            label="Enable Delete Message",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="enable_react_with_emoji",
            label="Enable React With Emoji",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="enable_pin_message",
            label="Enable Pin Message",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="enable_get_chat",
            label="Enable Get Chat",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="enable_get_file",
            label="Enable Get File",
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
    dependencies=["httpx"],
    docs_url="https://core.telegram.org/bots/api",
    function_names=(
        "delete_message",
        "edit_message",
        "get_chat",
        "get_file",
        "pin_message",
        "react_with_emoji",
        "send_animation",
        "send_audio",
        "send_document",
        "send_message",
        "send_photo",
        "send_sticker",
        "send_video",
    ),
)
def telegram_tools() -> type[TelegramTools]:
    """Return Telegram tools for sending messages."""
    from agno.tools.telegram import TelegramTools

    return TelegramTools
