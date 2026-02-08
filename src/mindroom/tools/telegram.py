"""Telegram tool configuration."""

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
    from agno.tools.telegram import TelegramTools


@register_tool_with_metadata(
    name="telegram",
    display_name="Telegram",
    description="Send messages via Telegram bot",
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
            name="enable_send_message",
            label="Enable Send Message",
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
    dependencies=["httpx"],
    docs_url="https://core.telegram.org/bots/api",
)
def telegram_tools() -> type[TelegramTools]:
    """Return Telegram tools for sending messages."""
    from agno.tools.telegram import TelegramTools

    return TelegramTools
