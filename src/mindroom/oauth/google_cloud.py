"""Built-in read-only Google Cloud OAuth provider."""

from __future__ import annotations

from typing import TYPE_CHECKING

import mindroom.oauth.google as google_oauth

if TYPE_CHECKING:
    from mindroom.oauth.providers import OAuthProvider

_GOOGLE_CLOUD_READ_ONLY_SCOPE = "https://www.googleapis.com/auth/cloud-platform.read-only"
_GOOGLE_CLOUD_OAUTH_SCOPES = (
    *google_oauth.GOOGLE_IDENTITY_SCOPES,
    _GOOGLE_CLOUD_READ_ONLY_SCOPE,
)


def google_cloud_oauth_provider() -> OAuthProvider:
    """Return the shared read-only connection used by every Google Cloud tool."""
    return google_oauth._google_oauth_provider(
        provider_id="google_cloud",
        display_name="Google Cloud",
        scopes=_GOOGLE_CLOUD_OAUTH_SCOPES,
        credential_service="google_cloud_oauth",
        # Several tools share this connection; each keeps settings under its own tool name.
        tool_config_service=None,
        client_config_services=("google_cloud_oauth_client",),
        status_capabilities=("Read-only Google Cloud API access",),
        include_granted_scopes=False,
    )
