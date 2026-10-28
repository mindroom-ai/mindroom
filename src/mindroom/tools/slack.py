"""Slack tool registration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolFileAccess,
    ToolManagedInitArg,
    ToolStatus,
)
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.slack import SlackTools


@register_tool_with_metadata(
    name="slack",
    display_name="Slack",
    description=(
        "Search, read, download, post, react, edit, and delete in Slack as the connected user or in a project's channels"
    ),
    category=ToolCategory.COMMUNICATION,
    # Downloads always land in the workspace, like every MindRoom-owned output; no function takes a local path.
    file_access=ToolFileAccess.NONE,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.OAUTH,
    requires_primary_runtime=True,
    auth_provider="slack",
    icon="SiSlack",
    icon_color="text-purple-600",
    config_fields=[
        ConfigField(
            name="write",
            label="Allow Write Operations",
            type="boolean",
            required=False,
            default=True,
            description="Allow posting, editing, and deleting messages and adding reactions.",
        ),
    ],
    agent_override_fields=[
        ConfigField(
            name="channels",
            label="Project Channels",
            type="string[]",
            required=False,
            default=None,
            placeholder="C0123ABCD",
            description=(
                "Slack channel IDs this agent reads and posts in with the MindRoom Slack app; "
                "without channels, every requester uses their own Slack account."
            ),
        ),
        ConfigField(
            name="room",
            label="Answering Room",
            type="text",
            required=False,
            default=None,
            description="One of the agent's rooms where mentions of the MindRoom app in its channels start threads.",
        ),
    ],
    managed_init_args=(
        ToolManagedInitArg.RUNTIME_PATHS,
        ToolManagedInitArg.CREDENTIALS_MANAGER,
        ToolManagedInitArg.WORKER_TARGET,
        ToolManagedInitArg.RUNTIME_CONFIG,
        ToolManagedInitArg.TOOL_OUTPUT_WORKSPACE_ROOT,
        ToolManagedInitArg.CURRENT_ROOM_ID,
    ),
    dependencies=[],
    docs_url="https://docs.mindroom.chat/tools/slack/",
    helper_text=(
        "Each user connects their own Slack account through the MindRoom Slack app. "
        "Store the app's client ID and secret in the slack_oauth_client credential service, "
        "and its bot token, signing secret and, for watches, app-level token in slack_app."
    ),
    function_names=(
        "slack_search",
        "slack_read",
        "slack_download",
        "slack_post",
        "slack_react",
        "slack_edit",
        "slack_delete",
        "slack_watch",
    ),
)
def slack_tools() -> type[SlackTools]:
    """Return Slack tools that act as the requester or as the MindRoom Slack app in a project's channels."""
    from mindroom.custom_tools.slack import SlackTools

    return SlackTools
