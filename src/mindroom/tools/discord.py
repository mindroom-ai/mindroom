"""Discord tool configuration."""

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
    from agno.tools.discord import DiscordTools


@register_tool_with_metadata(
    name="discord",
    display_name="Discord",
    description="Tool for interacting with Discord channels and servers",
    category=ToolCategory.COMMUNICATION,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="FaDiscord",
    icon_color="text-indigo-500",  # Discord brand color
    config_fields=[
        # Authentication
        ConfigField(
            name="bot_token",
            label="Bot Token",
            type="password",
            required=False,
            placeholder="MTAx...",
            description="Discord bot token for authentication (can also be set via DISCORD_BOT_TOKEN env var)",
        ),
        # Feature flags - Messaging
        ConfigField(
            name="enable_messaging",
            label="Enable Messaging",
            type="boolean",
            required=False,
            default=True,
            description="Enable sending messages to Discord channels",
        ),
        # Feature flags - History and Channel Management
        ConfigField(
            name="enable_history",
            label="Enable History",
            type="boolean",
            required=False,
            default=True,
            description="Enable retrieving message history from channels",
        ),
        ConfigField(
            name="enable_channel_management",
            label="Enable Channel Management",
            type="boolean",
            required=False,
            default=True,
            description="Enable getting channel info and listing channels",
        ),
        # Feature flags - Message Management
        ConfigField(
            name="enable_message_management",
            label="Enable Message Management",
            type="boolean",
            required=False,
            default=True,
            description="Enable deleting messages from channels",
        ),
    ],
    dependencies=["requests"],
    docs_url="https://docs.agno.com/tools/toolkits/social/discord",
)
def discord_tools() -> type[DiscordTools]:
    """Return Discord tools for interacting with Discord channels and servers."""
    from agno.tools.discord import DiscordTools

    return DiscordTools
