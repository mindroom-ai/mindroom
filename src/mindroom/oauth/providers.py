"""Provider contracts for MindRoom-managed OAuth flows."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import warnings
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, cast

from authlib.common.errors import AuthlibBaseError
from authlib.deprecate import AuthlibDeprecationWarning
from httpx import HTTPError

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

_DEFAULT_AUTHORIZE_TIMEOUT_SECONDS = 20.0
_TOKEN_ENDPOINT_AUTH_METHOD = "client_secret_post"  # noqa: S105
_PKCECodeChallengeMethod = Literal["S256"]


class OAuthProviderError(RuntimeError):
    """Base error for provider configuration and OAuth flow failures."""


class OAuthProviderNotConfiguredError(OAuthProviderError):
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
    ) -> None:
        super().__init__(message)
        self.provider_id = provider_id
        self.connect_url = connect_url


@dataclass(frozen=True, slots=True)
class OAuthClientConfig:
    """Resolved OAuth client settings for one runtime."""

    client_id: str
    client_secret: str
    redirect_uri: str


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
class OAuthClaimValidationContext:
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
_OAuthClaimValidator = Callable[[OAuthClaimValidationContext], None]


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
        "token_uri": provider.token_url,
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
    pkce_code_challenge_method: _PKCECodeChallengeMethod | None = None
    allowed_email_domains: tuple[str, ...] = ()
    allowed_hosted_domains: tuple[str, ...] = ()
    allowed_email_domains_env: str | Sequence[str] | None = None
    allowed_hosted_domains_env: str | Sequence[str] | None = None
    status_capabilities: tuple[str, ...] = ()
    token_parser: _OAuthTokenParser | None = None
    token_exchanger: _OAuthTokenExchanger | None = None
    claim_validator: _OAuthClaimValidator | None = None

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
        for service in self.all_client_config_services:
            validate_service_name(service)
            if not is_oauth_client_config_service(service):
                msg = f"OAuth provider '{self.id}' client config service '{service}' must end with '_oauth_client'"
                raise ValueError(msg)
        if not self.all_client_config_services:
            msg = f"OAuth provider '{self.id}' must declare at least one client config service"
            raise ValueError(msg)
        if not self.scopes:
            msg = f"OAuth provider '{self.id}' must declare at least one scope"
            raise ValueError(msg)
        if self.pkce_code_challenge_method not in {None, "S256"}:
            msg = f"OAuth provider '{self.id}' supports only S256 PKCE"
            raise ValueError(msg)
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
        if not isinstance(client_secret, str) or not client_secret.strip():
            return None
        redirect_uri = credentials.get("redirect_uri") if use_stored_redirect_uri else None
        return OAuthClientConfig(
            client_id=client_id.strip(),
            client_secret=client_secret.strip(),
            redirect_uri=redirect_uri.strip()
            if isinstance(redirect_uri, str) and redirect_uri.strip()
            else self.default_redirect_uri(runtime_paths),
        )

    def require_client_config(self, runtime_paths: RuntimePaths) -> OAuthClientConfig:
        """Return client settings or raise one safe user-facing configuration error."""
        client_config = self.client_config(runtime_paths)
        if client_config is not None:
            return client_config
        services = ", ".join(self.all_client_config_services) or "a *_oauth_client credential service"
        msg = f"OAuth provider '{self.id}' is not configured. Store client_id and client_secret in {services}."
        raise OAuthProviderNotConfiguredError(msg)

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

    def authorization_uri(
        self,
        runtime_paths: RuntimePaths,
        *,
        state: str,
        code_verifier: str | None = None,
    ) -> str:
        """Build the provider authorization URL for one state token."""
        client_config = self.require_client_config(runtime_paths)
        client = OAuth2Session(
            client_id=client_config.client_id,
            client_secret=client_config.client_secret,
            scope=self.scopes,
            redirect_uri=client_config.redirect_uri,
            token_endpoint_auth_method=_TOKEN_ENDPOINT_AUTH_METHOD,
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
                self.authorization_url,
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
        client_config = self.require_client_config(runtime_paths)
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
            token_endpoint_auth_method=_TOKEN_ENDPOINT_AUTH_METHOD,
            timeout=_DEFAULT_AUTHORIZE_TIMEOUT_SECONDS,
        ) as client:
            try:
                fetch_kwargs: dict[str, Any] = {
                    "code": code,
                    "grant_type": "authorization_code",
                }
                if self.pkce_code_challenge_method is not None:
                    fetch_kwargs["code_verifier"] = code_verifier
                token_response = await client.fetch_token(
                    self.token_url,
                    **fetch_kwargs,
                )
            except (AuthlibBaseError, HTTPError) as exc:
                msg = "OAuth token exchange failed"
                raise OAuthProviderError(msg) from exc
        if not isinstance(token_response, Mapping):
            msg = "OAuth token exchange failed"
            raise OAuthProviderError(msg)
        parser = self.token_parser or _default_token_parser
        return _token_result_with_core_metadata(
            self,
            parser(self, token_response, client_config, runtime_paths),
            client_id=client_config.client_id,
        )

    async def refresh_token_data(
        self,
        token_data: Mapping[str, Any],
        runtime_paths: RuntimePaths,
    ) -> dict[str, Any] | None:
        """Refresh stored credentials with a provider refresh token when possible."""
        refresh_token = token_data.get("refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token:
            return None
        client_config = self.require_client_config(runtime_paths)
        async with AsyncOAuth2Client(
            client_id=client_config.client_id,
            client_secret=client_config.client_secret,
            scope=self.scopes,
            redirect_uri=client_config.redirect_uri,
            token_endpoint_auth_method=_TOKEN_ENDPOINT_AUTH_METHOD,
            token=dict(token_data),
            timeout=_DEFAULT_AUTHORIZE_TIMEOUT_SECONDS,
        ) as client:
            try:
                response_data = await client.refresh_token(self.token_url, refresh_token=refresh_token)
            except (AuthlibBaseError, HTTPError):
                return None
        if not isinstance(response_data, Mapping):
            return None
        merged_response = dict(response_data)
        existing_claims = token_data.get("_oauth_claims")
        existing_claims_verified = token_data.get("_oauth_claims_verified")
        if "refresh_token" not in merged_response:
            merged_response["refresh_token"] = refresh_token
        if "_oauth_claims" not in merged_response and isinstance(existing_claims, Mapping):
            merged_response["_oauth_claims"] = dict(existing_claims)
        if "_oauth_claims_verified" not in merged_response and existing_claims_verified is True:
            merged_response["_oauth_claims_verified"] = True
        parsed = _token_result_with_core_metadata(
            self,
            (self.token_parser or _default_token_parser)(self, merged_response, client_config, runtime_paths),
            client_id=client_config.client_id,
        )
        refreshed = dict(parsed.token_data)
        if "refresh_token" not in parsed.token_data:
            refreshed["refresh_token"] = refresh_token
        return refreshed

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
            context = OAuthClaimValidationContext(
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
