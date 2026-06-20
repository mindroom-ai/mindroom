"""Generic OAuth provider framework."""

from mindroom.oauth.providers import (
    OAuthClaimValidationError,
    OAuthProvider,
    OAuthProviderError,
    OAuthRefreshRejectedError,
)

__all__ = [
    "OAuthClaimValidationError",
    "OAuthProvider",
    "OAuthProviderError",
    "OAuthRefreshRejectedError",
]
