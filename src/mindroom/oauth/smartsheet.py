"""Built-in Smartsheet OAuth provider."""

from __future__ import annotations

from mindroom.oauth.providers import OAuthProvider

__all__ = ["SMARTSHEET_SCOPES", "smartsheet_oauth_provider"]

# What the Smartsheet toolkit reads and changes: sheets, their rows, and search results.
SMARTSHEET_SCOPES = ("READ_SHEETS", "WRITE_SHEETS")


def smartsheet_oauth_provider() -> OAuthProvider:
    """Return the built-in Smartsheet OAuth provider definition; tokens always belong to the requesting user."""
    return OAuthProvider(
        id="smartsheet",
        display_name="Smartsheet",
        authorization_url="https://app.smartsheet.com/b/authorize",
        token_url="https://api.smartsheet.com/2.0/token",  # noqa: S106
        scopes=SMARTSHEET_SCOPES,
        credential_service="smartsheet_oauth",
        tool_config_service="smartsheet",
        client_config_services=("smartsheet_oauth_client",),
        requester_scoped_credentials=True,
        # A saved or environment API access token replaces the requester's OAuth connection.
        tool_config_oauth_fallback_fields=("access_token",),
    )
