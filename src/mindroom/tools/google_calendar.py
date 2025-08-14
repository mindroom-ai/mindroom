"""Google Calendar tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tools_metadata import (
    SetupType,
    ToolCategory,
    ToolStatus,
    register_tool_with_metadata,
)

if TYPE_CHECKING:
    from agno.tools.googlecalendar import GoogleCalendarTools


@register_tool_with_metadata(
    name="google_calendar",
    display_name="Google Calendar",
    description="View and schedule meetings with Google Calendar",
    category=ToolCategory.PRODUCTIVITY,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.SPECIAL,
    icon="FaCalendarAlt",
    icon_color="text-blue-600",  # Google Calendar blue
    config_fields=None,  # No config fields - uses Google Services OAuth
    dependencies=["google-api-python-client", "google-auth", "google-auth-httplib2", "google-auth-oauthlib"],
    docs_url="https://docs.agno.com/tools/toolkits/others/googlecalendar",
)
def google_calendar_tools() -> type[GoogleCalendarTools]:
    """Return Google Calendar tools for calendar management."""
    from agno.tools.googlecalendar import GoogleCalendarTools

    return GoogleCalendarTools
