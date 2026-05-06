"""Built-in Gmail OAuth provider."""

from __future__ import annotations

from typing import TYPE_CHECKING

import mindroom.oauth.google as google_oauth

if TYPE_CHECKING:
    from mindroom.oauth.providers import OAuthProvider

_GOOGLE_GMAIL_OAUTH_SCOPES = (
    *google_oauth.GOOGLE_IDENTITY_SCOPES,
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.compose",
)


def google_gmail_oauth_provider() -> OAuthProvider:
    """Return the built-in Gmail provider definition."""
    return google_oauth._google_oauth_provider(
        provider_id="google_gmail",
        display_name="Gmail",
        scopes=_GOOGLE_GMAIL_OAUTH_SCOPES,
        credential_service="google_gmail_oauth",
        tool_config_service="gmail",
        client_config_services=("google_gmail_oauth_client",),
        status_capabilities=("Gmail read/modify/compose",),
    )
