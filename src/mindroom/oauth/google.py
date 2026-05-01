"""Shared Google OAuth provider helpers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from google.auth import exceptions as google_auth_exceptions
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2 import id_token as google_id_token
from requests import exceptions as requests_exceptions

from mindroom.logging_config import get_logger
from mindroom.oauth.providers import (
    OAuthClaimValidationError,
    OAuthClientConfig,
    OAuthProvider,
    OAuthTokenResult,
    oauth_expires_at_from_response,
)

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)

GOOGLE_IDENTITY_SCOPES = (
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
)


def _google_token_parser(
    provider: OAuthProvider,
    token_response: Mapping[str, Any],
    client_config: OAuthClientConfig,
    _runtime_paths: RuntimePaths,
) -> OAuthTokenResult:
    access_token = token_response.get("access_token")
    refresh_token = token_response.get("refresh_token")
    id_token = token_response.get("id_token")
    if not isinstance(access_token, str) or not access_token:
        msg = "Google did not return an access token"
        raise OAuthClaimValidationError(msg)

    existing_claims = token_response.get("_oauth_claims")
    existing_claims_verified = token_response.get("_oauth_claims_verified") is True
    if (
        (not isinstance(id_token, str) or not id_token)
        and isinstance(existing_claims, Mapping)
        and existing_claims_verified
    ):
        claims = dict(existing_claims)
    elif not isinstance(id_token, str) or not id_token:
        msg = "Google did not return a verifiable identity token"
        raise OAuthClaimValidationError(msg)
    else:
        try:
            claims = google_id_token.verify_oauth2_token(
                id_token,
                GoogleRequest(),
                client_config.client_id,
            )
        except (ValueError, google_auth_exceptions.GoogleAuthError, requests_exceptions.RequestException) as exc:
            logger.warning(
                "google_id_token_verification_failed",
                provider_id=provider.id,
                error_type=type(exc).__name__,
            )
            msg = "Google identity token verification failed"
            raise OAuthClaimValidationError(msg) from exc
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
        "_source": "oauth",
        "_oauth_provider": provider.id,
    }
    if isinstance(refresh_token, str) and refresh_token:
        token_data["refresh_token"] = refresh_token
    token_type = token_response.get("token_type")
    if isinstance(token_type, str) and token_type:
        token_data["token_type"] = token_type
    expires_at = oauth_expires_at_from_response(token_response)
    if expires_at is not None:
        token_data["expires_at"] = expires_at

    return OAuthTokenResult(token_data=token_data, claims=claims, claims_verified=True)


def _google_provider_env_names(provider_id: str, legacy_google: bool = True) -> tuple[tuple[str, ...], tuple[str, ...]]:
    prefix = provider_id.upper()
    client_id_env = (
        f"{prefix}_CLIENT_ID",
        f"MINDROOM_OAUTH_{prefix}_CLIENT_ID",
        *(("GOOGLE_CLIENT_ID",) if legacy_google else ()),
    )
    client_secret_env = (
        f"{prefix}_CLIENT_SECRET",
        f"MINDROOM_OAUTH_{prefix}_CLIENT_SECRET",
        *(("GOOGLE_CLIENT_SECRET",) if legacy_google else ()),
    )
    return client_id_env, client_secret_env


def _google_redirect_env_names(provider_id: str) -> tuple[str, ...]:
    prefix = provider_id.upper()
    return (f"{prefix}_REDIRECT_URI", f"MINDROOM_OAUTH_{prefix}_REDIRECT_URI")


def _google_client_config_services(provider_id: str) -> tuple[str, ...]:
    return (f"{provider_id}_oauth_client",)


def _google_shared_client_config_services() -> tuple[str, ...]:
    return ("google_oauth_client",)


def _google_domain_env_names(provider_id: str, suffix: str) -> tuple[str, ...]:
    prefix = provider_id.upper()
    return (f"{prefix}_{suffix}", f"MINDROOM_OAUTH_{prefix}_{suffix}")
