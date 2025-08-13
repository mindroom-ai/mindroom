"""Slack tool configuration."""

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
    from agno.tools.slack import SlackTools


@register_tool_with_metadata(
    name="slack",
    display_name="Slack",
    description="Send messages and manage channels",
    category=ToolCategory.COMMUNICATION,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="FaSlack",
    icon_color="text-purple-600",
    config_fields=[
        # Authentication
        ConfigField(
            name="token",
            label="Slack Token",
            type="password",
            required=False,
            placeholder="xoxb-...",
            description="Slack bot token (can also be set via SLACK_TOKEN env var)",
        ),
        # Message operations
        ConfigField(
            name="send_message",
            label="Send Message",
            type="boolean",
            required=False,
            default=True,
            description="Enable sending messages to channels",
        ),
        ConfigField(
            name="send_message_thread",
            label="Send Message Thread",
            type="boolean",
            required=False,
            default=True,
            description="Enable sending threaded messages",
        ),
        # Channel operations
        ConfigField(
            name="list_channels",
            label="List Channels",
            type="boolean",
            required=False,
            default=True,
            description="Enable listing workspace channels",
        ),
        ConfigField(
            name="get_channel_history",
            label="Get Channel History",
            type="boolean",
            required=False,
            default=True,
            description="Enable retrieving channel message history",
        ),
    ],
    dependencies=["slack-sdk"],
    docs_url="https://docs.agno.com/tools/toolkits/social/slack",
)
def slack_tools() -> type[SlackTools]:
    """Return Slack tools for messaging and channel management."""
    from agno.tools.slack import SlackTools

    return SlackTools
