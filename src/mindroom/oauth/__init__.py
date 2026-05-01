"""Generic OAuth provider framework."""

from mindroom.oauth.providers import (
    OAuthClaimValidationContext,
    OAuthClaimValidationError,
    OAuthClientConfig,
    OAuthConnectionRequired,
    OAuthProvider,
    OAuthProviderError,
    OAuthProviderNotConfiguredError,
    OAuthTokenResult,
)
from mindroom.oauth.registry import load_oauth_providers, load_oauth_providers_for_snapshot
from mindroom.oauth.service import build_oauth_authorize_url, build_oauth_connect_instruction

__all__ = [
    "OAuthClaimValidationContext",
    "OAuthClaimValidationError",
    "OAuthClientConfig",
    "OAuthConnectionRequired",
    "OAuthProvider",
    "OAuthProviderError",
    "OAuthProviderNotConfiguredError",
    "OAuthTokenResult",
    "build_oauth_authorize_url",
    "build_oauth_connect_instruction",
    "load_oauth_providers",
    "load_oauth_providers_for_snapshot",
]
