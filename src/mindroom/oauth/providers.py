"""Provider contracts for MindRoom-managed OAuth flows."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import secrets
import time
import warnings
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, cast

from authlib.common.errors import AuthlibBaseError
from authlib.deprecate import AuthlibDeprecationWarning
from httpx import HTTPError, HTTPStatusError

from mindroom.credential_policy import is_oauth_client_config_service
from mindroom.credentials import get_runtime_credentials_manager, validate_service_name

warnings.filterwarnings(
    "ignore",
    category=AuthlibDeprecationWarning,
    module="authlib._joserfc_helpers",
)
from authlib.integrations.httpx_client import AsyncOAuth2Client  # noqa: E402
from authlib.integrations.requests_client import OAuth2Session  # noqa: E402

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths

_PKCECodeChallengeMethod = Literal["S256"]
_TokenEndpointAuthMethod = Literal["none", "client_secret_post", "client_secret_basic"]
_DEFAULT_AUTHORIZE_TIMEOUT_SECONDS = 20.0
_DEFAULT_REFRESH_SKEW_SECONDS = 60.0
_DEFAULT_TOKEN_ENDPOINT_AUTH_METHOD: _TokenEndpointAuthMethod = "client_secret_post"  # noqa: S105
_PUBLIC_TOKEN_ENDPOINT_AUTH_METHOD: _TokenEndpointAuthMethod = "none"  # noqa: S105
_SUPPORTED_TOKEN_ENDPOINT_AUTH_METHODS = frozenset(
    {_PUBLIC_TOKEN_ENDPOINT_AUTH_METHOD, "client_secret_post", "client_secret_basic"},
)
_SUPPORTED_PKCE_CODE_CHALLENGE_METHODS = frozenset({None, "S256"})


class OAuthProviderError(RuntimeError):
    """Base error for provider configuration and OAuth flow failures."""


class OAuthRefreshRejectedError(OAuthProviderError):
    """Raised when a provider rejects a refresh-token grant."""


class _OAuthProviderNotConfiguredError(OAuthProviderError):
    """Raised when a provider has no usable OAuth client configuration."""


class OAuthClaimValidationError(OAuthProviderError):
    """Raised when verified provider claims do not satisfy configured policy."""


class OAuthConnectionRequired(OAuthProviderError):  # noqa: N818
    """Raised by tools when a user must connect an OAuth provider."""

    def __init__(
        self,
        message: str,
        *,
        provider_id: str | None = None,
        connect_url: str | None = None,
        reason: str | None = None,
    ) -> None:
        super().__init__(message)
        self.provider_id = provider_id
        self.connect_url = connect_url
        self.reason = reason


def oauth_connection_required_payload(exc: OAuthConnectionRequired) -> dict[str, object]:
    """Return the structured tool payload for one OAuth connection prompt."""
    payload: dict[str, object] = {
        "error": str(exc),
        "oauth_connection_required": True,
        "provider": exc.provider_id,
        "connect_url": exc.connect_url,
    }
    if exc.reason is not None:
        payload["reason"] = exc.reason
    return payload


@dataclass(frozen=True, slots=True)
class OAuthClientConfig:
    """Resolved OAuth client settings for one runtime."""

    client_id: str
    client_secret: str | None
    redirect_uri: str


@dataclass(frozen=True, slots=True)
class OAuthRuntimeEndpoints:
    """OAuth endpoints resolved for one runtime."""

    authorization_url: str
    token_url: str
    token_endpoint_auth_method: _TokenEndpointAuthMethod | None = None


@dataclass(frozen=True, slots=True)
class _OAuthClientConfigResolution:
    """Resolved OAuth client settings plus the credential service that supplied them."""

    config: OAuthClientConfig
    service: str


@dataclass(frozen=True, slots=True)
class OAuthTokenResult:
    """Normalized token payload plus optional verified identity claims."""

    token_data: dict[str, Any]
    claims: dict[str, Any] = field(default_factory=dict)
    claims_verified: bool = False


@dataclass(frozen=True, slots=True)
class _OAuthClaimValidationContext:
    """Inputs passed to a provider-specific claim validator."""

    provider_id: str
    token_data: Mapping[str, Any]
    claims: Mapping[str, Any]
    claims_verified: bool
    runtime_paths: RuntimePaths


_OAuthTokenParser = Callable[["OAuthProvider", Mapping[str, Any], OAuthClientConfig, "RuntimePaths"], OAuthTokenResult]
_OAuthTokenExchanger = Callable[
    ["OAuthProvider", str, OAuthClientConfig, "RuntimePaths", str | None],
    OAuthTokenResult | Awaitable[OAuthTokenResult],
]
_OAuthClaimValidator = Callable[[_OAuthClaimValidationContext], None]
_OAuthRuntimeBootstrapper = Callable[["OAuthProvider", "RuntimePaths"], Awaitable[OAuthRuntimeEndpoints]]


def _normalize_env_names(names: str | Sequence[str] | None) -> tuple[str, ...]:
    if names is None:
        return ()
    if isinstance(names, str):
        return (names,)
    return tuple(name for name in names if name)


def _split_csv(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(part.strip().lower() for part in value.split(",") if part.strip())


def _runtime_env_value(runtime_paths: RuntimePaths, names: tuple[str, ...]) -> str | None:
    for name in names:
        value = runtime_paths.env_value(name)
        if value:
            return value.strip()
    return None


def _runtime_port(runtime_paths: RuntimePaths) -> str:
    return runtime_paths.env_value("MINDROOM_PORT", default="8765") or "8765"


def _decode_jwt_claims_unverified(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
        claims = json.loads(decoded.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return claims if isinstance(claims, dict) else {}


def _default_token_parser(
    provider: OAuthProvider,
    token_response: Mapping[str, Any],
    client_config: OAuthClientConfig,
    runtime_paths: RuntimePaths,
) -> OAuthTokenResult:
    del runtime_paths
    access_token = token_response.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        msg = "OAuth provider did not return an access token"
        raise OAuthProviderError(msg)

    scopes = provider.scopes
    response_scope = token_response.get("scope")
    if isinstance(response_scope, str) and response_scope.strip():
        scopes = tuple(response_scope.split())

    token_data: dict[str, Any] = {
        "token": access_token,
        "token_uri": token_response.get("_mindroom_token_url")
        if isinstance(token_response.get("_mindroom_token_url"), str)
        else provider.token_url,
        "client_id": client_config.client_id,
        "scopes": list(scopes),
        "_source": "oauth",
        "_oauth_provider": provider.id,
    }
    refresh_token = token_response.get("refresh_token")
    if isinstance(refresh_token, str) and refresh_token:
        token_data["refresh_token"] = refresh_token
    token_type = token_response.get("token_type")
    if isinstance(token_type, str) and token_type:
        token_data["token_type"] = token_type
    expires_at = oauth_expires_at_from_response(token_response)
    if expires_at is not None:
        token_data["expires_at"] = expires_at

    id_token = token_response.get("id_token")
    claims: dict[str, Any] = {}
    if isinstance(id_token, str) and id_token:
        token_data["_id_token"] = id_token
        claims = _decode_jwt_claims_unverified(id_token)

    return OAuthTokenResult(token_data=token_data, claims=claims, claims_verified=False)


def _token_result_with_core_metadata(
    provider: OAuthProvider,
    result: OAuthTokenResult,
    *,
    client_id: str | None = None,
) -> OAuthTokenResult:
    token_data = dict(result.token_data)
    if client_id is not None:
        token_data["client_id"] = client_id
    token_data["_source"] = "oauth"
    token_data["_oauth_provider"] = provider.id
    if not isinstance(token_data.get("scopes"), list):
        token_data["scopes"] = list(provider.scopes)
    return OAuthTokenResult(
        token_data=token_data,
        claims=dict(result.claims),
        claims_verified=result.claims_verified,
    )


def _verified_claims_for_storage(claims: Mapping[str, Any]) -> dict[str, Any]:
    """Return verified claims needed for later identity-policy checks."""
    return dict(claims)


def _claim_email_domain(claims: Mapping[str, Any]) -> str | None:
    email = claims.get("email")
    if not isinstance(email, str) or "@" not in email:
        return None
    return email.rsplit("@", 1)[-1].lower()


def oauth_expires_at_from_response(token_response: Mapping[str, Any]) -> float | None:
    """Return an absolute expiry timestamp from a provider or OAuth client token response."""
    expires_at = token_response.get("expires_at")
    if isinstance(expires_at, int | float) and expires_at > 0:
        return float(expires_at)
    expires_in = token_response.get("expires_in")
    if isinstance(expires_in, int | float) and expires_in > 0:
        return time.time() + float(expires_in)
    return None


def _token_data_needs_refresh(
    token_data: Mapping[str, Any],
    *,
    now: float | None = None,
) -> bool:
    refresh_token = token_data.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        return False
    token = token_data.get("token") or token_data.get("access_token")
    if not isinstance(token, str) or not token:
        return True
    expires_at = token_data.get("expires_at")
    if isinstance(expires_at, bool) or not isinstance(expires_at, int | float) or not math.isfinite(expires_at):
        return False
    return float(expires_at) <= (now if now is not None else time.time()) + _DEFAULT_REFRESH_SKEW_SECONDS


def _oauth_error_fields(error: object, description: object) -> tuple[str | None, str | None]:
    """Return safe OAuth error code and detail from standard non-secret response fields."""
    error_code = error.strip() if isinstance(error, str) and error.strip() else None
    parts = [value.strip() for value in (error, description) if isinstance(value, str) and value.strip()]
    return error_code, ": ".join(parts) if parts else None


def _http_status_oauth_error_fields(exc: HTTPStatusError) -> tuple[str | None, str | None]:
    """Return OAuth error detail from an HTTP error response body, if present."""
    try:
        payload = exc.response.json()
    except (ValueError, UnicodeDecodeError):
        return None, None
    if not isinstance(payload, Mapping):
        return None, None
    return _oauth_error_fields(payload.get("error"), payload.get("error_description"))


def _oauth_refresh_error(exc: AuthlibBaseError | HTTPError) -> OAuthProviderError:
    """Build a safe refresh failure with provider OAuth reason fields when available."""
    error_code: str | None = None
    detail: str | None = None
    if isinstance(exc, AuthlibBaseError):
        error_code, detail = _oauth_error_fields(exc.error, exc.description)
    elif isinstance(exc, HTTPStatusError):
        error_code, detail = _http_status_oauth_error_fields(exc)

    msg = "OAuth token refresh failed"
    if detail is not None:
        if error_code == "invalid_grant":
            return OAuthRefreshRejectedError(f"{msg}: {detail}")
        return OAuthProviderError(f"{msg}: {detail}")
    if error_code == "invalid_grant":
        return OAuthRefreshRejectedError(f"{msg}: {error_code}")
    return OAuthProviderError(msg)


def _generate_pkce_code_verifier() -> str:
    """Return one high-entropy PKCE verifier."""
    return secrets.token_urlsafe(64)


def _pkce_s256_code_challenge(code_verifier: str) -> str:
    """Return the RFC 7636 S256 challenge for one verifier."""
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


@dataclass(frozen=True, slots=True)
class OAuthProvider:
    """Provider definition registered by core or a plugin."""

    id: str
    display_name: str
    authorization_url: str
    token_url: str
    scopes: tuple[str, ...]
    credential_service: str
    tool_config_service: str | None = None
    client_config_services: tuple[str, ...] = ()
    shared_client_config_services: tuple[str, ...] = ()
    default_redirect_path: str | None = None
    extra_auth_params: Mapping[str, str] = field(default_factory=dict)
    extra_token_params: Mapping[str, str] = field(default_factory=dict)
    token_endpoint_auth_method: _TokenEndpointAuthMethod = _DEFAULT_TOKEN_ENDPOINT_AUTH_METHOD
    pkce_code_challenge_method: _PKCECodeChallengeMethod | None = None
    allow_empty_scopes: bool = False
    allowed_email_domains: tuple[str, ...] = ()
    allowed_hosted_domains: tuple[str, ...] = ()
    allowed_email_domains_env: str | Sequence[str] | None = None
    allowed_hosted_domains_env: str | Sequence[str] | None = None
    status_capabilities: tuple[str, ...] = ()
    token_parser: _OAuthTokenParser | None = None
    token_exchanger: _OAuthTokenExchanger | None = None
    claim_validator: _OAuthClaimValidator | None = None
    runtime_bootstrapper: _OAuthRuntimeBootstrapper | None = None

    def __post_init__(self) -> None:
        """Validate provider identifiers and redirect path shape."""
        validate_service_name(self.id)
        validate_service_name(self.credential_service)
        if is_oauth_client_config_service(self.credential_service):
            msg = (
                f"OAuth provider '{self.id}' credential_service '{self.credential_service}' "
                "must not end with '_oauth_client'"
            )
            raise ValueError(msg)
        if self.tool_config_service is not None:
            validate_service_name(self.tool_config_service)
            if is_oauth_client_config_service(self.tool_config_service):
                msg = (
                    f"OAuth provider '{self.id}' tool_config_service '{self.tool_config_service}' "
                    "must not end with '_oauth_client'"
                )
                raise ValueError(msg)
        self._validate_client_config_services()
        self._validate_provider_options()
        self._validate_redirect_path()

    def _validate_client_config_services(self) -> None:
        """Validate OAuth client config service names."""
        for service in self.all_client_config_services:
            validate_service_name(service)
            if not is_oauth_client_config_service(service):
                msg = f"OAuth provider '{self.id}' client config service '{service}' must end with '_oauth_client'"
                raise ValueError(msg)
        if not self.all_client_config_services:
            msg = f"OAuth provider '{self.id}' must declare at least one client config service"
            raise ValueError(msg)

    def _validate_provider_options(self) -> None:
        """Validate provider OAuth options."""
        if not self.scopes and not self.allow_empty_scopes:
            msg = f"OAuth provider '{self.id}' must declare at least one scope"
            raise ValueError(msg)
        if self.token_endpoint_auth_method not in _SUPPORTED_TOKEN_ENDPOINT_AUTH_METHODS:
            msg = f"OAuth provider '{self.id}' has unsupported token endpoint auth method"
            raise ValueError(msg)
        if self.pkce_code_challenge_method not in _SUPPORTED_PKCE_CODE_CHALLENGE_METHODS:
            msg = f"OAuth provider '{self.id}' supports only S256 PKCE"
            raise ValueError(msg)

    def _validate_redirect_path(self) -> None:
        """Validate provider callback path shape."""
        redirect_path = self.redirect_path
        if not redirect_path.startswith("/"):
            msg = f"OAuth provider '{self.id}' default_redirect_path must start with '/'"
            raise ValueError(msg)

    @property
    def all_client_config_services(self) -> tuple[str, ...]:
        """Return provider-specific then shared OAuth client config services."""
        return (*self.client_config_services, *self.shared_client_config_services)

    @property
    def redirect_path(self) -> str:
        """Return the relative MindRoom callback path for this provider."""
        return self.default_redirect_path or f"/api/oauth/{self.id}/callback"

    def client_config(self, runtime_paths: RuntimePaths) -> OAuthClientConfig | None:
        """Return resolved client settings or None when the provider is not configured."""
        resolution = self.client_config_resolution(runtime_paths)
        return resolution.config if resolution is not None else None

    def client_config_resolution(self, runtime_paths: RuntimePaths) -> _OAuthClientConfigResolution | None:
        """Return stored OAuth app client settings and the supplying credential service."""
        manager = get_runtime_credentials_manager(runtime_paths)
        for service in self.client_config_services:
            config = self._stored_client_config_from_service(runtime_paths, manager.load_credentials(service), True)
            if config is not None:
                return _OAuthClientConfigResolution(config=config, service=service)
        for service in self.shared_client_config_services:
            config = self._stored_client_config_from_service(runtime_paths, manager.load_credentials(service), False)
            if config is not None:
                return _OAuthClientConfigResolution(config=config, service=service)
        return None

    async def client_config_resolution_async(
        self,
        runtime_paths: RuntimePaths,
    ) -> _OAuthClientConfigResolution | None:
        """Return stored client settings, after any lazy runtime bootstrap."""
        resolution = self.client_config_resolution(runtime_paths)
        if resolution is not None:
            return resolution
        if self.runtime_bootstrapper is None:
            return None
        await self.runtime_endpoints(runtime_paths)
        return self.client_config_resolution(runtime_paths)

    def _stored_client_config_from_service(
        self,
        runtime_paths: RuntimePaths,
        credentials: Mapping[str, Any] | None,
        use_stored_redirect_uri: bool,
    ) -> OAuthClientConfig | None:
        """Return stored OAuth app client settings from one credential document."""
        if not credentials:
            return None
        client_id = credentials.get("client_id")
        client_secret = credentials.get("client_secret")
        if not isinstance(client_id, str) or not client_id.strip():
            return None
        if self.token_endpoint_auth_method != _PUBLIC_TOKEN_ENDPOINT_AUTH_METHOD and (
            not isinstance(client_secret, str) or not client_secret.strip()
        ):
            return None
        redirect_uri = credentials.get("redirect_uri") if use_stored_redirect_uri else None
        return OAuthClientConfig(
            client_id=client_id.strip(),
            client_secret=client_secret.strip() if isinstance(client_secret, str) and client_secret.strip() else None,
            redirect_uri=redirect_uri.strip()
            if isinstance(redirect_uri, str) and redirect_uri.strip()
            else self.default_redirect_uri(runtime_paths),
        )

    async def require_client_config_async(self, runtime_paths: RuntimePaths) -> OAuthClientConfig:
        """Return client settings after lazy bootstrap or raise a safe configuration error."""
        resolution = await self.client_config_resolution_async(runtime_paths)
        if resolution is not None:
            return resolution.config
        raise self._missing_client_config_error()

    def _missing_client_config_error(self) -> _OAuthProviderNotConfiguredError:
        """Build one safe client-configuration error."""
        services = ", ".join(self.all_client_config_services) or "a *_oauth_client credential service"
        required_fields = (
            "client_id"
            if self.token_endpoint_auth_method == _PUBLIC_TOKEN_ENDPOINT_AUTH_METHOD
            else "client_id and client_secret"
        )
        msg = f"OAuth provider '{self.id}' is not configured. Store {required_fields} in {services}."
        return _OAuthProviderNotConfiguredError(msg)

    async def runtime_endpoints(self, runtime_paths: RuntimePaths) -> OAuthRuntimeEndpoints:
        """Return OAuth endpoints, resolving dynamic provider metadata when configured."""
        if self.runtime_bootstrapper is not None:
            endpoints = await self.runtime_bootstrapper(self, runtime_paths)
        else:
            endpoints = OAuthRuntimeEndpoints(
                authorization_url=self.authorization_url,
                token_url=self.token_url,
                token_endpoint_auth_method=self.token_endpoint_auth_method,
            )
        if not endpoints.authorization_url.strip() or not endpoints.token_url.strip():
            msg = f"OAuth provider '{self.id}' could not resolve authorization and token endpoints."
            raise OAuthProviderError(msg)
        return endpoints

    def _runtime_token_endpoint_auth_method(self, endpoints: OAuthRuntimeEndpoints) -> _TokenEndpointAuthMethod:
        """Return the token endpoint auth method after endpoint resolution."""
        return endpoints.token_endpoint_auth_method or self.token_endpoint_auth_method

    def default_redirect_uri(self, runtime_paths: RuntimePaths) -> str:
        """Return the local default redirect URI for this provider."""
        configured_origin = runtime_paths.env_value("MINDROOM_PUBLIC_URL") or runtime_paths.env_value(
            "MINDROOM_BASE_URL",
        )
        if configured_origin:
            return f"{configured_origin.rstrip('/')}{self.redirect_path}"
        return f"http://localhost:{_runtime_port(runtime_paths)}{self.redirect_path}"

    def issue_pkce_code_verifier(self) -> str | None:
        """Return a new PKCE verifier when this provider requires PKCE."""
        if self.pkce_code_challenge_method is None:
            return None
        return _generate_pkce_code_verifier()

    async def authorization_uri_async(
        self,
        runtime_paths: RuntimePaths,
        *,
        state: str,
        code_verifier: str | None = None,
    ) -> str:
        """Build the provider authorization URL, resolving lazy runtime metadata first."""
        endpoints = await self.runtime_endpoints(runtime_paths)
        client_config = await self.require_client_config_async(runtime_paths)
        client = OAuth2Session(
            client_id=client_config.client_id,
            client_secret=client_config.client_secret,
            scope=self.scopes,
            redirect_uri=client_config.redirect_uri,
            token_endpoint_auth_method=self._runtime_token_endpoint_auth_method(endpoints),
        )
        auth_params = dict(self.extra_auth_params)
        if self.pkce_code_challenge_method is not None:
            if not code_verifier:
                msg = "OAuth provider requires a PKCE code verifier"
                raise OAuthProviderError(msg)
            auth_params["code_challenge"] = _pkce_s256_code_challenge(code_verifier)
            auth_params["code_challenge_method"] = self.pkce_code_challenge_method
        try:
            authorization_url, _ = client.create_authorization_url(
                endpoints.authorization_url,
                state=state,
                **auth_params,
            )
        finally:
            client.close()
        return authorization_url

    async def exchange_code(
        self,
        code: str,
        runtime_paths: RuntimePaths,
        *,
        code_verifier: str | None = None,
    ) -> OAuthTokenResult:
        """Exchange an authorization code for normalized credentials."""
        endpoints = await self.runtime_endpoints(runtime_paths)
        client_config = await self.require_client_config_async(runtime_paths)
        if self.pkce_code_challenge_method is not None and not code_verifier:
            msg = "OAuth provider requires a PKCE code verifier"
            raise OAuthProviderError(msg)
        if self.token_exchanger is not None:
            result = self.token_exchanger(self, code, client_config, runtime_paths, code_verifier)
            if isinstance(result, OAuthTokenResult):
                return _token_result_with_core_metadata(self, result, client_id=client_config.client_id)
            return _token_result_with_core_metadata(
                self,
                await cast("Awaitable[OAuthTokenResult]", result),
                client_id=client_config.client_id,
            )

        async with AsyncOAuth2Client(
            client_id=client_config.client_id,
            client_secret=client_config.client_secret,
            scope=self.scopes,
            redirect_uri=client_config.redirect_uri,
            token_endpoint_auth_method=self._runtime_token_endpoint_auth_method(endpoints),
            timeout=_DEFAULT_AUTHORIZE_TIMEOUT_SECONDS,
        ) as client:
            try:
                fetch_kwargs: dict[str, Any] = {
                    "code": code,
                    "grant_type": "authorization_code",
                }
                fetch_kwargs.update(self.extra_token_params)
                if self.pkce_code_challenge_method is not None:
                    fetch_kwargs["code_verifier"] = code_verifier
                token_response = await client.fetch_token(
                    endpoints.token_url,
                    **fetch_kwargs,
                )
            except (AuthlibBaseError, HTTPError) as exc:
                msg = "OAuth token exchange failed"
                raise OAuthProviderError(msg) from exc
        if not isinstance(token_response, Mapping):
            msg = "OAuth token exchange failed"
            raise OAuthProviderError(msg)
        parser = self.token_parser or _default_token_parser
        token_response = dict(token_response)
        token_response["_mindroom_token_url"] = endpoints.token_url
        return _token_result_with_core_metadata(
            self,
            parser(self, token_response, client_config, runtime_paths),
            client_id=client_config.client_id,
        )

    async def refresh_token_data(
        self,
        token_data: Mapping[str, Any],
        runtime_paths: RuntimePaths,
        *,
        force: bool = False,
    ) -> dict[str, Any] | None:
        """Refresh expiring token data and return updated credentials, or None."""
        if not force and not _token_data_needs_refresh(token_data):
            return None
        refresh_token = cast("str", token_data["refresh_token"])

        endpoints = await self.runtime_endpoints(runtime_paths)
        client_config = await self.require_client_config_async(runtime_paths)
        async with AsyncOAuth2Client(
            client_id=client_config.client_id,
            client_secret=client_config.client_secret,
            scope=self.scopes,
            token_endpoint_auth_method=self._runtime_token_endpoint_auth_method(endpoints),
            timeout=_DEFAULT_AUTHORIZE_TIMEOUT_SECONDS,
        ) as client:
            try:
                token_response = await client.refresh_token(
                    endpoints.token_url,
                    refresh_token=refresh_token,
                    **self.extra_token_params,
                )
            except (AuthlibBaseError, HTTPError) as exc:
                raise _oauth_refresh_error(exc) from exc
        if not isinstance(token_response, Mapping):
            msg = "OAuth token refresh failed"
            raise OAuthProviderError(msg)

        refresh_response = dict(token_response)
        refresh_response["_mindroom_token_url"] = endpoints.token_url
        response_refresh_token = refresh_response.get("refresh_token")
        existing_refresh_token = token_data.get("refresh_token")
        if (
            (not isinstance(response_refresh_token, str) or not response_refresh_token)
            and isinstance(existing_refresh_token, str)
            and existing_refresh_token
        ):
            refresh_response["refresh_token"] = existing_refresh_token

        response_claims = refresh_response.get("_oauth_claims")
        existing_claims = token_data.get("_oauth_claims")
        if (
            (not isinstance(response_claims, Mapping) or not response_claims)
            and isinstance(existing_claims, Mapping)
            and existing_claims
        ):
            refresh_response["_oauth_claims"] = existing_claims
        if (
            refresh_response.get("_oauth_claims_verified") is not True
            and token_data.get("_oauth_claims_verified") is True
        ):
            refresh_response["_oauth_claims_verified"] = True
        parser = self.token_parser or _default_token_parser
        result = parser(self, refresh_response, client_config, runtime_paths)
        verified_claims = refresh_response.get("_oauth_claims")
        if (
            not result.claims_verified
            and refresh_response.get("_oauth_claims_verified") is True
            and isinstance(verified_claims, Mapping)
        ):
            result = OAuthTokenResult(
                token_data=result.token_data,
                claims=dict(verified_claims),
                claims_verified=True,
            )
        result = _token_result_with_core_metadata(self, result, client_id=client_config.client_id)
        self.validate_claims(result, runtime_paths)
        return self.token_result_with_safe_claims(result).token_data

    def resolved_allowed_email_domains(self, runtime_paths: RuntimePaths) -> tuple[str, ...]:
        """Return email-domain restrictions from provider config and env."""
        configured = tuple(domain.strip().lower() for domain in self.allowed_email_domains if domain.strip())
        env_value = _runtime_env_value(runtime_paths, _normalize_env_names(self.allowed_email_domains_env))
        return tuple(dict.fromkeys((*configured, *_split_csv(env_value))))

    def resolved_allowed_hosted_domains(self, runtime_paths: RuntimePaths) -> tuple[str, ...]:
        """Return hosted-domain restrictions from provider config and env."""
        configured = tuple(domain.strip().lower() for domain in self.allowed_hosted_domains if domain.strip())
        env_value = _runtime_env_value(runtime_paths, _normalize_env_names(self.allowed_hosted_domains_env))
        return tuple(dict.fromkeys((*configured, *_split_csv(env_value))))

    def validate_claims(self, result: OAuthTokenResult, runtime_paths: RuntimePaths) -> None:
        """Apply generic and provider-specific identity restrictions."""
        allowed_email_domains = self.resolved_allowed_email_domains(runtime_paths)
        allowed_hosted_domains = self.resolved_allowed_hosted_domains(runtime_paths)
        if (allowed_email_domains or allowed_hosted_domains) and not result.claims_verified:
            msg = "Configured OAuth identity restrictions require verified provider claims"
            raise OAuthClaimValidationError(msg)

        if allowed_email_domains:
            if result.claims.get("email_verified") is not True:
                msg = "OAuth account email ownership is not verified"
                raise OAuthClaimValidationError(msg)
            email_domain = _claim_email_domain(result.claims)
            if email_domain is None or email_domain not in allowed_email_domains:
                msg = "OAuth account email domain is not allowed"
                raise OAuthClaimValidationError(msg)

        if allowed_hosted_domains:
            hosted_domain = result.claims.get("hd")
            if not isinstance(hosted_domain, str) or hosted_domain.lower() not in allowed_hosted_domains:
                msg = "OAuth hosted domain claim is not allowed"
                raise OAuthClaimValidationError(msg)

        if self.claim_validator is not None:
            context = _OAuthClaimValidationContext(
                provider_id=self.id,
                token_data=result.token_data,
                claims=result.claims,
                claims_verified=result.claims_verified,
                runtime_paths=runtime_paths,
            )
            self.claim_validator(context)

    def token_result_with_safe_claims(self, result: OAuthTokenResult) -> OAuthTokenResult:
        """Return token result with safe claim summary persisted as internal metadata."""
        result = _token_result_with_core_metadata(self, result)
        token_data = dict(result.token_data)
        token_data.pop("_id_token", None)
        token_data.pop("id_token", None)
        token_data.pop("client_secret", None)
        token_data.pop("_oauth_claims", None)
        token_data.pop("_oauth_claims_verified", None)
        if result.claims and result.claims_verified:
            token_data["_oauth_claims"] = _verified_claims_for_storage(result.claims)
            token_data["_oauth_claims_verified"] = True
        return OAuthTokenResult(
            token_data=token_data,
            claims=dict(result.claims),
            claims_verified=result.claims_verified,
        )
