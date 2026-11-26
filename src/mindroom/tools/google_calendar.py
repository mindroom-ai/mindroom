"""Google Calendar tool configuration."""

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
    from mindroom.custom_tools.google_calendar import GoogleCalendarTools


@register_tool_with_metadata(
    name="google_calendar",
    file_access=ToolFileAccess.NONE,
    display_name="Google Calendar",
    description="View calendars and create or update events in Google Calendar",
    category=ToolCategory.PRODUCTIVITY,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.OAUTH,
    requires_primary_runtime=True,
    auth_provider="google_calendar",
    icon="SiGooglecalendar",
    icon_color="text-blue-600",  # Google Calendar blue
    config_fields=[
        ConfigField(
            name="calendar_id",
            label="Calendar ID",
            type="text",
            required=False,
            default="primary",
            placeholder="primary",
            description="The Google Calendar ID to use (default: 'primary' for the user's main calendar)",
        ),
        ConfigField(
            name="allow_update",
            label="Allow Updates",
            type="boolean",
            required=False,
            default=True,
            description="Allow the agent to create, update, and delete calendar events",
        ),
    ],
    managed_init_args=(
        ToolManagedInitArg.RUNTIME_PATHS,
        ToolManagedInitArg.CREDENTIALS_MANAGER,
        ToolManagedInitArg.WORKER_TARGET,
        ToolManagedInitArg.RUNTIME_CONFIG,
    ),
    dependencies=["google-api-python-client", "google-auth", "google-auth-httplib2", "google-auth-oauthlib"],
    docs_url="https://docs.mindroom.chat/tools/calendar-and-scheduling/#google_calendar",
    function_names=(
        "google_calendar_read",
        "google_calendar_write",
    ),
    action_scoped_grants=(
        "google_calendar_read",
        "google_calendar_write",
    ),
)
def google_calendar_tools() -> type[GoogleCalendarTools]:
    """Return Google Calendar tools for calendar management."""
    from mindroom.custom_tools.google_calendar import GoogleCalendarTools

    return GoogleCalendarTools
