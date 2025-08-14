"""Google Calendar tool configuration."""

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
    from mindroom.custom_tools.google_calendar import GoogleCalendarTools


@register_tool_with_metadata(
    name="google_calendar",
    display_name="Google Calendar",
    description="View and schedule meetings with Google Calendar",
    category=ToolCategory.PRODUCTIVITY,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.SPECIAL,
    auth_provider="google",  # Authentication provided by Google Services integration
    icon="FaCalendarAlt",
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
            default=False,
            description="Allow the agent to create, update, and delete calendar events",
        ),
    ],
    dependencies=["google-api-python-client", "google-auth", "google-auth-httplib2", "google-auth-oauthlib"],
    docs_url="https://docs.agno.com/tools/toolkits/others/googlecalendar",
)
def google_calendar_tools() -> type[GoogleCalendarTools]:
    """Return Google Calendar tools for calendar management."""
    from mindroom.custom_tools.google_calendar import GoogleCalendarTools

    return GoogleCalendarTools
