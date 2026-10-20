"""Outlook calendar tool registration."""

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
    from mindroom.custom_tools.outlook_calendar import OutlookCalendarTools


@register_tool_with_metadata(
    name="outlook_calendar",
    display_name="Outlook Calendar",
    description="View Outlook calendars and create, change, and answer events as the connected user",
    category=ToolCategory.PRODUCTIVITY,
    file_access=ToolFileAccess.NONE,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.OAUTH,
    requires_primary_runtime=True,
    auth_provider="outlook_calendar",
    icon="CalendarDays",
    icon_color="text-blue-600",
    config_fields=[
        ConfigField(
            name="write",
            label="Allow Write Operations",
            type="boolean",
            required=False,
            default=True,
            description="Allow creating, changing, deleting, and answering events.",
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
        "outlook_calendar_list_calendars",
        "outlook_calendar_list_events",
        "outlook_calendar_get_event",
        "outlook_calendar_create_event",
        "outlook_calendar_update_event",
        "outlook_calendar_delete_event",
        "outlook_calendar_respond_to_event",
    ),
)
def outlook_calendar_tools() -> type[OutlookCalendarTools]:
    """Return Outlook calendar tools that act as the requester."""
    from mindroom.custom_tools.outlook_calendar import OutlookCalendarTools

    return OutlookCalendarTools
