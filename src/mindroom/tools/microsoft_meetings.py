"""Microsoft Teams meeting transcript tool registration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import (
    SetupType,
    ToolCategory,
    ToolFileAccess,
    ToolManagedInitArg,
    ToolStatus,
)
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.microsoft_meetings import MicrosoftMeetingsTools


@register_tool_with_metadata(
    name="microsoft_meetings",
    display_name="Microsoft Teams Meetings",
    description="Find Teams meetings by join link and read their transcripts as the connected user",
    category=ToolCategory.COMMUNICATION,
    file_access=ToolFileAccess.NONE,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.OAUTH,
    requires_primary_runtime=True,
    auth_provider="microsoft_meetings",
    icon="TbBrandTeams",
    icon_color="text-violet-600",
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
        "microsoft_meetings_find",
        "microsoft_meetings_list_transcripts",
        "microsoft_meetings_get_transcript",
    ),
)
def microsoft_meetings_tools() -> type[MicrosoftMeetingsTools]:
    """Return Microsoft Teams meeting transcript tools that act as the requester."""
    from mindroom.custom_tools.microsoft_meetings import MicrosoftMeetingsTools

    return MicrosoftMeetingsTools
