"""Built-in Google Calendar OAuth provider."""

from __future__ import annotations

from mindroom.oauth.google import (
    GOOGLE_IDENTITY_SCOPES,
    _google_domain_env_names,
    _google_provider_env_names,
    _google_redirect_env_names,
    _google_token_parser,
)
from mindroom.oauth.providers import OAuthProvider

GOOGLE_CALENDAR_OAUTH_SCOPES = (
    *GOOGLE_IDENTITY_SCOPES,
    "https://www.googleapis.com/auth/calendar.readonly",
)
GOOGLE_CALENDAR_WRITE_OAUTH_SCOPES = (
    *GOOGLE_IDENTITY_SCOPES,
    "https://www.googleapis.com/auth/calendar",
)


def google_calendar_oauth_provider(*, allow_update: bool = False) -> OAuthProvider:
    """Return the built-in Google Calendar provider definition."""
    client_id_env, client_secret_env = _google_provider_env_names("google_calendar")
    return OAuthProvider(
        id="google_calendar",
        display_name="Google Calendar",
        authorization_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",  # noqa: S106
        scopes=GOOGLE_CALENDAR_WRITE_OAUTH_SCOPES if allow_update else GOOGLE_CALENDAR_OAUTH_SCOPES,
        credential_service="google_calendar_oauth",
        tool_config_service="google_calendar",
        client_id_env=client_id_env,
        client_secret_env=client_secret_env,
        redirect_uri_env=_google_redirect_env_names("google_calendar"),
        allowed_email_domains_env=_google_domain_env_names("google_calendar", "ALLOWED_EMAIL_DOMAINS"),
        allowed_hosted_domains_env=_google_domain_env_names("google_calendar", "ALLOWED_HOSTED_DOMAINS"),
        extra_auth_params={
            "access_type": "offline",
            "include_granted_scopes": "true",
            "prompt": "consent",
        },
        status_capabilities=("Calendar event read/write",),
        token_parser=_google_token_parser,
    )
