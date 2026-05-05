"""Generic OAuth provider framework."""

from mindroom.oauth.providers import (
    OAuthClaimValidationError,
    OAuthProvider,
    OAuthProviderError,
)
from mindroom.oauth.registry import load_oauth_providers_for_snapshot

__all__ = [
    "OAuthClaimValidationError",
    "OAuthProvider",
    "OAuthProviderError",
    "load_oauth_providers_for_snapshot",
]
