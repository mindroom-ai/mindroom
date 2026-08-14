"""Built-in GitHub App user OAuth provider."""

from __future__ import annotations

from mindroom.oauth.providers import OAuthProvider


def github_oauth_provider() -> OAuthProvider:
    """Return the built-in GitHub App user OAuth provider definition."""
    return OAuthProvider(
        id="github",
        display_name="GitHub",
        authorization_url="https://github.com/login/oauth/authorize",
        token_url="https://github.com/login/oauth/access_token",  # noqa: S106
        scopes=(),
        allow_empty_scopes=True,
        credential_service="github_oauth",
        tool_config_service="github",
        client_config_services=("github_oauth_client",),
        default_redirect_path="/api/oauth/github/callback",
        pkce_code_challenge_method="S256",
        requester_scoped_credentials=True,
        tool_config_oauth_fallback_fields=("access_token",),
        tool_config_oauth_fallback_env_vars=("GITHUB_ACCESS_TOKEN",),
    )
