"""Built-in Google Calendar OAuth provider."""

from __future__ import annotations

from typing import TYPE_CHECKING

import mindroom.oauth.google as google_oauth

if TYPE_CHECKING:
    from mindroom.oauth.providers import OAuthProvider

_GOOGLE_CALENDAR_OAUTH_SCOPES = (
    *google_oauth.GOOGLE_IDENTITY_SCOPES,
    "https://www.googleapis.com/auth/calendar",
)


def google_calendar_oauth_provider() -> OAuthProvider:
    """Return the built-in Google Calendar provider definition."""
    return google_oauth._google_oauth_provider(
        provider_id="google_calendar",
        display_name="Google Calendar",
        scopes=_GOOGLE_CALENDAR_OAUTH_SCOPES,
        credential_service="google_calendar_oauth",
        tool_config_service="google_calendar",
        client_config_services=("google_calendar_oauth_client",),
        status_capabilities=("Calendar event read/write",),
    )
