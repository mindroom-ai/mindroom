"""Tests for built-in Google OAuth provider definitions."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom.oauth.google import _google_oauth_provider, _google_token_parser
from mindroom.oauth.google_calendar import _GOOGLE_CALENDAR_OAUTH_SCOPES, google_calendar_oauth_provider
from mindroom.oauth.google_drive import _GOOGLE_DRIVE_OAUTH_SCOPES, google_drive_oauth_provider
from mindroom.oauth.google_gmail import _GOOGLE_GMAIL_OAUTH_SCOPES, google_gmail_oauth_provider
from mindroom.oauth.google_sheets import _GOOGLE_SHEETS_OAUTH_SCOPES, google_sheets_oauth_provider
from mindroom.oauth.providers import OAuthConnectionRequired, oauth_connection_required_payload
from mindroom.oauth.service import build_oauth_connect_instruction

if TYPE_CHECKING:
    from mindroom.oauth.providers import OAuthProvider

GOOGLE_AUTHORIZATION_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105
GOOGLE_EXTRA_AUTH_PARAMS = {
    "access_type": "offline",
    "include_granted_scopes": "true",
    "prompt": "consent",
}


@pytest.mark.parametrize(
    ("provider", "expected"),
    [
        (
            google_calendar_oauth_provider(),
            {
                "id": "google_calendar",
                "display_name": "Google Calendar",
                "scopes": _GOOGLE_CALENDAR_OAUTH_SCOPES,
                "credential_service": "google_calendar_oauth",
                "tool_config_service": "google_calendar",
                "client_config_services": ("google_calendar_oauth_client",),
                "status_capabilities": ("Calendar event read/write",),
            },
        ),
        (
            google_drive_oauth_provider(),
            {
                "id": "google_drive",
                "display_name": "Google Drive",
                "scopes": _GOOGLE_DRIVE_OAUTH_SCOPES,
                "credential_service": "google_drive_oauth",
                "tool_config_service": "google_drive",
                "client_config_services": ("google_drive_oauth_client",),
                "status_capabilities": ("Drive file search", "Drive file read"),
            },
        ),
        (
            google_gmail_oauth_provider(),
            {
                "id": "google_gmail",
                "display_name": "Gmail",
                "scopes": _GOOGLE_GMAIL_OAUTH_SCOPES,
                "credential_service": "google_gmail_oauth",
                "tool_config_service": "gmail",
                "client_config_services": ("google_gmail_oauth_client",),
                "status_capabilities": ("Gmail read/modify/compose",),
            },
        ),
        (
            google_sheets_oauth_provider(),
            {
                "id": "google_sheets",
                "display_name": "Google Sheets",
                "scopes": _GOOGLE_SHEETS_OAUTH_SCOPES,
                "credential_service": "google_sheets_oauth",
                "tool_config_service": "google_sheets",
                "client_config_services": ("google_sheets_oauth_client",),
                "status_capabilities": ("Sheets read/write",),
            },
        ),
    ],
)
def test_public_google_oauth_providers_preserve_service_specific_fields(
    provider: OAuthProvider,
    expected: dict[str, object],
) -> None:
    """Public Google provider factories preserve service-specific metadata."""
    assert provider.id == expected["id"]
    assert provider.display_name == expected["display_name"]
    assert provider.scopes == expected["scopes"]
    assert provider.credential_service == expected["credential_service"]
    assert provider.tool_config_service == expected["tool_config_service"]
    assert provider.client_config_services == expected["client_config_services"]
    assert provider.status_capabilities == expected["status_capabilities"]


@pytest.mark.parametrize(
    "provider",
    [
        google_calendar_oauth_provider(),
        google_drive_oauth_provider(),
        google_gmail_oauth_provider(),
        google_sheets_oauth_provider(),
    ],
)
def test_public_google_oauth_providers_preserve_shared_google_oauth_fields(provider: OAuthProvider) -> None:
    """Public Google provider factories preserve shared Google OAuth metadata."""
    provider_prefix = provider.id.upper()

    assert provider.authorization_url == GOOGLE_AUTHORIZATION_URL
    assert provider.token_url == GOOGLE_TOKEN_URL
    assert provider.shared_client_config_services == ("google_oauth_client",)
    assert provider.allowed_email_domains_env == (
        f"{provider_prefix}_ALLOWED_EMAIL_DOMAINS",
        f"MINDROOM_OAUTH_{provider_prefix}_ALLOWED_EMAIL_DOMAINS",
    )
    assert provider.allowed_hosted_domains_env == (
        f"{provider_prefix}_ALLOWED_HOSTED_DOMAINS",
        f"MINDROOM_OAUTH_{provider_prefix}_ALLOWED_HOSTED_DOMAINS",
    )
    assert provider.extra_auth_params == GOOGLE_EXTRA_AUTH_PARAMS
    assert provider.token_parser is _google_token_parser


def test_google_oauth_provider_helper_builds_common_google_provider_skeleton() -> None:
    """The private Google helper builds the shared provider skeleton."""
    provider = _google_oauth_provider(
        provider_id="google_example",
        display_name="Google Example",
        scopes=("openid", "https://www.googleapis.com/auth/example"),
        credential_service="google_example_oauth",
        tool_config_service="google_example",
        client_config_services=("google_example_oauth_client",),
        status_capabilities=("Example read/write",),
    )

    assert provider.id == "google_example"
    assert provider.display_name == "Google Example"
    assert provider.authorization_url == GOOGLE_AUTHORIZATION_URL
    assert provider.token_url == GOOGLE_TOKEN_URL
    assert provider.scopes == ("openid", "https://www.googleapis.com/auth/example")
    assert provider.credential_service == "google_example_oauth"
    assert provider.tool_config_service == "google_example"
    assert provider.client_config_services == ("google_example_oauth_client",)
    assert provider.shared_client_config_services == ("google_oauth_client",)
    assert provider.allowed_email_domains_env == (
        "GOOGLE_EXAMPLE_ALLOWED_EMAIL_DOMAINS",
        "MINDROOM_OAUTH_GOOGLE_EXAMPLE_ALLOWED_EMAIL_DOMAINS",
    )
    assert provider.allowed_hosted_domains_env == (
        "GOOGLE_EXAMPLE_ALLOWED_HOSTED_DOMAINS",
        "MINDROOM_OAUTH_GOOGLE_EXAMPLE_ALLOWED_HOSTED_DOMAINS",
    )
    assert provider.extra_auth_params == GOOGLE_EXTRA_AUTH_PARAMS
    assert provider.status_capabilities == ("Example read/write",)
    assert provider.token_parser is _google_token_parser


def test_oauth_connection_required_payload_preserves_structured_fields() -> None:
    """OAuth-required tool payloads keep the established public field names."""
    exc = OAuthConnectionRequired(
        "Google Drive is not connected for this agent.",
        provider_id="google_drive",
        connect_url="/api/oauth/google_drive/connect?agent_name=general",
    )

    assert oauth_connection_required_payload(exc) == {
        "error": "Google Drive is not connected for this agent.",
        "oauth_connection_required": True,
        "provider": "google_drive",
        "connect_url": "/api/oauth/google_drive/connect?agent_name=general",
    }


def test_build_oauth_connect_instruction_formats_shared_message() -> None:
    """OAuth connection instructions should have one shared service formatter."""
    assert build_oauth_connect_instruction(
        google_drive_oauth_provider(),
        "/api/oauth/google_drive/connect?agent_name=general",
    ) == (
        "Google Drive is not connected for this agent. "
        "Open this MindRoom link to connect it, then retry the request: "
        "/api/oauth/google_drive/connect?agent_name=general"
    )
