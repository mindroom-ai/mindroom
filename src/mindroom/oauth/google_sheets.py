"""Built-in Google Sheets OAuth provider."""

from __future__ import annotations

from typing import TYPE_CHECKING

import mindroom.oauth.google as google_oauth

if TYPE_CHECKING:
    from mindroom.oauth.providers import OAuthProvider

_GOOGLE_SHEETS_OAUTH_SCOPES = (
    *google_oauth.GOOGLE_IDENTITY_SCOPES,
    "https://www.googleapis.com/auth/spreadsheets",
)


def google_sheets_oauth_provider() -> OAuthProvider:
    """Return the built-in Google Sheets provider definition."""
    return google_oauth._google_oauth_provider(
        provider_id="google_sheets",
        display_name="Google Sheets",
        scopes=_GOOGLE_SHEETS_OAUTH_SCOPES,
        credential_service="google_sheets_oauth",
        tool_config_service="google_sheets",
        client_config_services=("google_sheets_oauth_client",),
        status_capabilities=("Sheets read/write",),
    )
