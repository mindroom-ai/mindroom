"""Microsoft Teams tool registration."""

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
    from mindroom.custom_tools.microsoft_teams import MicrosoftTeamsTools


@register_tool_with_metadata(
    name="microsoft_teams",
    display_name="Microsoft Teams",
    description="Read and send Teams channel and chat messages, start chats, and read presence as the connected user",
    category=ToolCategory.COMMUNICATION,
    file_access=ToolFileAccess.NONE,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.OAUTH,
    requires_primary_runtime=True,
    auth_provider="microsoft_teams",
    icon="TbBrandTeams",
    icon_color="text-indigo-600",
    config_fields=[
        ConfigField(
            name="write",
            label="Allow Write Operations",
            type="boolean",
            required=False,
            default=True,
            description="Allow sending channel messages, channel replies, and chat messages, and starting chats.",
        ),
    ],
    managed_init_args=(
        ToolManagedInitArg.RUNTIME_PATHS,
        ToolManagedInitArg.CREDENTIALS_MANAGER,
        ToolManagedInitArg.WORKER_TARGET,
        ToolManagedInitArg.RUNTIME_CONFIG,
    ),
    docs_url="https://docs.mindroom.chat/tools/microsoft-365/",
    helper_text=(
        "Each user connects their own Microsoft account. "
        "Store the Microsoft Entra app's client ID and secret in the microsoft_oauth_client credential service."
    ),
    function_names=(
        "microsoft_teams_list_teams",
        "microsoft_teams_list_channels",
        "microsoft_teams_list_channel_messages",
        "microsoft_teams_list_channel_message_replies",
        "microsoft_teams_list_chats",
        "microsoft_teams_list_chat_messages",
        "microsoft_teams_get_presence",
        "microsoft_teams_send_channel_message",
        "microsoft_teams_reply_to_channel_message",
        "microsoft_teams_send_chat_message",
        "microsoft_teams_start_chat",
    ),
)
def microsoft_teams_tools() -> type[MicrosoftTeamsTools]:
    """Return Microsoft Teams tools that act as the requester."""
    from mindroom.custom_tools.microsoft_teams import MicrosoftTeamsTools

    return MicrosoftTeamsTools
