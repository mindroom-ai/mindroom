"""Built-in Google Drive OAuth provider."""

from __future__ import annotations

from typing import TYPE_CHECKING

import mindroom.oauth.google as google_oauth

if TYPE_CHECKING:
    from mindroom.oauth.providers import OAuthProvider

_GOOGLE_DRIVE_OAUTH_SCOPES = (
    *google_oauth.GOOGLE_IDENTITY_SCOPES,
    "https://www.googleapis.com/auth/drive.readonly",
)


def google_drive_oauth_provider() -> OAuthProvider:
    """Return the built-in Google Drive provider definition."""
    return google_oauth._google_oauth_provider(
        provider_id="google_drive",
        display_name="Google Drive",
        scopes=_GOOGLE_DRIVE_OAUTH_SCOPES,
        credential_service="google_drive_oauth",
        tool_config_service="google_drive",
        client_config_services=("google_drive_oauth_client",),
        status_capabilities=(
            "Drive file search",
            "Drive file read",
        ),
    )
