"""Built-in GitHub App user OAuth provider."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import httpx

from mindroom.oauth.providers import OAuthProvider

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["GITHUB_USER_API_URL", "LOGIN_LOOKUP_TIMEOUT_SECONDS", "github_oauth_provider"]

GITHUB_USER_API_URL = "https://api.github.com/user"
LOGIN_LOOKUP_TIMEOUT_SECONDS = 5.0
# GitHub logins are 1 to 39 letters, digits, or single hyphens that never start the name.
_GITHUB_LOGIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}")


def _login_lookup_client() -> httpx.Client:
    """Build the client for the login lookup; it never follows a redirect, so the token goes to one URL only."""
    return httpx.Client(timeout=LOGIN_LOOKUP_TIMEOUT_SECONDS, follow_redirects=False)


def _github_account_claims(token_data: Mapping[str, Any]) -> dict[str, Any]:
    """Read the account login with the token a code exchange just returned, as `{"login": ...}`.

    GitHub's token response names no account, so the connection would show as just "Connected". The provider runs
    this in a worker thread and is the one error boundary: a GitHub failure or an unsafe login raises here, the
    provider logs only the error type, never the token, and the account still connects without a label.
    Each phase of the request is bounded by `LOGIN_LOOKUP_TIMEOUT_SECONDS`.
    """
    token = token_data.get("token")
    if not isinstance(token, str) or not token:
        return {}
    with _login_lookup_client() as client:
        response = client.get(
            GITHUB_USER_API_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        response.raise_for_status()
        payload = response.json()
    login = payload.get("login") if isinstance(payload, dict) else None
    if not isinstance(login, str) or not _GITHUB_LOGIN.fullmatch(login):
        msg = "GitHub returned no usable login"
        raise ValueError(msg)
    return {"login": login}


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
        account_claims_reader=_github_account_claims,
    )
