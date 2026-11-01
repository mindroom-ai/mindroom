"""Built-in Google OAuth providers and their shared helpers."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from functools import partial
from typing import TYPE_CHECKING, Any, cast

import httpx

from mindroom.background_tasks import run_blocking_until_complete
from mindroom.credentials.manager import get_runtime_credentials_manager
from mindroom.logging_config import get_logger
from mindroom.matrix.provisioning_env import (
    local_client_headers,
    local_provisioning_client_credentials_from_env,
    provisioning_url_from_env,
)
from mindroom.oauth.providers import (
    RUNTIME_BOOTSTRAPPED_CLIENT_CONFIG_KEY,
    OAuthClaimValidationError,
    OAuthClientConfig,
    OAuthProvider,
    OAuthProviderError,
    OAuthRuntimeEndpoints,
    OAuthTokenResult,
    is_oauth_loopback_hostname,
    oauth_expires_at_from_response,
)

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)

_GOOGLE_AUTHORIZATION_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105
_GOOGLE_CLIENT_CONFIG_SERVICE = "google_oauth_client"
_GOOGLE_PROVISIONING_PATH = "/v1/local-mindroom/oauth/google-client"
_GOOGLE_PROVISIONED_CLIENT_FETCHED_AT_KEY = "_oauth_client_runtime_bootstrap_fetched_at"
_GOOGLE_PROVISIONED_CLIENT_TTL_SECONDS = 60 * 60
_GOOGLE_IDENTITY_VERIFICATION_TIMEOUT_SECONDS = 20.0
_GOOGLE_IDENTITY_SCOPES = (
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
)
GOOGLE_DRIVE_READ_OAUTH_SCOPES = (
    *_GOOGLE_IDENTITY_SCOPES,
    "https://www.googleapis.com/auth/drive.readonly",
)
GOOGLE_DRIVE_WRITE_SCOPE = "https://www.googleapis.com/auth/drive"


@dataclass(frozen=True, slots=True)
class _GoogleProviderSpec:
    """What sets one built-in Google provider apart; every other field comes from the shared Google defaults."""

    display_name: str
    tool_config_service: str | None
    # Requested after `_GOOGLE_IDENTITY_SCOPES`.
    service_scopes: tuple[str, ...]
    status_capabilities: tuple[str, ...]
    include_granted_scopes: bool = True


_GOOGLE_PROVIDERS = {
    "google_calendar": _GoogleProviderSpec(
        display_name="Google Calendar",
        tool_config_service="google_calendar",
        service_scopes=(
            "https://www.googleapis.com/auth/calendar.events",
            "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
            "https://www.googleapis.com/auth/calendar.freebusy",
            "https://www.googleapis.com/auth/calendar.settings.readonly",
        ),
        status_capabilities=("Calendar event read/write",),
    ),
    "google_cloud": _GoogleProviderSpec(
        display_name="Google Cloud",
        # Several tools share this connection; each keeps settings under its own tool name.
        tool_config_service=None,
        service_scopes=("https://www.googleapis.com/auth/cloud-platform.read-only",),
        status_capabilities=("Read-only Google Cloud API access",),
        include_granted_scopes=False,
    ),
    "google_docs": _GoogleProviderSpec(
        display_name="Google Docs",
        tool_config_service="google_docs",
        service_scopes=("https://www.googleapis.com/auth/documents",),
        status_capabilities=("Docs create and read", "Docs text editing"),
        include_granted_scopes=False,
    ),
    "google_drive": _GoogleProviderSpec(
        display_name="Google Drive",
        tool_config_service="google_drive",
        service_scopes=(GOOGLE_DRIVE_WRITE_SCOPE,),
        status_capabilities=("Drive file search and read", "Drive file upload and organization"),
    ),
    "google_gmail": _GoogleProviderSpec(
        display_name="Gmail",
        tool_config_service="gmail",
        service_scopes=("https://www.googleapis.com/auth/gmail.modify",),
        status_capabilities=("Gmail read, send, draft, and mailbox management",),
    ),
    "google_sheets": _GoogleProviderSpec(
        display_name="Google Sheets",
        tool_config_service="google_sheets",
        service_scopes=("https://www.googleapis.com/auth/spreadsheets",),
        status_capabilities=("Sheets read/write",),
    ),
    "google_tasks": _GoogleProviderSpec(
        display_name="Google Tasks",
        tool_config_service="google_tasks",
        service_scopes=("https://www.googleapis.com/auth/tasks",),
        status_capabilities=("Tasks read/write",),
        include_granted_scopes=False,
    ),
}


def _google_runtime_endpoints() -> OAuthRuntimeEndpoints:
    """Return Google's fixed OAuth endpoints."""
    return OAuthRuntimeEndpoints(
        authorization_url=_GOOGLE_AUTHORIZATION_URL,
        token_url=_GOOGLE_TOKEN_URL,
    )


def _provisioning_client_credentials(runtime_paths: RuntimePaths) -> tuple[str, str, str] | None:
    """Return the paired provisioning endpoint and local client credentials, read as Matrix registration reads them."""
    try:
        credentials = local_provisioning_client_credentials_from_env(runtime_paths)
    except ValueError as exc:
        raise OAuthProviderError(str(exc)) from exc
    if credentials is None:
        return None
    provisioning_url = provisioning_url_from_env(runtime_paths)
    if provisioning_url is None:
        msg = "Google OAuth bootstrap requires MINDROOM_PROVISIONING_URL. Run `mindroom connect` to restore pairing."
        raise OAuthProviderError(msg)
    parsed_url = httpx.URL(provisioning_url)
    if parsed_url.scheme != "https" and not (
        parsed_url.scheme == "http" and is_oauth_loopback_hostname(parsed_url.host)
    ):
        msg = "MINDROOM_PROVISIONING_URL must use HTTPS, except for localhost development."
        raise OAuthProviderError(msg)
    return provisioning_url, *credentials


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


def _provisioned_google_client_is_fresh(credentials: Mapping[str, object] | None) -> bool:
    """Return whether cached provisioned credentials are complete and recent."""
    if not credentials or credentials.get(RUNTIME_BOOTSTRAPPED_CLIENT_CONFIG_KEY) is not True:
        return False
    if _valid_provisioned_google_client(credentials) is None:
        return False
    fetched_at = credentials.get(_GOOGLE_PROVISIONED_CLIENT_FETCHED_AT_KEY)
    if isinstance(fetched_at, bool) or not isinstance(fetched_at, int | float):
        return False
    age = time.time() - fetched_at
    return 0 <= age < _GOOGLE_PROVISIONED_CLIENT_TTL_SECONDS


async def _google_runtime_bootstrapper(
    provider: OAuthProvider,
    runtime_paths: RuntimePaths,
) -> OAuthRuntimeEndpoints:
    """Fetch the installed-app client config through an authenticated local pairing."""
    resolution = await asyncio.to_thread(provider.client_config_resolution, runtime_paths)
    if resolution is not None and resolution.custom:
        return _google_runtime_endpoints()

    manager = await asyncio.to_thread(get_runtime_credentials_manager, runtime_paths)
    existing = await asyncio.to_thread(manager.load_credentials, _GOOGLE_CLIENT_CONFIG_SERVICE)
    if existing and existing.get(RUNTIME_BOOTSTRAPPED_CLIENT_CONFIG_KEY) is not True:
        return _google_runtime_endpoints()
    if _provisioned_google_client_is_fresh(existing):
        return _google_runtime_endpoints()

    provisioning_credentials = _provisioning_client_credentials(runtime_paths)
    if provisioning_credentials is None:
        # Unpaired: nothing to fetch, so only a client already stored can serve.
        return _google_runtime_endpoints()

    provisioning_url, local_client_id, local_client_secret = provisioning_credentials
    headers = local_client_headers(local_client_id, local_client_secret)
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
        _GOOGLE_PROVISIONED_CLIENT_FETCHED_AT_KEY: time.time(),
    }
    if existing != credentials:
        await run_blocking_until_complete(manager.save_credentials, _GOOGLE_CLIENT_CONFIG_SERVICE, credentials)
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
            msg = "MindRoom pairing credentials are invalid or revoked. Run `mindroom connect` again."
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
        # google-auth and requests load only when a Google sign-in completes.
        from google.auth import exceptions as google_auth_exceptions  # noqa: PLC0415
        from google.auth.transport.requests import Request as GoogleRequest  # noqa: PLC0415
        from google.oauth2 import id_token as google_id_token  # noqa: PLC0415
        from requests import exceptions as requests_exceptions  # noqa: PLC0415

        try:
            claims = google_id_token.verify_oauth2_token(
                id_token,
                partial(GoogleRequest(), timeout=_GOOGLE_IDENTITY_VERIFICATION_TIMEOUT_SECONDS),
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

    token_data: dict[str, Any] = {
        "token": access_token,
        "client_id": client_config.client_id,
        "_source": "oauth",
        "_oauth_provider": provider.id,
    }
    response_scope = token_response.get("scope")
    if isinstance(response_scope, str) and response_scope.strip():
        token_data["scopes"] = response_scope.split()
    if isinstance(refresh_token, str) and refresh_token:
        token_data["refresh_token"] = refresh_token
    token_type = token_response.get("token_type")
    if isinstance(token_type, str) and token_type:
        token_data["token_type"] = token_type
    expires_at = oauth_expires_at_from_response(token_response)
    if expires_at is not None:
        token_data["expires_at"] = expires_at

    return OAuthTokenResult(token_data=token_data, claims=claims, claims_verified=True)


def with_google_account_picker(provider: OAuthProvider) -> OAuthProvider:
    """Return `provider` with Google's account picker before consent; any other provider is returned as it is.

    A browser signed in to one Google account otherwise goes straight to consent for it, so a connection meant for
    another account, such as a service's own beside the tools' one, would connect the same account again.
    """
    if provider.authorization_url != _GOOGLE_AUTHORIZATION_URL:
        return provider
    return replace(provider, extra_auth_params={**provider.extra_auth_params, "prompt": "consent select_account"})


def _google_domain_env_names(provider_id: str, suffix: str) -> tuple[str, ...]:
    """Return provider-specific environment variable names for Google domain settings."""
    prefix = provider_id.upper()
    return (f"{prefix}_{suffix}", f"MINDROOM_OAUTH_{prefix}_{suffix}")


def google_oauth_provider(provider_id: str) -> OAuthProvider:
    """Return the built-in Google provider `provider_id` with the shared Google OAuth defaults."""
    spec = _GOOGLE_PROVIDERS[provider_id]
    extra_auth_params = {
        "access_type": "offline",
        "prompt": "consent",
    }
    if spec.include_granted_scopes:
        extra_auth_params["include_granted_scopes"] = "true"
    return OAuthProvider(
        id=provider_id,
        display_name=spec.display_name,
        authorization_url=_GOOGLE_AUTHORIZATION_URL,
        token_url=_GOOGLE_TOKEN_URL,
        scopes=(*_GOOGLE_IDENTITY_SCOPES, *spec.service_scopes),
        credential_service=f"{provider_id}_oauth",
        tool_config_service=spec.tool_config_service,
        client_config_services=(f"{provider_id}_oauth_client",),
        shared_client_config_services=(_GOOGLE_CLIENT_CONFIG_SERVICE,),
        allowed_email_domains_env=_google_domain_env_names(provider_id, "ALLOWED_EMAIL_DOMAINS"),
        allowed_hosted_domains_env=_google_domain_env_names(provider_id, "ALLOWED_HOSTED_DOMAINS"),
        extra_auth_params=extra_auth_params,
        pkce_code_challenge_method="S256",
        runtime_bootstrapper=_google_runtime_bootstrapper,
        status_capabilities=spec.status_capabilities,
        token_parser=_google_token_parser,
    )


def google_oauth_providers() -> tuple[OAuthProvider, ...]:
    """Return every built-in Google provider."""
    return tuple(google_oauth_provider(provider_id) for provider_id in _GOOGLE_PROVIDERS)


def google_cloud_oauth_provider() -> OAuthProvider:
    """Return the shared read-only connection used by every Google Cloud tool."""
    return google_oauth_provider("google_cloud")
