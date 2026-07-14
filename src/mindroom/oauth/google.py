"""Shared Google OAuth provider helpers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, cast

import httpx
from google.auth import exceptions as google_auth_exceptions
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2 import id_token as google_id_token
from requests import exceptions as requests_exceptions

from mindroom.credentials import get_runtime_credentials_manager
from mindroom.logging_config import get_logger
from mindroom.oauth.providers import (
    RUNTIME_BOOTSTRAPPED_CLIENT_CONFIG_KEY,
    OAuthClaimValidationError,
    OAuthClientConfig,
    OAuthProvider,
    OAuthProviderError,
    OAuthRuntimeEndpoints,
    OAuthTokenResult,
    oauth_expires_at_from_response,
)

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)

_GOOGLE_AUTHORIZATION_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105
_GOOGLE_CLIENT_CONFIG_SERVICE = "google_oauth_client"
_GOOGLE_PROVISIONING_PATH = "/v1/local-mindroom/oauth/google-client"
GOOGLE_IDENTITY_SCOPES = (
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
)


def _google_runtime_endpoints() -> OAuthRuntimeEndpoints:
    """Return Google's fixed OAuth endpoints."""
    return OAuthRuntimeEndpoints(
        authorization_url=_GOOGLE_AUTHORIZATION_URL,
        token_url=_GOOGLE_TOKEN_URL,
    )


def _provisioning_client_credentials(runtime_paths: RuntimePaths) -> tuple[str, str, str] | None:
    """Return the paired provisioning endpoint and local client credentials."""
    provisioning_url = (runtime_paths.env_value("MINDROOM_PROVISIONING_URL") or "").strip().rstrip("/")
    client_id = (runtime_paths.env_value("MINDROOM_LOCAL_CLIENT_ID") or "").strip()
    client_secret = (runtime_paths.env_value("MINDROOM_LOCAL_CLIENT_SECRET") or "").strip()
    if not provisioning_url and not client_id and not client_secret:
        return None
    if not provisioning_url or not client_id or not client_secret:
        msg = (
            "Google OAuth bootstrap requires MINDROOM_PROVISIONING_URL, MINDROOM_LOCAL_CLIENT_ID, "
            "and MINDROOM_LOCAL_CLIENT_SECRET. Run `mindroom connect --pair-code ...` again."
        )
        raise OAuthProviderError(msg)
    return provisioning_url, client_id, client_secret


def _valid_provisioned_google_client(payload: object) -> tuple[str, str] | None:
    """Validate one provisioning response without accepting blank client fields."""
    if not isinstance(payload, dict):
        return None
    typed_payload = cast("dict[str, object]", payload)
    client_id = typed_payload.get("client_id")
    client_secret = typed_payload.get("client_secret")
    if not isinstance(client_id, str) or not client_id.strip():
        return None
    if not isinstance(client_secret, str) or not client_secret.strip():
        return None
    return client_id.strip(), client_secret.strip()


async def _google_runtime_bootstrapper(
    _provider: OAuthProvider,
    runtime_paths: RuntimePaths,
) -> OAuthRuntimeEndpoints:
    """Fetch the installed-app client config through an authenticated local pairing."""
    resolution = _provider.client_config_resolution(runtime_paths)
    if resolution is not None and resolution.custom:
        return _google_runtime_endpoints()

    manager = get_runtime_credentials_manager(runtime_paths)
    existing = manager.load_credentials(_GOOGLE_CLIENT_CONFIG_SERVICE)
    if existing and existing.get(RUNTIME_BOOTSTRAPPED_CLIENT_CONFIG_KEY) is not True:
        return _google_runtime_endpoints()

    provisioning_credentials = _provisioning_client_credentials(runtime_paths)
    if provisioning_credentials is None:
        if existing:
            return _google_runtime_endpoints()
        msg = (
            "Google OAuth is not configured. Pair this local install with `mindroom connect --pair-code ...`, "
            "or save a custom Google OAuth client in the dashboard."
        )
        raise OAuthProviderError(msg)

    provisioning_url, local_client_id, local_client_secret = provisioning_credentials
    headers = {
        "X-Local-MindRoom-Client-Id": local_client_id,
        "X-Local-MindRoom-Client-Secret": local_client_secret,
    }
    try:
        client_id, client_secret = await _fetch_provisioned_google_client(provisioning_url, headers)
    except OAuthProviderError as exc:
        if existing:
            logger.warning("google_oauth_client_bootstrap_unavailable", error_type=type(exc).__name__)
            return _google_runtime_endpoints()
        raise

    credentials = {
        "client_id": client_id,
        "client_secret": client_secret,
        RUNTIME_BOOTSTRAPPED_CLIENT_CONFIG_KEY: True,
    }
    if existing != credentials:
        manager.save_credentials(_GOOGLE_CLIENT_CONFIG_SERVICE, credentials)
    return _google_runtime_endpoints()


async def _fetch_provisioned_google_client(
    provisioning_url: str,
    headers: Mapping[str, str],
) -> tuple[str, str]:
    """Fetch and validate the desktop client from the provisioning service."""
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            response = await client.get(
                f"{provisioning_url}{_GOOGLE_PROVISIONING_PATH}",
                headers=headers,
            )
    except httpx.HTTPError as exc:
        msg = "Could not fetch the Google OAuth client configuration from the MindRoom provisioning service."
        raise OAuthProviderError(msg) from exc

    if not response.is_success:
        if response.status_code in {401, 403}:
            msg = "MindRoom pairing credentials are invalid or revoked. Run `mindroom connect --pair-code ...` again."
        elif response.status_code == 503:
            msg = "The MindRoom provisioning service has not configured the Google OAuth client yet."
        else:
            msg = f"MindRoom provisioning returned HTTP {response.status_code} for Google OAuth client bootstrap."
        raise OAuthProviderError(msg)

    try:
        provisioned_client = _valid_provisioned_google_client(response.json())
    except ValueError as exc:
        msg = "MindRoom provisioning returned invalid JSON for Google OAuth client bootstrap."
        raise OAuthProviderError(msg) from exc
    if provisioned_client is None:
        msg = "MindRoom provisioning returned an invalid Google OAuth client configuration."
        raise OAuthProviderError(msg)
    return provisioned_client


def _google_token_parser(
    provider: OAuthProvider,
    token_response: Mapping[str, Any],
    client_config: OAuthClientConfig,
    _runtime_paths: RuntimePaths,
) -> OAuthTokenResult:
    """Parse a Google OAuth token response into shared token data."""
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


def _google_domain_env_names(provider_id: str, suffix: str) -> tuple[str, ...]:
    """Return provider-specific environment variable names for Google domain settings."""
    prefix = provider_id.upper()
    return (f"{prefix}_{suffix}", f"MINDROOM_OAUTH_{prefix}_{suffix}")


def _google_oauth_provider(
    *,
    provider_id: str,
    display_name: str,
    scopes: tuple[str, ...],
    credential_service: str,
    tool_config_service: str,
    client_config_services: tuple[str, ...],
    status_capabilities: tuple[str, ...],
) -> OAuthProvider:
    """Return a Google OAuth provider with shared Google OAuth defaults."""
    return OAuthProvider(
        id=provider_id,
        display_name=display_name,
        authorization_url=_GOOGLE_AUTHORIZATION_URL,
        token_url=_GOOGLE_TOKEN_URL,
        scopes=scopes,
        credential_service=credential_service,
        tool_config_service=tool_config_service,
        client_config_services=client_config_services,
        shared_client_config_services=(_GOOGLE_CLIENT_CONFIG_SERVICE,),
        allowed_email_domains_env=_google_domain_env_names(provider_id, "ALLOWED_EMAIL_DOMAINS"),
        allowed_hosted_domains_env=_google_domain_env_names(provider_id, "ALLOWED_HOSTED_DOMAINS"),
        extra_auth_params={
            "access_type": "offline",
            "include_granted_scopes": "true",
            "prompt": "consent",
        },
        pkce_code_challenge_method="S256",
        runtime_bootstrapper=_google_runtime_bootstrapper,
        status_capabilities=status_capabilities,
        token_parser=_google_token_parser,
    )
