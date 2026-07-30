"""Generic OAuth provider framework."""

from mindroom.oauth.providers import (
    OAuthClaimValidationError,
    OAuthClientConfigResolution,
    OAuthProvider,
    OAuthProviderError,
    OAuthRefreshRejectedError,
    is_oauth_loopback_hostname,
    oauth_connect_url_requires_host_browser,
)

__all__ = [
    "OAuthClaimValidationError",
    "OAuthClientConfigResolution",
    "OAuthProvider",
    "OAuthProviderError",
    "OAuthRefreshRejectedError",
    "is_oauth_loopback_hostname",
    "oauth_connect_url_requires_host_browser",
]
