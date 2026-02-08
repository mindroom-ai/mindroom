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
    icon="SiSlack",
    icon_color="text-purple-600",
    config_fields=[
        ConfigField(
            name="token",
            label="Token",
            type="password",
            required=False,
            default=None,
        ),
        ConfigField(
            name="markdown",
            label="Markdown",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_send_message",
            label="Enable Send Message",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_send_message_thread",
            label="Enable Send Message Thread",
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
            name="enable_get_channel_history",
            label="Enable Get Channel History",
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
    dependencies=["slack-sdk"],
    docs_url="https://docs.agno.com/tools/toolkits/social/slack",
)
def slack_tools() -> type[SlackTools]:
    """Return Slack tools for messaging and channel management."""
    from agno.tools.slack import SlackTools

    return SlackTools
