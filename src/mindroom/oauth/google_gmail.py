"""Built-in Gmail OAuth provider."""

from __future__ import annotations

from mindroom.oauth.google import (
    GOOGLE_IDENTITY_SCOPES,
    google_domain_env_names,
    google_token_parser,
)
from mindroom.oauth.providers import OAuthProvider

_GOOGLE_GMAIL_OAUTH_SCOPES = (
    *GOOGLE_IDENTITY_SCOPES,
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.compose",
)


def google_gmail_oauth_provider() -> OAuthProvider:
    """Return the built-in Gmail provider definition."""
    return OAuthProvider(
        id="google_gmail",
        display_name="Gmail",
        authorization_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",  # noqa: S106
        scopes=_GOOGLE_GMAIL_OAUTH_SCOPES,
        credential_service="google_gmail_oauth",
        tool_config_service="gmail",
        client_config_services=("google_gmail_oauth_client",),
        shared_client_config_services=("google_oauth_client",),
        allowed_email_domains_env=google_domain_env_names("google_gmail", "ALLOWED_EMAIL_DOMAINS"),
        allowed_hosted_domains_env=google_domain_env_names("google_gmail", "ALLOWED_HOSTED_DOMAINS"),
        extra_auth_params={
            "access_type": "offline",
            "include_granted_scopes": "true",
            "prompt": "consent",
        },
        status_capabilities=("Gmail read/modify/compose",),
        token_parser=google_token_parser,
    )
