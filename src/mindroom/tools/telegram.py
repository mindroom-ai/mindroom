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
    icon="FaTelegram",
    icon_color="text-blue-500",  # Telegram blue
    config_fields=[
        # Authentication/Connection parameters
        ConfigField(
            name="chat_id",
            label="Chat ID",
            type="text",
            required=True,
            placeholder="123456789 or @username",
            description="The chat ID or username to send messages to",
        ),
        ConfigField(
            name="token",
            label="Bot Token",
            type="password",
            required=False,
            placeholder="1234567890:ABCdefGHijKlmnOPqrSTuvwXYZ",
            description="Telegram bot token (can also be set via TELEGRAM_TOKEN env var)",
        ),
    ],
    dependencies=["httpx"],
    docs_url="https://core.telegram.org/bots/api",
)
def telegram_tools() -> type[TelegramTools]:
    """Return Telegram tools for sending messages."""
    from agno.tools.telegram import TelegramTools

    return TelegramTools
