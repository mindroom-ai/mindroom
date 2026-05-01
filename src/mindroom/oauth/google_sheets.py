"""Built-in Google Sheets OAuth provider."""

from __future__ import annotations

from mindroom.oauth.google import (
    GOOGLE_IDENTITY_SCOPES,
    _google_client_config_services,
    _google_domain_env_names,
    _google_provider_env_names,
    _google_redirect_env_names,
    _google_shared_client_config_services,
    _google_token_parser,
)
from mindroom.oauth.providers import OAuthProvider

GOOGLE_SHEETS_OAUTH_SCOPES = (
    *GOOGLE_IDENTITY_SCOPES,
    "https://www.googleapis.com/auth/spreadsheets",
)


def google_sheets_oauth_provider() -> OAuthProvider:
    """Return the built-in Google Sheets provider definition."""
    client_id_env, client_secret_env = _google_provider_env_names("google_sheets")
    return OAuthProvider(
        id="google_sheets",
        display_name="Google Sheets",
        authorization_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",  # noqa: S106
        scopes=GOOGLE_SHEETS_OAUTH_SCOPES,
        credential_service="google_sheets_oauth",
        tool_config_service="google_sheets",
        client_config_services=_google_client_config_services("google_sheets"),
        shared_client_config_services=_google_shared_client_config_services(),
        client_id_env=client_id_env,
        client_secret_env=client_secret_env,
        redirect_uri_env=_google_redirect_env_names("google_sheets"),
        allowed_email_domains_env=_google_domain_env_names("google_sheets", "ALLOWED_EMAIL_DOMAINS"),
        allowed_hosted_domains_env=_google_domain_env_names("google_sheets", "ALLOWED_HOSTED_DOMAINS"),
        extra_auth_params={
            "access_type": "offline",
            "include_granted_scopes": "true",
            "prompt": "consent",
        },
        status_capabilities=("Sheets read/write",),
        token_parser=_google_token_parser,
    )
