# ruff: noqa: D100
from __future__ import annotations

import asyncio
import html
import importlib
import json
import re
import secrets
import time
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Protocol, cast
from urllib.parse import quote, unquote, urlencode

import jwt
from fastapi import APIRouter, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from jwt import PyJWKClient, PyJWTError
from pydantic import BaseModel

from mindroom.api import config_lifecycle
from mindroom.api.config_lifecycle import ApiSnapshot
from mindroom.api.config_lifecycle import request_snapshot as request_api_snapshot
from mindroom.api.config_lifecycle import store_request_snapshot as store_request_api_snapshot
from mindroom.api.connections_session_cookie import connections_session_cookie, read_connections_session
from mindroom.api.open_access import open_access_rejection
from mindroom.authorization import is_platform_administrator
from mindroom.matrix.identity import (
    matrix_user_id_from_email,
    try_parse_historical_matrix_user_id,
    validate_email_to_matrix_mapping,
)
from mindroom.origins import deployment_origin, normalized_origin
from mindroom.tool_system.dependencies import (
    auto_install_enabled,
    auto_install_optional_extra_for_import_retry,
    optional_dependency_environment,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.constants import RuntimePaths

router = APIRouter(tags=["auth"])


def require_operator_key(request: Request, authorization: str | None) -> None:
    """Require the runtime's configured operator bearer key, regardless of user auth mode."""
    runtime_paths = config_lifecycle.bind_current_request_snapshot(request).runtime_paths
    configured_key = runtime_paths.env_value("MINDROOM_API_KEY")
    if not configured_key:
        raise HTTPException(status_code=503, detail="This operational check requires MINDROOM_API_KEY")
    token = _extract_bearer_token(authorization)
    if token is None or not secrets.compare_digest(token.encode(), configured_key.encode()):
        raise HTTPException(status_code=401, detail="Missing or invalid credentials")


# Host-only by prefix, so sibling tenant subdomains can neither receive nor overwrite it.
_PLATFORM_SESSION_COOKIE_NAME = "__Host-mindroom_platform_session"
_PLATFORM_SESSION_TYPE = "mindroom_platform_session"
_PLATFORM_SESSION_MAX_AGE_SECONDS = 3600
_PLATFORM_SSO_TICKET_TYPE = "mindroom_platform_sso_ticket"
_PLATFORM_SSO_CLOCK_SKEW_SECONDS = 10
# Ticket IDs already exchanged by this runtime, mapped to their expiry.
_used_platform_sso_ticket_ids: dict[str, int] = {}
_STANDALONE_AUTH_COOKIE_NAME = "mindroom_api_key"
# Set only by server code; ASGI scope keys cannot come from the client.
_CONNECTIONS_SESSION_CALLBACK_SCOPE_KEY = "mindroom.connections_session_callback"
# Each Connections page names the account it shows, so a stale tab cannot act as the browser's new account.
_CONNECTIONS_USER_HEADER = "X-MindRoom-Connections-User"
_TRUSTED_UPSTREAM_JWKS_CACHE_SECONDS = 60
_TRUSTED_UPSTREAM_JWKS_TIMEOUT_SECONDS = 5
_JWT_MAX_BYTES = 16 * 1024
_SUPABASE_TOKEN_SHAPE = re.compile(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
# Each Supabase token check is a blocking network call in a worker thread; cap how many run at once.
_supabase_token_checks = asyncio.Semaphore(4)
_REDIRECT_TARGET_DECODE_PASSES = 5
_STANDALONE_PUBLIC_PATHS = frozenset(
    {
        "/api/homeassistant/callback",
        "/api/integrations/spotify/callback",
    },
)


class _AuthSessionRequest(BaseModel):
    """Standalone dashboard login payload."""

    api_key: str


class _SupabaseUserProtocol(Protocol):
    id: str
    email: str | None


class _SupabaseUserResponseProtocol(Protocol):
    user: _SupabaseUserProtocol | None


class _SupabaseAuthProtocol(Protocol):
    def get_user(self, token: str) -> _SupabaseUserResponseProtocol | None: ...


class _SupabaseClientProtocol(Protocol):
    auth: _SupabaseAuthProtocol


@dataclass(frozen=True)
class _TrustedUpstreamJwtIdentity:
    """Identity claims verified from the upstream JWT."""

    email: str
    user_id: str | None = None
    matrix_user_id: str | None = None


@dataclass(frozen=True)
class _TrustedUpstreamAuthSettings:
    """Settings for the signed assertion in which a deployment-owned gateway names the browser user."""

    enabled: bool = False
    jwt_header: str | None = None
    jwks_url: str | None = None
    audience: str | None = None
    issuer: str | None = None
    email_claim: str = "email"
    user_id_claim: str | None = None
    matrix_user_id_claim: str | None = None
    email_to_matrix_user_id_template: str | None = None
    email_domain: str | None = None


@dataclass(frozen=True)
class _ApiAuthSettings:
    """Dashboard authentication settings for one runtime."""

    supabase_url: str | None
    supabase_anon_key: str | None
    account_id: str | None
    mindroom_api_key: str | None
    public_url: str | None = None
    platform_sso_url: str | None = None
    platform_sso_secret: str | None = None
    trusted_upstream: _TrustedUpstreamAuthSettings = field(default_factory=_TrustedUpstreamAuthSettings)


@dataclass(frozen=True)
class ApiAuthState:
    """Cached authentication client state for one runtime."""

    runtime_paths: RuntimePaths
    settings: _ApiAuthSettings
    supabase_auth: _SupabaseClientProtocol | None
    trusted_upstream_jwt_client: PyJWKClient | None = None


def _build_auth_settings(runtime_paths: RuntimePaths, *, account_id: str | None = None) -> _ApiAuthSettings:
    """Read dashboard auth settings from one explicit runtime context."""
    return _ApiAuthSettings(
        supabase_url=runtime_paths.env_value("SUPABASE_URL"),
        supabase_anon_key=runtime_paths.env_value("SUPABASE_ANON_KEY"),
        account_id=account_id,
        mindroom_api_key=runtime_paths.env_value("MINDROOM_API_KEY"),
        public_url=runtime_paths.env_value("MINDROOM_PUBLIC_URL"),
        platform_sso_url=_env_text(runtime_paths, "MINDROOM_PLATFORM_SSO_URL"),
        platform_sso_secret=_env_text(runtime_paths, "MINDROOM_PLATFORM_SSO_SECRET"),
        trusted_upstream=_build_trusted_upstream_auth_settings(runtime_paths),
    )


def _platform_auth_credentials(settings: _ApiAuthSettings) -> tuple[str, str] | None:
    """Return the Supabase URL and anon key when platform authentication owns dashboard requests."""
    if settings.supabase_url and settings.supabase_anon_key:
        return settings.supabase_url, settings.supabase_anon_key
    return None


def dashboard_requires_credential(runtime_paths: RuntimePaths) -> bool:
    """Return whether dashboard requests need a credential instead of being authorized by reachability alone."""
    settings = _build_auth_settings(runtime_paths)
    return (
        settings.trusted_upstream.enabled
        or _platform_auth_credentials(settings) is not None
        or bool(settings.mindroom_api_key)
    )


def _env_text(runtime_paths: RuntimePaths, name: str) -> str | None:
    value = runtime_paths.env_value(name)
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _build_trusted_upstream_auth_settings(runtime_paths: RuntimePaths) -> _TrustedUpstreamAuthSettings:
    """Read trusted-upstream auth settings from one runtime context."""
    return _TrustedUpstreamAuthSettings(
        enabled=runtime_paths.env_flag("MINDROOM_TRUSTED_UPSTREAM_AUTH_ENABLED"),
        jwt_header=_env_text(runtime_paths, "MINDROOM_TRUSTED_UPSTREAM_JWT_HEADER"),
        jwks_url=_env_text(runtime_paths, "MINDROOM_TRUSTED_UPSTREAM_JWKS_URL"),
        audience=_env_text(runtime_paths, "MINDROOM_TRUSTED_UPSTREAM_JWT_AUDIENCE"),
        issuer=_env_text(runtime_paths, "MINDROOM_TRUSTED_UPSTREAM_JWT_ISSUER"),
        email_claim=_env_text(runtime_paths, "MINDROOM_TRUSTED_UPSTREAM_JWT_EMAIL_CLAIM") or "email",
        user_id_claim=_env_text(runtime_paths, "MINDROOM_TRUSTED_UPSTREAM_JWT_USER_ID_CLAIM"),
        matrix_user_id_claim=_env_text(runtime_paths, "MINDROOM_TRUSTED_UPSTREAM_JWT_MATRIX_USER_ID_CLAIM"),
        email_to_matrix_user_id_template=_env_text(
            runtime_paths,
            "MINDROOM_TRUSTED_UPSTREAM_EMAIL_TO_MATRIX_USER_ID_TEMPLATE",
        ),
        email_domain=_env_text(runtime_paths, "MINDROOM_TRUSTED_UPSTREAM_EMAIL_DOMAIN"),
    )


def _build_trusted_upstream_jwt_client(settings: _TrustedUpstreamAuthSettings) -> PyJWKClient | None:
    """Return a short-lived JWKS cache for trusted-upstream auth."""
    if not settings.enabled or settings.jwks_url is None:
        return None
    return PyJWKClient(
        settings.jwks_url,
        cache_keys=False,
        cache_jwk_set=True,
        lifespan=_TRUSTED_UPSTREAM_JWKS_CACHE_SECONDS,
        timeout=_TRUSTED_UPSTREAM_JWKS_TIMEOUT_SECONDS,
    )


def _app_auth_state(api_app: FastAPI) -> ApiAuthState:
    """Return the committed auth state for one API app instance."""
    app_state = config_lifecycle.app_state(api_app)
    api_state = config_lifecycle.require_api_state(api_app)
    with api_state.config_lock:
        snapshot = api_state.snapshot
        state = cast("ApiAuthState | None", snapshot.auth_state)
        if state is not None and state.runtime_paths == snapshot.runtime_paths:
            return state
        settings = _build_auth_settings(snapshot.runtime_paths, account_id=app_state.api_auth_account_id)
        state = ApiAuthState(
            runtime_paths=snapshot.runtime_paths,
            settings=settings,
            supabase_auth=_init_supabase_auth(snapshot.runtime_paths, settings),
            trusted_upstream_jwt_client=_build_trusted_upstream_jwt_client(settings.trusted_upstream),
        )
        api_state.snapshot = replace(snapshot, auth_state=state)
        return state


def _init_supabase_auth(
    runtime_paths: RuntimePaths,
    settings: _ApiAuthSettings,
) -> _SupabaseClientProtocol | None:
    """Initialize Supabase auth client when credentials are configured."""
    credentials = _platform_auth_credentials(settings)
    if credentials is None:
        return None

    try:
        with optional_dependency_environment():
            create_client = importlib.import_module("supabase").create_client
    except ModuleNotFoundError:
        disabled_hint = ""
        if not auto_install_enabled(runtime_paths):
            disabled_hint = " Auto-install is disabled by MINDROOM_NO_AUTO_INSTALL_TOOLS."
        if not auto_install_optional_extra_for_import_retry("supabase", runtime_paths):
            msg = (
                "SUPABASE_URL and SUPABASE_ANON_KEY are set but the 'supabase' package is not available."
                f"{disabled_hint} Install it with: pip install 'mindroom[supabase]'"
            )
            raise ImportError(msg) from None
        with optional_dependency_environment():
            create_client = importlib.import_module("supabase").create_client

    return cast("_SupabaseClientProtocol", create_client(*credentials))


def _extract_bearer_token(authorization: str | None) -> str | None:
    """Return the bearer token value from an Authorization header."""
    if not authorization or not authorization.startswith("Bearer "):
        return None
    token = authorization.removeprefix("Bearer ").strip()
    return token or None


def _is_standalone_public_path(path: str) -> bool:
    """Return whether one unauthenticated standalone callback path may enter its handler."""
    return path in _STANDALONE_PUBLIC_PATHS


def _validated_trusted_upstream_email_to_matrix_template(settings: _TrustedUpstreamAuthSettings) -> str | None:
    """Return the configured email-to-Matrix template after validating it."""
    template = settings.email_to_matrix_user_id_template
    if template is None:
        return None
    try:
        validate_email_to_matrix_mapping(template, settings.email_domain)
    except ValueError as exc:
        raise HTTPException(
            status_code=500,
            detail=(
                "Trusted upstream email mapping requires a valid template and MINDROOM_TRUSTED_UPSTREAM_EMAIL_DOMAIN"
            ),
        ) from exc
    return template


def _trusted_upstream_required_jwt_setting(value: str | None, env_name: str) -> str:
    if value is None:
        raise HTTPException(
            status_code=500,
            detail=f"Trusted upstream auth is enabled but {env_name} is not set",
        )
    return value


async def _verified_trusted_upstream_jwt_claims(
    request: Request,
    jwt_client: PyJWKClient,
    *,
    header: str,
    audience: str,
    issuer: str,
    required_claims: tuple[str, ...],
    invalid_detail: str,
    algorithms: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Verify one configured upstream JWT and return its claims."""
    token = (request.headers.get(header) or "").strip()
    if not token:
        raise HTTPException(status_code=401, detail=f"Missing trusted upstream JWT header: {header}")
    if len(token.encode("utf-8")) > _JWT_MAX_BYTES:
        raise HTTPException(status_code=401, detail=invalid_detail)

    try:
        signing_key = await asyncio.to_thread(jwt_client.get_signing_key_from_jwt, token)
        accepted_algorithms = algorithms
        if accepted_algorithms is None:
            algorithm = signing_key.algorithm_name
            if algorithm is None:
                raise jwt.InvalidTokenError
            accepted_algorithms = (algorithm,)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=accepted_algorithms,
            audience=audience,
            issuer=issuer,
            options={"require": list(required_claims)},
        )
    except PyJWTError as exc:
        raise HTTPException(status_code=401, detail=invalid_detail) from exc
    return claims


async def _verified_trusted_upstream_jwt_identity(
    request: Request,
    settings: _TrustedUpstreamAuthSettings,
    jwt_client: PyJWKClient | None,
) -> _TrustedUpstreamJwtIdentity:
    """Return the identity claims the gateway signed for this request."""
    header = _trusted_upstream_required_jwt_setting(settings.jwt_header, "MINDROOM_TRUSTED_UPSTREAM_JWT_HEADER")
    audience = _trusted_upstream_required_jwt_setting(settings.audience, "MINDROOM_TRUSTED_UPSTREAM_JWT_AUDIENCE")
    issuer = _trusted_upstream_required_jwt_setting(settings.issuer, "MINDROOM_TRUSTED_UPSTREAM_JWT_ISSUER")
    _trusted_upstream_required_jwt_setting(settings.jwks_url, "MINDROOM_TRUSTED_UPSTREAM_JWKS_URL")
    if jwt_client is None:
        raise HTTPException(
            status_code=500,
            detail="Trusted upstream auth is enabled but JWKS validation is not configured",
        )

    required_claims = ["exp", "iss", "aud", settings.email_claim]
    if settings.user_id_claim is not None:
        required_claims.append(settings.user_id_claim)
    if settings.matrix_user_id_claim is not None:
        required_claims.append(settings.matrix_user_id_claim)
    claims = await _verified_trusted_upstream_jwt_claims(
        request,
        jwt_client,
        header=header,
        audience=audience,
        issuer=issuer,
        required_claims=tuple(required_claims),
        invalid_detail="Invalid trusted upstream JWT",
    )

    email = _trusted_upstream_jwt_string_claim(claims, settings.email_claim)
    user_id = (
        _trusted_upstream_jwt_string_claim(claims, settings.user_id_claim)
        if settings.user_id_claim is not None
        else None
    )
    matrix_user_id = (
        _trusted_upstream_jwt_string_claim(claims, settings.matrix_user_id_claim)
        if settings.matrix_user_id_claim is not None
        else None
    )
    return _TrustedUpstreamJwtIdentity(email=email, user_id=user_id, matrix_user_id=matrix_user_id)


def _trusted_upstream_jwt_string_claim(claims: dict[str, Any], claim_name: str) -> str:
    claim = claims.get(claim_name)
    if not isinstance(claim, str) or not claim.strip():
        raise HTTPException(status_code=401, detail="Invalid trusted upstream JWT")
    return claim.strip()


def _trusted_upstream_matrix_user_id(
    settings: _TrustedUpstreamAuthSettings,
    identity: _TrustedUpstreamJwtIdentity,
) -> str | None:
    """Return the signed Matrix claim, else the Matrix user the email template names, else None."""
    if settings.matrix_user_id_claim is not None:
        matrix_user_id = try_parse_historical_matrix_user_id(identity.matrix_user_id)
        if matrix_user_id is None:
            raise HTTPException(status_code=401, detail="Invalid trusted upstream Matrix user id")
        return matrix_user_id
    template = _validated_trusted_upstream_email_to_matrix_template(settings)
    if template is None:
        return None
    try:
        return matrix_user_id_from_email(identity.email, template, settings.email_domain)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Invalid trusted upstream email identity") from exc


async def _trusted_upstream_auth_user(
    request: Request,
    settings: _TrustedUpstreamAuthSettings,
    jwt_client: PyJWKClient | None = None,
) -> dict[str, Any] | None:
    """Return the browser user the gateway's signed assertion names, when trusted upstream auth is on."""
    if not settings.enabled:
        return None
    identity = await _verified_trusted_upstream_jwt_identity(request, settings, jwt_client)
    auth_user: dict[str, Any] = {
        "user_id": identity.user_id or identity.email,
        "email": identity.email,
        "auth_source": "trusted_upstream",
    }
    matrix_user_id = _trusted_upstream_matrix_user_id(settings, identity)
    if matrix_user_id is not None:
        auth_user["matrix_user_id"] = matrix_user_id
    return auth_user


def _supabase_auth_error_class() -> type[Exception]:
    """Return Supabase's AuthError class for narrow exception handling at the auth boundary."""
    return cast("type[Exception]", importlib.import_module("supabase_auth.errors").AuthError)


async def _validate_supabase_token(token: str, auth_state: ApiAuthState) -> _SupabaseUserProtocol | None:
    """Validate a Supabase access token and return the authenticated user."""
    if auth_state.supabase_auth is None:
        return None
    # Supabase access tokens are JWTs, so any other bearer value is rejected without a network call.
    if len(token) > _JWT_MAX_BYTES or _SUPABASE_TOKEN_SHAPE.fullmatch(token) is None:
        return None

    try:
        async with _supabase_token_checks:
            response = await asyncio.to_thread(auth_state.supabase_auth.auth.get_user, token)
    except _supabase_auth_error_class():
        return None

    if not response or not response.user:
        return None

    return response.user


def _verified_platform_sso_claims(
    request: Request,
    settings: _ApiAuthSettings,
    token: str,
    *,
    token_type: str,
    required_claims: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Verify a platform login ticket or session signed with this instance's own SSO key."""
    audience = deployment_origin(settings.public_url, str(request.base_url))
    if settings.platform_sso_secret is None or audience is None:
        raise HTTPException(status_code=401, detail="Missing or invalid credentials")
    try:
        claims = jwt.decode(
            token,
            settings.platform_sso_secret,
            algorithms=["HS256"],
            audience=audience,
            leeway=_PLATFORM_SSO_CLOCK_SKEW_SECONDS,
            options={"require": ["typ", "aud", "sub", "iat", "exp", *required_claims]},
        )
    except PyJWTError as exc:
        raise HTTPException(status_code=401, detail="Invalid platform login") from exc
    subject = claims["sub"]
    email = claims.get("email")
    if (
        claims["typ"] != token_type
        or not isinstance(subject, str)
        or not subject
        or (email is not None and not isinstance(email, str))
    ):
        raise HTTPException(status_code=401, detail="Invalid platform login")
    return claims


async def _platform_user_identity(
    request: Request,
    authorization: str | None,
    auth_state: ApiAuthState,
) -> tuple[str, str | None]:
    """Return the user from a Supabase bearer token or this instance's platform session cookie."""
    bearer_token = _extract_bearer_token(authorization)
    if bearer_token is not None:
        user = await _validate_supabase_token(bearer_token, auth_state)
        if user is None:
            raise HTTPException(status_code=401, detail="Invalid token")
        return user.id, user.email

    session_token = request.cookies.get(_PLATFORM_SESSION_COOKIE_NAME)
    if not session_token:
        raise HTTPException(status_code=401, detail="Missing or invalid credentials")
    claims = _verified_platform_sso_claims(
        request,
        auth_state.settings,
        session_token,
        token_type=_PLATFORM_SESSION_TYPE,
    )
    return claims["sub"], claims.get("email")


def _consume_platform_sso_ticket(ticket_id: object, expires_at: int) -> None:
    """Accept each platform login ticket once while it can still verify."""
    now = time.time()
    for used_id, used_expires_at in list(_used_platform_sso_ticket_ids.items()):
        if used_expires_at + _PLATFORM_SSO_CLOCK_SKEW_SECONDS < now:
            del _used_platform_sso_ticket_ids[used_id]
    if not isinstance(ticket_id, str) or not ticket_id or ticket_id in _used_platform_sso_ticket_ids:
        raise HTTPException(status_code=401, detail="Invalid platform login")
    _used_platform_sso_ticket_ids[ticket_id] = expires_at


def _bind_authenticated_request_snapshot(request: Request) -> ApiSnapshot:
    """Bind one coherent auth/runtime/config snapshot to the request."""
    existing = request_api_snapshot(request)
    bound_auth_state = cast("ApiAuthState | None", existing.auth_state) if existing is not None else None
    if (
        existing is not None
        and bound_auth_state is not None
        and bound_auth_state.runtime_paths == existing.runtime_paths
    ):
        return existing

    app_state = config_lifecycle.app_state(request.app)
    api_state = config_lifecycle.require_api_state(request.app)
    with api_state.config_lock:
        current = api_state.snapshot
        auth_state = cast("ApiAuthState | None", current.auth_state)
        if auth_state is None or auth_state.runtime_paths != current.runtime_paths:
            settings = _build_auth_settings(current.runtime_paths, account_id=app_state.api_auth_account_id)
            auth_state = ApiAuthState(
                runtime_paths=current.runtime_paths,
                settings=settings,
                supabase_auth=_init_supabase_auth(current.runtime_paths, settings),
                trusted_upstream_jwt_client=_build_trusted_upstream_jwt_client(settings.trusted_upstream),
            )
            current = replace(current, auth_state=auth_state)
            api_state.snapshot = current
        return store_request_api_snapshot(request, current)


def _request_auth_state(request: Request) -> ApiAuthState:
    """Return the request-bound auth state when available."""
    snapshot = request_api_snapshot(request)
    if snapshot is None:
        return _app_auth_state(request.app)
    auth_state = cast("ApiAuthState | None", snapshot.auth_state)
    if auth_state is None or auth_state.runtime_paths != snapshot.runtime_paths:
        return cast("ApiAuthState", _bind_authenticated_request_snapshot(request).auth_state)
    return auth_state


async def request_has_frontend_access(request: Request) -> bool:
    """Return whether the current request may load the dashboard UI."""
    authorization = request.headers.get("authorization")
    snapshot = _bind_authenticated_request_snapshot(request)
    auth_state = cast("ApiAuthState", snapshot.auth_state)
    if _is_connections_path(request.scope["path"]):
        # The portal page is a static shell that signs itself in; its APIs authenticate every request.
        return True
    try:
        auth_user = await authenticate_user(request, authorization, allow_public_paths=False)
    except HTTPException as exc:
        # Only a missing or invalid credential falls through to the login page.
        if exc.status_code not in {401, 403}:
            raise
        return False
    if auth_state.settings.trusted_upstream.enabled:
        _require_connections_route_authorized(request, auth_user, snapshot)
    return True


def sanitize_next_path(next_path: str | None) -> str:
    """Normalize redirect targets to an absolute in-app path."""
    if not next_path or not next_path.startswith("/") or _leaves_dashboard_origin(next_path):
        return "/"
    return next_path


def _leaves_dashboard_origin(next_path: str) -> bool:
    """Return whether a browser may resolve one target outside the dashboard origin."""
    candidate = next_path
    for _ in range(_REDIRECT_TARGET_DECODE_PASSES):
        if _is_offsite_candidate(candidate):
            return True
        decoded = unquote(candidate)
        if decoded == candidate:
            return False
        candidate = decoded
    return _is_offsite_candidate(candidate)


def _is_offsite_candidate(candidate: str) -> bool:
    """Return whether one decoded target reads as protocol-relative to a browser's URL parser."""
    # The WHATWG URL parser removes ASCII tab and newline before parsing, so "/\t/evil.com"
    # resolves to the scheme-relative "//evil.com". Reject every C0 control character instead of
    # replaying that removal, since no in-app path needs one.
    if any(character < " " for character in candidate):
        return True
    return candidate.replace("\\", "/").startswith("//")


def _request_path_with_query(request: Request) -> str:
    path = request.url.path
    query = request.url.query
    return f"{path}?{query}" if query else path


_DASHBOARD_CORS_ALLOWED_ORIGINS_ENV = "MINDROOM_DASHBOARD_CORS_ALLOWED_ORIGINS"
_DASHBOARD_CORS_ALLOW_ALL_ORIGINS_ENV = "MINDROOM_DASHBOARD_CORS_ALLOW_ALL_ORIGINS"
_DEFAULT_DASHBOARD_CORS_ALLOWED_ORIGINS = (
    "http://localhost:3003",
    "http://localhost:5173",
    "http://127.0.0.1:3003",
    "http://127.0.0.1:5173",
)


def _hosted_browser_origins(runtime_paths: RuntimePaths) -> list[str]:
    """Return hosted browser origins allowed to make credentialed API calls."""
    return list(
        dict.fromkeys(
            origin
            for origin in (
                normalized_origin(runtime_paths.env_value("MINDROOM_PUBLIC_URL")),
                normalized_origin(runtime_paths.env_value("MINDROOM_PLATFORM_LOGIN_URL")),
            )
            if origin is not None
        ),
    )


def dashboard_cors_origins(runtime_paths: RuntimePaths) -> tuple[str, ...]:
    """Return the browser origins the dashboard API serves cross-origin; `("*",)` when every origin is allowed.

    `MINDROOM_DASHBOARD_CORS_ALLOW_ALL_ORIGINS` allows every origin. Otherwise
    `MINDROOM_DASHBOARD_CORS_ALLOWED_ORIGINS` lists them; unset, a hosted instance allows its own origins and any
    other instance the local development servers.
    """
    if runtime_paths.env_flag(_DASHBOARD_CORS_ALLOW_ALL_ORIGINS_ENV):
        return ("*",)
    configured_origins = runtime_paths.env_value(_DASHBOARD_CORS_ALLOWED_ORIGINS_ENV)
    if configured_origins is None:
        hosted_origins = tuple(_hosted_browser_origins(runtime_paths))
        if hosted_origins:
            return hosted_origins
        return _DEFAULT_DASHBOARD_CORS_ALLOWED_ORIGINS
    origins = tuple(origin for origin in (part.strip() for part in configured_origins.split(",")) if origin)
    return origins or _DEFAULT_DASHBOARD_CORS_ALLOWED_ORIGINS


def _platform_redirect_target(request: Request, auth_settings: _ApiAuthSettings, next_path: str | None) -> str:
    origin = normalized_origin(auth_settings.public_url)
    if origin is None:
        return str(request.url)
    path = sanitize_next_path(next_path or _request_path_with_query(request))
    return f"{origin}{path}"


def login_redirect_for_request(request: Request, *, next_path: str | None = None) -> RedirectResponse | None:
    """Return the dashboard login redirect for one browser request when configured."""
    auth_settings = _request_auth_state(request).settings
    if auth_settings.trusted_upstream.enabled:
        return None
    if (
        _platform_auth_credentials(auth_settings) is not None
        and auth_settings.platform_sso_url
        and auth_settings.platform_sso_secret
    ):
        redirect_to = quote(_platform_redirect_target(request, auth_settings, next_path), safe="")
        return RedirectResponse(f"{auth_settings.platform_sso_url}?redirect_to={redirect_to}")
    if auth_settings.mindroom_api_key:
        login_target = sanitize_next_path(next_path or _request_path_with_query(request))
        return RedirectResponse(f"/login?{urlencode({'next': login_target})}")
    return None


def _render_standalone_login_page(
    next_path: str,
    runtime_paths: RuntimePaths,
) -> str:
    """Return the standalone dashboard login page."""
    next_path_js = (
        json.dumps(next_path)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )
    env_path = html.escape(str(runtime_paths.env_path))
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>MindRoom Login</title>
  <style>
    body {{
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      background: #f6f4ef;
      color: #1f2523;
      font-family: system-ui, sans-serif;
    }}
    form {{
      width: min(24rem, calc(100vw - 2rem));
      padding: 1.5rem;
      border: 1px solid #d2cbbd;
      border-radius: 1rem;
      background: #fffdf7;
      box-shadow: 0 1rem 3rem rgba(31, 37, 35, 0.08);
    }}
    h1 {{
      margin: 0 0 0.75rem;
      font-size: 1.4rem;
    }}
    p {{
      margin: 0 0 1rem;
      color: #5d655f;
    }}
    code {{
      padding: 0.1rem 0.3rem;
      border-radius: 0.35rem;
      background: #f1ece0;
      font-size: 0.92em;
    }}
    input, button {{
      box-sizing: border-box;
      width: 100%;
      border-radius: 0.75rem;
      font: inherit;
    }}
    input {{
      margin-bottom: 0.75rem;
      padding: 0.8rem 0.9rem;
      border: 1px solid #c7cfc7;
      background: white;
    }}
    button {{
      padding: 0.85rem 1rem;
      border: 0;
      background: #1f2523;
      color: white;
      cursor: pointer;
    }}
    #error {{
      min-height: 1.25rem;
      margin-top: 0.75rem;
      color: #b42318;
    }}
  </style>
</head>
<body>
  <form id="login-form">
    <h1>MindRoom Dashboard</h1>
    <p>Enter the dashboard API key to continue.</p>
    <p>Find it in <code>{env_path}</code> as <code>MINDROOM_API_KEY=...</code>.</p>
    <input id="api-key" name="api-key" type="password" autocomplete="current-password" autofocus>
    <button type="submit">Continue</button>
    <div id="error" role="alert"></div>
  </form>
  <script>
    const nextPath = {next_path_js};
    const form = document.getElementById("login-form");
    const input = document.getElementById("api-key");
    const error = document.getElementById("error");

    form.addEventListener("submit", async (event) => {{
      event.preventDefault();
      error.textContent = "";
      const response = await fetch("/api/auth/session", {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify({{ api_key: input.value }}),
      }});
      if (response.ok) {{
        const target = new URL(nextPath, window.location.origin);
        window.location.assign(target.origin === window.location.origin ? target.href : "/");
        return;
      }}
      error.textContent = "Invalid API key.";
      input.select();
    }});
  </script>
</body>
</html>"""


def _is_connections_path(path: str) -> bool:
    """Recognize only the dedicated personal frontend and API path segments."""
    return any(path == prefix or path.startswith(f"{prefix}/") for prefix in ("/connections", "/api/connections"))


def _is_oauth_popup_path(path: str, actions: frozenset[str]) -> bool:
    """Recognize exactly `/api/oauth/{provider}/{action}` for one of `actions`."""
    parts = path.split("/")
    return len(parts) == 5 and parts[1:3] == ["api", "oauth"] and bool(parts[3]) and parts[4] in actions


def allow_connections_session_for_oauth_callback(request: Request) -> None:
    """Let this OAuth callback authenticate with the Connections session, because a session started its flow."""
    request.scope[_CONNECTIONS_SESSION_CALLBACK_SCOPE_KEY] = True


def _connections_session_path_honored(request: Request) -> bool:
    """Honor sessions on portal paths and OAuth success pages, and on callbacks only for session-started flows."""
    path = request.scope["path"]
    if _is_oauth_popup_path(path, frozenset({"callback"})):
        return request.scope.get(_CONNECTIONS_SESSION_CALLBACK_SCOPE_KEY) is True
    return _is_connections_path(path) or _is_oauth_popup_path(path, frozenset({"success"}))


def _connections_session_auth_user(request: Request) -> dict[str, Any] | None:
    """Return the Connections session identity.

    It is honored on portal paths, and on dashboard routes for an administrator whose sign-in the homeserver bound
    to this deployment, outside hosted platform mode and never on OAuth callbacks.
    """
    snapshot = _bind_authenticated_request_snapshot(request)
    public_url = cast("ApiAuthState", snapshot.auth_state).settings.public_url
    cookie = connections_session_cookie(deployment_origin(public_url, str(request.base_url)))
    token = request.cookies.get(cookie)
    session = read_connections_session(token, now=time.time()) if token else None
    if session is None:
        return None
    if not _connections_session_path_honored(request) and not (
        session.dashboard
        and not _is_oauth_popup_path(request.scope["path"], frozenset({"callback"}))
        and cast("ApiAuthState", snapshot.auth_state).supabase_auth is None
        and is_administrator(session.matrix_user_id, snapshot)
    ):
        return None
    matrix_user_id = session.matrix_user_id
    return {"user_id": matrix_user_id, "email": None, "matrix_user_id": matrix_user_id, "auth_source": "matrix_openid"}


def is_administrator(matrix_user_id: object, snapshot: ApiSnapshot) -> bool:
    """Return whether a signed Matrix user is a platform administrator in the snapshot's config."""
    return (
        isinstance(matrix_user_id, str)
        and snapshot.runtime_config is not None
        and is_platform_administrator(matrix_user_id, snapshot.runtime_config, snapshot.runtime_paths)
    )


def _require_connections_route_authorized(
    request: Request,
    auth_user: dict[str, Any],
    snapshot: ApiSnapshot,
) -> None:
    """Keep trusted personal users outside administrator routes while `MINDROOM_CONNECTIONS_AGENT` is set."""
    if not _env_text(snapshot.runtime_paths, "MINDROOM_CONNECTIONS_AGENT"):
        return
    path = request.scope["path"]
    if _is_connections_path(path):
        return
    if _is_oauth_popup_path(path, frozenset({"callback", "success", "reset"})):
        # These handlers independently validate their state or reset capability.
        return
    if is_administrator(auth_user.get("matrix_user_id"), snapshot):
        return
    if _is_browser_page_request(request):
        # A user who opens a dashboard page lands on their own page, which says why, instead of a JSON refusal.
        raise HTTPException(
            status_code=303,
            detail="Administrator access required",
            headers={"Location": "/connections?notice=admin-only"},
        )
    raise HTTPException(status_code=403, detail="Administrator access required")


def _is_browser_page_request(request: Request) -> bool:
    """Recognize a browser loading a page: a GET that accepts HTML, outside the API."""
    path = request.scope["path"]
    return (
        request.method == "GET"
        and "text/html" in request.headers.get("accept", "").lower()
        and path != "/api"
        and not path.startswith("/api/")
    )


async def require_connections_user(request: Request) -> dict[str, Any]:
    """Authenticate a Connections user through a portal session or a signed upstream Matrix identity."""
    auth_user = _connections_session_auth_user(request)
    if auth_user is not None:
        request.scope["auth_user"] = auth_user
    else:
        settings = _request_auth_state(request).settings.trusted_upstream
        if not settings.enabled:
            # Without a signed upstream identity, only a portal session can authenticate, so ask the page to sign in.
            raise HTTPException(status_code=401, detail="Connections sign-in required")
        auth_user = await _require_signed_matrix_user(request, label="Connections")
    page_user = request.headers.get(_CONNECTIONS_USER_HEADER)
    if page_user is not None and page_user != auth_user["matrix_user_id"]:
        # Sessions and upstream identities are browser-wide, so a page showing another account must reload.
        raise HTTPException(status_code=401, detail="This page shows another account. Reload this page.")
    return auth_user


def trusted_upstream_email_to_matrix_user_id(request: Request) -> Callable[[str], str | None] | None:
    """Return how a signed upstream login turns an email into its Matrix user, or None when the email does not decide it."""
    settings = _request_auth_state(request).settings.trusted_upstream
    # In `_trusted_upstream_matrix_user_id`, a signed Matrix claim wins over the email template, which then names no one.
    if not settings.enabled or settings.matrix_user_id_claim is not None:
        return None
    template = _validated_trusted_upstream_email_to_matrix_template(settings)
    if template is None:
        return None

    def matrix_user_id(email: str) -> str | None:
        try:
            return matrix_user_id_from_email(email, template, settings.email_domain)
        except ValueError:
            return None

    return matrix_user_id


async def require_personal_user(request: Request) -> dict[str, Any]:
    """Authenticate personal read APIs without granting administrator access."""
    return await _require_signed_matrix_user(request, label="Personal APIs")


async def require_usage_service(request: Request) -> None:
    """Authenticate the dedicated usage-export service without creating a user identity."""
    await _require_service_jwt(
        request,
        audience_env="MINDROOM_USAGE_SERVICE_JWT_AUDIENCE",
        client_id_env="MINDROOM_USAGE_SERVICE_CLIENT_ID",
        unconfigured_detail="Usage export service authentication is not configured",
        invalid_detail="Invalid usage service JWT",
    )


async def require_budget_service(request: Request) -> None:
    """Authenticate the dedicated budget service without creating a user identity."""
    await _require_service_jwt(
        request,
        audience_env="MINDROOM_BUDGET_SERVICE_JWT_AUDIENCE",
        client_id_env="MINDROOM_BUDGET_SERVICE_CLIENT_ID",
        unconfigured_detail="Budget service authentication is not configured",
        invalid_detail="Invalid budget service JWT",
    )


async def _require_service_jwt(
    request: Request,
    *,
    audience_env: str,
    client_id_env: str,
    unconfigured_detail: str,
    invalid_detail: str,
) -> None:
    """Require a trusted-upstream service assertion for one dedicated audience and client ID."""
    auth_state = cast("ApiAuthState", _bind_authenticated_request_snapshot(request).auth_state)
    settings = auth_state.settings.trusted_upstream
    audience = _env_text(auth_state.runtime_paths, audience_env)
    client_id = _env_text(auth_state.runtime_paths, client_id_env)
    if (
        not settings.enabled
        or settings.jwt_header is None
        or settings.jwks_url is None
        or settings.audience is None
        or settings.issuer is None
        or audience is None
        or client_id is None
        or auth_state.trusted_upstream_jwt_client is None
    ):
        raise HTTPException(status_code=503, detail=unconfigured_detail)

    claims = await _verified_trusted_upstream_jwt_claims(
        request,
        auth_state.trusted_upstream_jwt_client,
        header=settings.jwt_header,
        audience=audience,
        issuer=settings.issuer,
        algorithms=("RS256",),
        required_claims=("exp", "iat", "iss", "aud", "type", "common_name", "sub"),
        invalid_detail=invalid_detail,
    )

    if (
        claims.get("type") != "app"
        or claims.get("sub") != ""
        or not isinstance(claims.get("common_name"), str)
        or claims["common_name"] != client_id
    ):
        raise HTTPException(status_code=401, detail=invalid_detail)


async def _require_signed_matrix_user(request: Request, *, label: str) -> dict[str, Any]:
    auth_state = cast("ApiAuthState", _bind_authenticated_request_snapshot(request).auth_state)
    settings = auth_state.settings.trusted_upstream
    if not settings.enabled:
        raise HTTPException(status_code=403, detail=f"{label} require trusted signed authentication")
    auth_user = await _trusted_upstream_auth_user(request, settings, auth_state.trusted_upstream_jwt_client)
    matrix_user_id = auth_user.get("matrix_user_id") if auth_user is not None else None
    if not isinstance(matrix_user_id, str) or try_parse_historical_matrix_user_id(matrix_user_id) is None:
        raise HTTPException(status_code=403, detail=f"{label} require a verified Matrix identity")
    request.scope["auth_user"] = auth_user
    return cast("dict[str, Any]", auth_user)


def require_same_origin(
    request: Request,
    expected_origin: str,
    *,
    detail: str = "Browser changes require a same-origin request",
    headers: dict[str, str] | None = None,
) -> None:
    """Reject browser mutations from another origin, including contradictory fetch metadata."""
    if request.headers.get("origin") != expected_origin or request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, detail, headers=headers)


def _require_browser_mutation_origin(
    request: Request,
    settings: _ApiAuthSettings,
    validated_authorization: str | None = None,
) -> None:
    if (
        request.method in {"GET", "HEAD", "OPTIONS", "TRACE"}
        or _extract_bearer_token(validated_authorization) is not None
    ):
        return
    origin = deployment_origin(settings.public_url, str(request.base_url))
    if origin is None:
        raise HTTPException(403, "Browser changes require a valid public origin")
    require_same_origin(request, origin)


async def authenticate_user(
    request: Request,
    authorization: str | None = Header(None),
    *,
    allow_public_paths: bool = True,
) -> dict[str, Any]:
    """Authenticate the request, enforcing account ownership and browser mutation origin."""
    snapshot = _bind_authenticated_request_snapshot(request)
    auth_state = cast("ApiAuthState", snapshot.auth_state)
    session_user = _connections_session_auth_user(request)
    if session_user is not None:
        _require_browser_mutation_origin(request, auth_state.settings)
        request.scope["auth_user"] = session_user
        return session_user

    mindroom_api_key = auth_state.settings.mindroom_api_key
    trusted_auth_user = await _trusted_upstream_auth_user(
        request,
        auth_state.settings.trusted_upstream,
        auth_state.trusted_upstream_jwt_client,
    )
    if trusted_auth_user is not None:
        _require_browser_mutation_origin(request, auth_state.settings)
        request.scope["auth_user"] = trusted_auth_user
        return trusted_auth_user

    if auth_state.supabase_auth is None:
        if allow_public_paths and _is_standalone_public_path(request.scope["path"]):
            auth_user = {"user_id": "standalone", "email": None}
            request.scope["auth_user"] = auth_user
            return auth_user

        if mindroom_api_key:
            token = _extract_bearer_token(authorization) or request.cookies.get(_STANDALONE_AUTH_COOKIE_NAME) or None
            if token is None:
                raise HTTPException(status_code=401, detail="Missing or invalid credentials")
            if not secrets.compare_digest(token, mindroom_api_key):
                raise HTTPException(status_code=401, detail="Invalid API key")
            _require_browser_mutation_origin(request, auth_state.settings, authorization)
        else:
            # Without a credential, only where the request comes from authorizes it.
            rejection = open_access_rejection(request.headers, request.method, snapshot.runtime_paths)
            if rejection is not None:
                raise HTTPException(rejection.status_code, rejection.detail)
        auth_user = {"user_id": "standalone", "email": None}
        request.scope["auth_user"] = auth_user
        return auth_user

    user_id, email = await _platform_user_identity(request, authorization, auth_state)
    if auth_state.settings.account_id and user_id != auth_state.settings.account_id:
        raise HTTPException(status_code=403, detail="Forbidden")

    _require_browser_mutation_origin(request, auth_state.settings, authorization)
    auth_user = {"user_id": user_id, "email": email}
    request.scope["auth_user"] = auth_user
    return auth_user


async def verify_user(
    request: Request,
    authorization: str | None = Header(None),
    *,
    allow_public_paths: bool = True,
) -> dict[str, Any]:
    """Authenticate a dashboard request and enforce its route access policy."""
    auth_user = await authenticate_user(request, authorization, allow_public_paths=allow_public_paths)
    snapshot = _bind_authenticated_request_snapshot(request)
    if cast("ApiAuthState", snapshot.auth_state).settings.trusted_upstream.enabled:
        _require_connections_route_authorized(request, auth_user, snapshot)
    return auth_user


@router.post("/api/auth/session", include_in_schema=False)
async def create_auth_session(request: Request, payload: _AuthSessionRequest, response: Response) -> dict[str, bool]:
    """Set a same-origin cookie for standalone dashboard auth."""
    mindroom_api_key = _app_auth_state(request.app).settings.mindroom_api_key
    if not mindroom_api_key:
        raise HTTPException(status_code=404, detail="Dashboard auth is not enabled")

    if not payload.api_key or not secrets.compare_digest(payload.api_key, mindroom_api_key):
        raise HTTPException(status_code=401, detail="Invalid API key")

    response.set_cookie(
        key=_STANDALONE_AUTH_COOKIE_NAME,
        value=payload.api_key,
        path="/",
        secure=request.url.scheme == "https",
        httponly=True,
        samesite="lax",
    )
    return {"success": True}


@router.delete("/api/auth/session", include_in_schema=False)
async def clear_auth_session(response: Response) -> dict[str, bool]:
    """Clear the standalone dashboard auth cookie."""
    response.delete_cookie(key=_STANDALONE_AUTH_COOKIE_NAME, path="/")
    return {"success": True}


@router.get("/api/auth/platform-sso", include_in_schema=False)
async def complete_platform_sso(request: Request, ticket: str, next: str = "/") -> RedirectResponse:  # noqa: A002
    """Exchange a single-use platform login ticket for a host-only dashboard session."""
    auth_state = cast("ApiAuthState", _bind_authenticated_request_snapshot(request).auth_state)
    settings = auth_state.settings
    if auth_state.supabase_auth is None or settings.trusted_upstream.enabled or settings.platform_sso_secret is None:
        raise HTTPException(status_code=404, detail="Platform login is not enabled")

    claims = _verified_platform_sso_claims(
        request,
        settings,
        ticket,
        token_type=_PLATFORM_SSO_TICKET_TYPE,
        required_claims=("jti",),
    )
    _consume_platform_sso_ticket(claims["jti"], int(claims["exp"]))
    if settings.account_id and claims["sub"] != settings.account_id:
        raise HTTPException(status_code=403, detail="Forbidden")

    now = int(time.time())
    session_token = jwt.encode(
        {
            "typ": _PLATFORM_SESSION_TYPE,
            "aud": claims["aud"],
            "sub": claims["sub"],
            "email": claims.get("email"),
            "iat": now,
            "exp": now + _PLATFORM_SESSION_MAX_AGE_SECONDS,
        },
        settings.platform_sso_secret,
        algorithm="HS256",
    )
    response = RedirectResponse(sanitize_next_path(next))
    response.set_cookie(
        key=_PLATFORM_SESSION_COOKIE_NAME,
        value=session_token,
        max_age=_PLATFORM_SESSION_MAX_AGE_SECONDS,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )
    return response


@router.get("/login", include_in_schema=False)
async def standalone_login(request: Request, next: str = "/") -> Response:  # noqa: A002
    """Render the standalone dashboard login form when API-key auth is enabled."""
    if not cast("ApiAuthState", _bind_authenticated_request_snapshot(request).auth_state).settings.mindroom_api_key:
        raise HTTPException(status_code=404, detail="Not found")

    next_path = sanitize_next_path(next)
    if await request_has_frontend_access(request):
        return RedirectResponse(next_path)

    return HTMLResponse(_render_standalone_login_page(next_path, config_lifecycle.api_runtime_paths(request)))
