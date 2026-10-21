"""Microsoft 365 People tool registration."""

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
    from mindroom.custom_tools.microsoft_people import MicrosoftPeopleTools


@register_tool_with_metadata(
    name="microsoft_people",
    display_name="Microsoft 365 People",
    description="Look up people, their managers, and their direct reports in the connected user's organization",
    category=ToolCategory.PRODUCTIVITY,
    file_access=ToolFileAccess.NONE,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.OAUTH,
    requires_primary_runtime=True,
    auth_provider="microsoft_people",
    icon="Users",
    icon_color="text-blue-600",
    managed_init_args=(
        ToolManagedInitArg.RUNTIME_PATHS,
        ToolManagedInitArg.CREDENTIALS_MANAGER,
        ToolManagedInitArg.WORKER_TARGET,
        ToolManagedInitArg.RUNTIME_CONFIG,
    ),
    docs_url="https://docs.mindroom.chat/tools/microsoft-365/",
    helper_text=(
        "Each user connects their own Microsoft account. "
        "Store the Microsoft Entra app's client ID and secret in the microsoft_oauth_client credential service. "
        "An administrator must grant consent for User.Read.All."
    ),
    function_names=(
        "microsoft_people_search",
        "microsoft_people_get_profile",
        "microsoft_people_list_direct_reports",
    ),
)
def microsoft_people_tools() -> type[MicrosoftPeopleTools]:
    """Return Microsoft 365 People tools that act as the requester."""
    from mindroom.custom_tools.microsoft_people import MicrosoftPeopleTools

    return MicrosoftPeopleTools
