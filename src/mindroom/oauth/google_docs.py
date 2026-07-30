"""Built-in Google Docs OAuth provider."""

from __future__ import annotations

from typing import TYPE_CHECKING

import mindroom.oauth.google as google_oauth

if TYPE_CHECKING:
    from mindroom.oauth.providers import OAuthProvider

_GOOGLE_DOCS_OAUTH_SCOPES = (
    *google_oauth.GOOGLE_IDENTITY_SCOPES,
    "https://www.googleapis.com/auth/documents",
)


def google_docs_oauth_provider() -> OAuthProvider:
    """Return the built-in Google Docs provider definition."""
    return google_oauth._google_oauth_provider(
        provider_id="google_docs",
        display_name="Google Docs",
        scopes=_GOOGLE_DOCS_OAUTH_SCOPES,
        credential_service="google_docs_oauth",
        tool_config_service="google_docs",
        client_config_services=("google_docs_oauth_client",),
        status_capabilities=(
            "Docs create and read",
            "Docs text editing",
        ),
        include_granted_scopes=False,
    )
