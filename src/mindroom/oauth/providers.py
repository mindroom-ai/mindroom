"""Provider contracts for MindRoom-managed OAuth flows."""

from __future__ import annotations

import base64
import json
import time
import warnings
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from authlib.common.errors import AuthlibBaseError
from authlib.deprecate import AuthlibDeprecationWarning
from httpx import HTTPError

from mindroom.credentials import validate_service_name

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


class OAuthProviderError(RuntimeError):
    """Base error for provider configuration and OAuth flow failures."""


class OAuthProviderNotConfiguredError(OAuthProviderError):
    """Raised when a provider has no usable OAuth client configuration."""


class OAuthClaimValidationError(OAuthProviderError):
    """Raised when verified provider claims do not satisfy configured policy."""


class OAuthConnectionRequired(OAuthProviderError):  # noqa: N818
    """Raised by tools when a user must connect an OAuth provider."""


@dataclass(frozen=True, slots=True)
class OAuthClientConfig:
    """Resolved OAuth client settings for one runtime."""

    client_id: str
    client_secret: str
    redirect_uri: str


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


OAuthTokenParser = Callable[["OAuthProvider", Mapping[str, Any], OAuthClientConfig, "RuntimePaths"], OAuthTokenResult]
OAuthTokenExchanger = Callable[
    ["OAuthProvider", str, OAuthClientConfig, "RuntimePaths"],
    OAuthTokenResult | Awaitable[OAuthTokenResult],
]
OAuthClaimValidator = Callable[[OAuthClaimValidationContext], None]


def _normalize_env_names(names: str | Sequence[str] | None) -> tuple[str, ...]:
    if names is None:
        return ()
    if isinstance(names, str):
        return (names,)
    return tuple(name for name in names if name)


def _env_prefix(provider_id: str) -> str:
    return "".join(char if char.isalnum() else "_" for char in provider_id.upper())


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
        if claims:
            token_data["_oauth_claims"] = _safe_claim_summary(claims)

    return OAuthTokenResult(token_data=token_data, claims=claims, claims_verified=False)


def _safe_claim_summary(claims: Mapping[str, Any]) -> dict[str, str]:
    summary: dict[str, str] = {}
    for key in ("sub", "email", "hd"):
        value = claims.get(key)
        if isinstance(value, str) and value:
            summary[key] = value
    return summary


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


@dataclass(frozen=True, slots=True)
class OAuthProvider:
    """Provider definition registered by core or a plugin."""

    id: str
    display_name: str
    authorization_url: str
    token_url: str
    scopes: tuple[str, ...]
    credential_service: str
    client_id_env: str | Sequence[str] | None = None
    client_secret_env: str | Sequence[str] | None = None
    redirect_uri_env: str | Sequence[str] | None = None
    default_redirect_path: str | None = None
    extra_auth_params: Mapping[str, str] = field(default_factory=dict)
    allowed_email_domains: tuple[str, ...] = ()
    allowed_hosted_domains: tuple[str, ...] = ()
    allowed_email_domains_env: str | Sequence[str] | None = None
    allowed_hosted_domains_env: str | Sequence[str] | None = None
    status_capabilities: tuple[str, ...] = ()
    token_parser: OAuthTokenParser | None = None
    token_exchanger: OAuthTokenExchanger | None = None
    claim_validator: OAuthClaimValidator | None = None

    def __post_init__(self) -> None:
        """Validate provider identifiers and redirect path shape."""
        validate_service_name(self.id)
        validate_service_name(self.credential_service)
        if not self.scopes:
            msg = f"OAuth provider '{self.id}' must declare at least one scope"
            raise ValueError(msg)
        redirect_path = self.redirect_path
        if not redirect_path.startswith("/"):
            msg = f"OAuth provider '{self.id}' default_redirect_path must start with '/'"
            raise ValueError(msg)

    @property
    def redirect_path(self) -> str:
        """Return the relative MindRoom callback path for this provider."""
        return self.default_redirect_path or f"/api/oauth/{self.id}/callback"

    @property
    def normalized_client_id_env(self) -> tuple[str, ...]:
        """Return client ID environment variable names in lookup order."""
        explicit = _normalize_env_names(self.client_id_env)
        if explicit:
            return explicit
        return (f"MINDROOM_OAUTH_{_env_prefix(self.id)}_CLIENT_ID",)

    @property
    def normalized_client_secret_env(self) -> tuple[str, ...]:
        """Return client secret environment variable names in lookup order."""
        explicit = _normalize_env_names(self.client_secret_env)
        if explicit:
            return explicit
        return (f"MINDROOM_OAUTH_{_env_prefix(self.id)}_CLIENT_SECRET",)

    @property
    def normalized_redirect_uri_env(self) -> tuple[str, ...]:
        """Return redirect URI environment variable names in lookup order."""
        explicit = _normalize_env_names(self.redirect_uri_env)
        if explicit:
            return explicit
        return (f"MINDROOM_OAUTH_{_env_prefix(self.id)}_REDIRECT_URI",)

    def client_config(self, runtime_paths: RuntimePaths) -> OAuthClientConfig | None:
        """Return resolved client settings or None when the provider is not configured."""
        client_id = _runtime_env_value(runtime_paths, self.normalized_client_id_env)
        client_secret = _runtime_env_value(runtime_paths, self.normalized_client_secret_env)
        if not client_id or not client_secret:
            return None
        redirect_uri = _runtime_env_value(runtime_paths, self.normalized_redirect_uri_env)
        return OAuthClientConfig(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri or self.default_redirect_uri(runtime_paths),
        )

    def require_client_config(self, runtime_paths: RuntimePaths) -> OAuthClientConfig:
        """Return client settings or raise one safe user-facing configuration error."""
        client_config = self.client_config(runtime_paths)
        if client_config is not None:
            return client_config
        env_names = ", ".join((*self.normalized_client_id_env, *self.normalized_client_secret_env))
        msg = f"OAuth provider '{self.id}' is not configured. Set {env_names}."
        raise OAuthProviderNotConfiguredError(msg)

    def default_redirect_uri(self, runtime_paths: RuntimePaths) -> str:
        """Return the local default redirect URI for this provider."""
        return f"http://localhost:{_runtime_port(runtime_paths)}{self.redirect_path}"

    def authorization_uri(self, runtime_paths: RuntimePaths, *, state: str) -> str:
        """Build the provider authorization URL for one state token."""
        client_config = self.require_client_config(runtime_paths)
        client = OAuth2Session(
            client_id=client_config.client_id,
            client_secret=client_config.client_secret,
            scope=self.scopes,
            redirect_uri=client_config.redirect_uri,
            token_endpoint_auth_method=_TOKEN_ENDPOINT_AUTH_METHOD,
        )
        try:
            authorization_url, _ = client.create_authorization_url(
                self.authorization_url,
                state=state,
                **dict(self.extra_auth_params),
            )
        finally:
            client.close()
        return authorization_url

    async def exchange_code(self, code: str, runtime_paths: RuntimePaths) -> OAuthTokenResult:
        """Exchange an authorization code for normalized credentials."""
        client_config = self.require_client_config(runtime_paths)
        if self.token_exchanger is not None:
            result = self.token_exchanger(self, code, client_config, runtime_paths)
            if isinstance(result, OAuthTokenResult):
                return result
            return await cast("Awaitable[OAuthTokenResult]", result)

        async with AsyncOAuth2Client(
            client_id=client_config.client_id,
            client_secret=client_config.client_secret,
            scope=self.scopes,
            redirect_uri=client_config.redirect_uri,
            token_endpoint_auth_method=_TOKEN_ENDPOINT_AUTH_METHOD,
            timeout=_DEFAULT_AUTHORIZE_TIMEOUT_SECONDS,
        ) as client:
            try:
                token_response = await client.fetch_token(
                    self.token_url,
                    code=code,
                    grant_type="authorization_code",
                )
            except (AuthlibBaseError, HTTPError) as exc:
                msg = "OAuth token exchange failed"
                raise OAuthProviderError(msg) from exc
        if not isinstance(token_response, Mapping):
            msg = "OAuth token exchange failed"
            raise OAuthProviderError(msg)
        parser = self.token_parser or _default_token_parser
        return parser(self, token_response, client_config, runtime_paths)

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
        merged_response = {**dict(token_data), **response_data}
        parsed = (self.token_parser or _default_token_parser)(self, merged_response, client_config, runtime_paths)
        refreshed = dict(token_data)
        refreshed.update(parsed.token_data)
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
        token_data = dict(result.token_data)
        if result.claims:
            token_data["_oauth_claims"] = _safe_claim_summary(result.claims)
        return OAuthTokenResult(
            token_data=token_data,
            claims=dict(result.claims),
            claims_verified=result.claims_verified,
        )
