"""Discord tool configuration."""

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
    from agno.tools.discord import DiscordTools


@register_tool_with_metadata(
    name="discord",
    display_name="Discord",
    description="Tool for interacting with Discord channels and servers",
    category=ToolCategory.COMMUNICATION,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="SiDiscord",
    icon_color="text-indigo-500",  # Discord brand color
    config_fields=[
        ConfigField(
            name="bot_token",
            label="Bot Token",
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
            name="enable_get_channel_messages",
            label="Enable Get Channel Messages",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_get_channel_info",
            label="Enable Get Channel Info",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_list_channels",
            label="Enable List Channels",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_delete_message",
            label="Enable Delete Message",
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
    docs_url="https://docs.agno.com/tools/toolkits/social/discord",
)
def discord_tools() -> type[DiscordTools]:
    """Return Discord tools for interacting with Discord channels and servers."""
    from agno.tools.discord import DiscordTools

    return DiscordTools
