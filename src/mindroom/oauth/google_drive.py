"""Built-in Google Drive OAuth provider."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from mindroom.oauth.providers import (
    OAuthClaimValidationError,
    OAuthClientConfig,
    OAuthProvider,
    OAuthTokenResult,
)
from mindroom.tool_system.dependencies import ensure_tool_deps

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.constants import RuntimePaths

_GOOGLE_ID_TOKEN_DEPS = ["google-auth"]
GOOGLE_DRIVE_OAUTH_SCOPES = (
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/drive.readonly",
)


def _google_drive_token_parser(
    provider: OAuthProvider,
    token_response: Mapping[str, Any],
    client_config: OAuthClientConfig,
    runtime_paths: RuntimePaths,
) -> OAuthTokenResult:
    access_token = token_response.get("access_token")
    refresh_token = token_response.get("refresh_token")
    id_token = token_response.get("id_token")
    if not isinstance(access_token, str) or not access_token:
        msg = "Google did not return an access token"
        raise OAuthClaimValidationError(msg)
    if not isinstance(id_token, str) or not id_token:
        msg = "Google did not return a verifiable identity token"
        raise OAuthClaimValidationError(msg)

    ensure_tool_deps(_GOOGLE_ID_TOKEN_DEPS, "google_drive", runtime_paths)
    from google.auth.transport.requests import Request as GoogleRequest  # noqa: PLC0415
    from google.oauth2 import id_token as google_id_token  # noqa: PLC0415

    claims = google_id_token.verify_oauth2_token(
        id_token,
        GoogleRequest(),
        client_config.client_id,
    )
    if not isinstance(claims, dict):
        msg = "Google identity token verification did not return claims"
        raise OAuthClaimValidationError(msg)

    scopes = provider.scopes
    response_scope = token_response.get("scope")
    if isinstance(response_scope, str) and response_scope.strip():
        scopes = tuple(response_scope.split())

    token_data: dict[str, Any] = {
        "token": access_token,
        "token_uri": provider.token_url,
        "client_id": client_config.client_id,
        "scopes": list(scopes),
        "_id_token": id_token,
        "_source": "oauth",
        "_oauth_provider": provider.id,
    }
    if isinstance(refresh_token, str) and refresh_token:
        token_data["refresh_token"] = refresh_token
    token_type = token_response.get("token_type")
    if isinstance(token_type, str) and token_type:
        token_data["token_type"] = token_type
    expires_in = token_response.get("expires_in")
    if isinstance(expires_in, int | float) and expires_in > 0:
        token_data["expires_at"] = time.time() + float(expires_in)

    return OAuthTokenResult(token_data=token_data, claims=claims, claims_verified=True)


def google_drive_oauth_provider() -> OAuthProvider:
    """Return the built-in Google Drive provider definition."""
    return OAuthProvider(
        id="google_drive",
        display_name="Google Drive",
        authorization_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",  # noqa: S106
        scopes=GOOGLE_DRIVE_OAUTH_SCOPES,
        credential_service="google_drive",
        client_id_env=(
            "GOOGLE_DRIVE_CLIENT_ID",
            "MINDROOM_OAUTH_GOOGLE_DRIVE_CLIENT_ID",
            "GOOGLE_CLIENT_ID",
        ),
        client_secret_env=(
            "GOOGLE_DRIVE_CLIENT_SECRET",
            "MINDROOM_OAUTH_GOOGLE_DRIVE_CLIENT_SECRET",
            "GOOGLE_CLIENT_SECRET",
        ),
        redirect_uri_env=(
            "GOOGLE_DRIVE_REDIRECT_URI",
            "MINDROOM_OAUTH_GOOGLE_DRIVE_REDIRECT_URI",
        ),
        allowed_email_domains_env=(
            "GOOGLE_DRIVE_ALLOWED_EMAIL_DOMAINS",
            "MINDROOM_OAUTH_GOOGLE_DRIVE_ALLOWED_EMAIL_DOMAINS",
        ),
        allowed_hosted_domains_env=(
            "GOOGLE_DRIVE_ALLOWED_HOSTED_DOMAINS",
            "MINDROOM_OAUTH_GOOGLE_DRIVE_ALLOWED_HOSTED_DOMAINS",
        ),
        extra_auth_params={
            "access_type": "offline",
            "include_granted_scopes": "true",
            "prompt": "consent",
        },
        status_capabilities=(
            "Drive file search",
            "Drive file read",
        ),
        token_parser=_google_drive_token_parser,
    )
