"""Generic OAuth provider framework."""

from mindroom.oauth.providers import OAuthClaimValidationError, OAuthProvider, OAuthProviderError

__all__ = [
    "OAuthClaimValidationError",
    "OAuthProvider",
    "OAuthProviderError",
]
