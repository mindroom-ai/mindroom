"""Generic OAuth provider framework."""

from mindroom.oauth.providers import (
    OAuthClaimValidationError,
    OAuthClientConfigResolution,
    OAuthProvider,
    OAuthProviderError,
    OAuthRefreshRejectedError,
    is_oauth_loopback_hostname,
)

__all__ = [
    "OAuthClaimValidationError",
    "OAuthClientConfigResolution",
    "OAuthProvider",
    "OAuthProviderError",
    "OAuthRefreshRejectedError",
    "is_oauth_loopback_hostname",
]
