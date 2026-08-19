"""Generic OAuth provider framework."""

from mindroom.oauth.discovery import OAuthDiscoveryConfig, oauth_runtime_bootstrapper
from mindroom.oauth.providers import (
    OAuthClaimValidationError,
    OAuthClientConfigResolution,
    OAuthProvider,
    OAuthProviderError,
    OAuthRefreshRejectedError,
    is_oauth_loopback_hostname,
    is_valid_non_loopback_oauth_hostname,
    oauth_connect_url_requires_host_browser,
)

__all__ = [
    "OAuthClaimValidationError",
    "OAuthClientConfigResolution",
    "OAuthDiscoveryConfig",
    "OAuthProvider",
    "OAuthProviderError",
    "OAuthRefreshRejectedError",
    "is_oauth_loopback_hostname",
    "is_valid_non_loopback_oauth_hostname",
    "oauth_connect_url_requires_host_browser",
    "oauth_runtime_bootstrapper",
]
