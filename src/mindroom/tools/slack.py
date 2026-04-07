"""Slack tool configuration."""

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
            name="output_directory",
            label="Output Directory",
            type="text",
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
            name="enable_upload_file",
            label="Enable Upload File",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_download_file",
            label="Enable Download File",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_search_messages",
            label="Enable Search Messages",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="enable_get_thread",
            label="Enable Get Thread",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="enable_list_users",
            label="Enable List Users",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="enable_get_user_info",
            label="Enable Get User Info",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="enable_get_channel_info",
            label="Enable Get Channel Info",
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
        ConfigField(
            name="max_file_size",
            label="Max File Size",
            type="number",
            required=False,
            default=1073741824,
        ),
        ConfigField(
            name="thread_message_limit",
            label="Thread Message Limit",
            type="number",
            required=False,
            default=20,
        ),
    ],
    dependencies=["slack-sdk"],
    docs_url="https://docs.agno.com/tools/toolkits/social/slack",
)
def slack_tools() -> type[SlackTools]:
    """Return Slack tools for messaging and channel management."""
    from agno.tools.slack import SlackTools

    return SlackTools
