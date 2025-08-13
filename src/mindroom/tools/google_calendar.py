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
    config_fields=[
        # Authentication/Connection parameters
        ConfigField(
            name="credentials_path",
            label="Credentials Path",
            type="text",
            required=False,
            placeholder="/path/to/credentials.json",
            description="Path to the OAuth 2.0 credentials JSON file from Google Cloud Console",
        ),
        ConfigField(
            name="token_path",
            label="Token Path",
            type="password",
            required=False,
            default="token.json",
            placeholder="token.json",
            description="Path where the user's access and refresh tokens will be stored",
        ),
        ConfigField(
            name="access_token",
            label="Access Token",
            type="password",
            required=False,
            placeholder="ya29.a0...",
            description="OAuth access token (can also be set via environment variables)",
        ),
        ConfigField(
            name="calendar_id",
            label="Calendar ID",
            type="text",
            required=False,
            default="primary",
            placeholder="primary",
            description="ID of the calendar to use (default is the user's primary calendar)",
        ),
        ConfigField(
            name="oauth_port",
            label="OAuth Port",
            type="number",
            required=False,
            default=8080,
            placeholder="8080",
            description="Port to use for OAuth callback during authentication flow",
        ),
        # Permission scopes
        ConfigField(
            name="scopes",
            label="Scopes",
            type="text",
            required=False,
            placeholder="https://www.googleapis.com/auth/calendar",
            description="List of OAuth scopes for calendar access (leave empty for defaults)",
        ),
        # Feature flags
        ConfigField(
            name="allow_update",
            label="Allow Updates",
            type="boolean",
            required=False,
            default=False,
            description="Enable creating, updating, and deleting calendar events (requires write scope)",
        ),
    ],
    dependencies=["google-api-python-client", "google-auth", "google-auth-httplib2", "google-auth-oauthlib"],
    docs_url="https://docs.agno.com/tools/toolkits/others/googlecalendar",
)
def google_calendar_tools() -> type[GoogleCalendarTools]:
    """Return Google Calendar tools for calendar management."""
    from agno.tools.googlecalendar import GoogleCalendarTools

    return GoogleCalendarTools
