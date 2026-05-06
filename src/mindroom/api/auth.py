# ruff: noqa: D100
from __future__ import annotations

import html
import importlib
import json
import secrets
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Protocol, cast
from urllib.parse import quote, urlencode

import jwt
from fastapi import APIRouter, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from jwt import PyJWKClient, PyJWTError
from pydantic import BaseModel

from mindroom.api import config_lifecycle
from mindroom.api.config_lifecycle import ApiSnapshot
from mindroom.api.config_lifecycle import request_snapshot as request_api_snapshot
from mindroom.api.config_lifecycle import store_request_snapshot as store_request_api_snapshot
from mindroom.matrix.identity import try_parse_historical_matrix_user_id
from mindroom.tool_system.dependencies import auto_install_enabled, auto_install_tool_extra

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths

router = APIRouter(tags=["auth"])

_PLATFORM_AUTH_COOKIE_NAME = "mindroom_jwt"
_STANDALONE_AUTH_COOKIE_NAME = "mindroom_api_key"
_TRUSTED_UPSTREAM_JWKS_CACHE_SECONDS = 60
_TRUSTED_UPSTREAM_JWKS_TIMEOUT_SECONDS = 5
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
class _TrustedUpstreamJwtSettings:
    """Signed assertion settings for trusted-upstream auth."""

    require_jwt: bool = False
    header: str | None = None
    jwks_url: str | None = None
    audience: str | None = None
    issuer: str | None = None
    email_claim: str = "email"


@dataclass(frozen=True)
class _TrustedUpstreamAuthSettings:
    """Trusted reverse-proxy/browser identity settings for hosted deployments."""

    enabled: bool = False
    user_id_header: str | None = None
    email_header: str | None = None
    matrix_user_id_header: str | None = None
    email_to_matrix_user_id_template: str | None = None
    jwt: _TrustedUpstreamJwtSettings = field(default_factory=_TrustedUpstreamJwtSettings)


@dataclass(frozen=True)
class _ApiAuthSettings:
    """Dashboard authentication settings for one runtime."""

    platform_login_url: str | None
    supabase_url: str | None
    supabase_anon_key: str | None
    account_id: str | None
    mindroom_api_key: str | None
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
        platform_login_url=runtime_paths.env_value("MINDROOM_PLATFORM_LOGIN_URL"),
        supabase_url=runtime_paths.env_value("SUPABASE_URL"),
        supabase_anon_key=runtime_paths.env_value("SUPABASE_ANON_KEY"),
        account_id=account_id,
        mindroom_api_key=runtime_paths.env_value("MINDROOM_API_KEY"),
        trusted_upstream=_build_trusted_upstream_auth_settings(runtime_paths),
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
        user_id_header=_env_text(runtime_paths, "MINDROOM_TRUSTED_UPSTREAM_USER_ID_HEADER"),
        email_header=_env_text(runtime_paths, "MINDROOM_TRUSTED_UPSTREAM_EMAIL_HEADER"),
        matrix_user_id_header=_env_text(runtime_paths, "MINDROOM_TRUSTED_UPSTREAM_MATRIX_USER_ID_HEADER"),
        email_to_matrix_user_id_template=_env_text(
            runtime_paths,
            "MINDROOM_TRUSTED_UPSTREAM_EMAIL_TO_MATRIX_USER_ID_TEMPLATE",
        ),
        jwt=_TrustedUpstreamJwtSettings(
            require_jwt=runtime_paths.env_flag("MINDROOM_TRUSTED_UPSTREAM_REQUIRE_JWT"),
            header=_env_text(runtime_paths, "MINDROOM_TRUSTED_UPSTREAM_JWT_HEADER"),
            jwks_url=_env_text(runtime_paths, "MINDROOM_TRUSTED_UPSTREAM_JWKS_URL"),
            audience=_env_text(runtime_paths, "MINDROOM_TRUSTED_UPSTREAM_JWT_AUDIENCE"),
            issuer=_env_text(runtime_paths, "MINDROOM_TRUSTED_UPSTREAM_JWT_ISSUER"),
            email_claim=_env_text(runtime_paths, "MINDROOM_TRUSTED_UPSTREAM_JWT_EMAIL_CLAIM") or "email",
        ),
    )


def _build_trusted_upstream_jwt_client(settings: _TrustedUpstreamAuthSettings) -> PyJWKClient | None:
    """Return a short-lived JWKS cache for strict trusted-upstream auth."""
    jwt_settings = settings.jwt
    if not settings.enabled or not jwt_settings.require_jwt or jwt_settings.jwks_url is None:
        return None
    return PyJWKClient(
        jwt_settings.jwks_url,
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
            supabase_auth=_init_supabase_auth(
                snapshot.runtime_paths,
                settings.supabase_url,
                settings.supabase_anon_key,
            ),
            trusted_upstream_jwt_client=_build_trusted_upstream_jwt_client(settings.trusted_upstream),
        )
        api_state.snapshot = replace(snapshot, auth_state=state)
        return state


def _init_supabase_auth(
    runtime_paths: RuntimePaths,
    supabase_url: str | None,
    supabase_anon_key: str | None,
) -> _SupabaseClientProtocol | None:
    """Initialize Supabase auth client when credentials are configured."""
    if not supabase_url or not supabase_anon_key:
        return None

    try:
        create_client = importlib.import_module("supabase").create_client
    except ModuleNotFoundError:
        disabled_hint = ""
        if not auto_install_enabled(runtime_paths):
            disabled_hint = " Auto-install is disabled by MINDROOM_NO_AUTO_INSTALL_TOOLS."
        if not auto_install_tool_extra("supabase", runtime_paths):
            msg = (
                "SUPABASE_URL and SUPABASE_ANON_KEY are set but the 'supabase' package is not available."
                f"{disabled_hint} Install it with: pip install 'mindroom[supabase]'"
            )
            raise ImportError(msg) from None
        create_client = importlib.import_module("supabase").create_client

    return cast("_SupabaseClientProtocol", create_client(supabase_url, supabase_anon_key))


def _extract_bearer_token(authorization: str | None) -> str | None:
    """Return the bearer token value from an Authorization header."""
    if not authorization or not authorization.startswith("Bearer "):
        return None
    token = authorization.removeprefix("Bearer ").strip()
    return token or None


def _is_standalone_public_path(path: str) -> bool:
    """Return whether one unauthenticated standalone callback path may enter its handler."""
    return path in _STANDALONE_PUBLIC_PATHS


def _get_request_token(
    request: Request,
    authorization: str | None,
    *,
    cookie_names: tuple[str, ...],
) -> str | None:
    """Return the request auth token from bearer auth or one of the allowed cookies."""
    bearer_token = _extract_bearer_token(authorization)
    if bearer_token:
        return bearer_token

    for cookie_name in cookie_names:
        cookie_value = request.cookies.get(cookie_name)
        if cookie_value:
            return cookie_value

    return None


def _get_configured_header(request: Request, header_name: str | None) -> str | None:
    """Return a stripped configured header value when present."""
    if header_name is None:
        return None
    value = request.headers.get(header_name)
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _trusted_upstream_email_localpart(email: str) -> str | None:
    """Return the localpart from a trusted email identity."""
    if email.count("@") != 1:
        return None
    localpart, separator, domain = email.partition("@")
    if not separator or not localpart or not domain:
        return None
    return localpart


def _validated_trusted_upstream_email_to_matrix_template(
    settings: _TrustedUpstreamAuthSettings,
) -> str | None:
    """Return the configured email-to-Matrix template after validating it."""
    template = settings.email_to_matrix_user_id_template
    if template is None:
        return None
    if settings.email_header is None:
        raise HTTPException(
            status_code=500,
            detail=(
                "Trusted upstream email-to-Matrix template is set but MINDROOM_TRUSTED_UPSTREAM_EMAIL_HEADER is not set"
            ),
        )
    if template.count("{localpart}") != 1:
        raise HTTPException(
            status_code=500,
            detail=("Trusted upstream email-to-Matrix template must contain exactly one {localpart} placeholder"),
        )
    return template


def _derive_trusted_upstream_matrix_user_id(
    settings: _TrustedUpstreamAuthSettings,
    email: str | None,
    template: str | None,
) -> str | None:
    """Derive a Matrix user ID from the trusted email header when configured."""
    if template is None:
        return None
    if email is None:
        raise HTTPException(
            status_code=401,
            detail=f"Missing trusted upstream email header: {settings.email_header}",
        )
    localpart = _trusted_upstream_email_localpart(email)
    if localpart is None:
        raise HTTPException(status_code=401, detail="Invalid trusted upstream email")
    derived = template.replace("{localpart}", localpart)
    parsed_matrix_user_id = try_parse_historical_matrix_user_id(derived)
    if parsed_matrix_user_id is None:
        raise HTTPException(status_code=401, detail="Invalid trusted upstream Matrix user id")
    return parsed_matrix_user_id


def _trusted_upstream_required_jwt_setting(value: str | None, env_name: str) -> str:
    if value is None:
        raise HTTPException(
            status_code=500,
            detail=f"Trusted upstream strict JWT auth is enabled but {env_name} is not set",
        )
    return value


def _verified_trusted_upstream_jwt_claim(
    request: Request,
    settings: _TrustedUpstreamAuthSettings,
    jwt_client: PyJWKClient | None,
) -> str | None:
    """Return the verified upstream identity claim when strict mode is enabled."""
    jwt_settings = settings.jwt
    if not jwt_settings.require_jwt:
        return None

    header = _trusted_upstream_required_jwt_setting(
        jwt_settings.header,
        "MINDROOM_TRUSTED_UPSTREAM_JWT_HEADER",
    )
    audience = _trusted_upstream_required_jwt_setting(
        jwt_settings.audience,
        "MINDROOM_TRUSTED_UPSTREAM_JWT_AUDIENCE",
    )
    issuer = _trusted_upstream_required_jwt_setting(
        jwt_settings.issuer,
        "MINDROOM_TRUSTED_UPSTREAM_JWT_ISSUER",
    )
    _trusted_upstream_required_jwt_setting(
        jwt_settings.jwks_url,
        "MINDROOM_TRUSTED_UPSTREAM_JWKS_URL",
    )
    if jwt_client is None:
        raise HTTPException(
            status_code=500,
            detail="Trusted upstream strict JWT auth is enabled but JWKS validation is not configured",
        )

    token = _get_configured_header(request, header)
    if token is None:
        raise HTTPException(status_code=401, detail=f"Missing trusted upstream JWT header: {header}")

    try:
        signing_key = jwt_client.get_signing_key_from_jwt(token)
        algorithm = signing_key.algorithm_name
        if algorithm is None:
            raise jwt.InvalidTokenError
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=[algorithm],
            audience=audience,
            issuer=issuer,
            options={"require": ["exp", "iss", "aud", jwt_settings.email_claim]},
        )
    except PyJWTError as exc:
        raise HTTPException(status_code=401, detail="Invalid trusted upstream JWT") from exc

    claim = claims.get(jwt_settings.email_claim)
    if not isinstance(claim, str) or not claim.strip():
        raise HTTPException(status_code=401, detail="Invalid trusted upstream JWT")
    return claim.strip()


def _verified_trusted_upstream_email(
    settings: _TrustedUpstreamAuthSettings,
    user_id: str,
    email: str | None,
    jwt_claim: str | None,
) -> str | None:
    """Return the trusted email after checking strict JWT identity consistency."""
    if jwt_claim is None:
        return email

    identity_value = email or user_id
    if not secrets.compare_digest(identity_value, jwt_claim):
        raise HTTPException(status_code=401, detail="Trusted upstream identity does not match JWT claim")
    if settings.email_header is None and email is None:
        return jwt_claim
    return email


def _trusted_upstream_auth_user(
    request: Request,
    settings: _TrustedUpstreamAuthSettings,
    jwt_client: PyJWKClient | None = None,
) -> dict[str, Any] | None:
    """Return the trusted-upstream auth user for this request when configured."""
    if not settings.enabled:
        return None
    if settings.user_id_header is None:
        raise HTTPException(
            status_code=500,
            detail="Trusted upstream auth is enabled but MINDROOM_TRUSTED_UPSTREAM_USER_ID_HEADER is not set",
        )

    user_id = _get_configured_header(request, settings.user_id_header)
    if user_id is None:
        raise HTTPException(
            status_code=401,
            detail=f"Missing trusted upstream identity header: {settings.user_id_header}",
        )

    jwt_claim = _verified_trusted_upstream_jwt_claim(request, settings, jwt_client)
    email_to_matrix_template = _validated_trusted_upstream_email_to_matrix_template(settings)
    email = _get_configured_header(request, settings.email_header)
    email = _verified_trusted_upstream_email(settings, user_id, email, jwt_claim)
    matrix_user_id = _get_configured_header(request, settings.matrix_user_id_header)
    parsed_matrix_user_id = try_parse_historical_matrix_user_id(matrix_user_id)
    if matrix_user_id is not None and parsed_matrix_user_id is None:
        raise HTTPException(status_code=401, detail="Invalid trusted upstream Matrix user id")
    if parsed_matrix_user_id is None:
        parsed_matrix_user_id = _derive_trusted_upstream_matrix_user_id(settings, email, email_to_matrix_template)

    auth_user = {
        "user_id": user_id,
        "email": email,
        "auth_source": "trusted_upstream",
    }
    if parsed_matrix_user_id is not None:
        auth_user["matrix_user_id"] = parsed_matrix_user_id
    return auth_user


def _supabase_auth_error_class() -> type[Exception]:
    """Return Supabase's AuthError class for narrow exception handling at the auth boundary."""
    return cast("type[Exception]", importlib.import_module("supabase_auth.errors").AuthError)


def _validate_supabase_token(token: str, auth_state: ApiAuthState) -> _SupabaseUserProtocol | None:
    """Validate a Supabase access token and return the authenticated user."""
    if auth_state.supabase_auth is None:
        return None

    try:
        response = auth_state.supabase_auth.auth.get_user(token)
    except _supabase_auth_error_class():
        return None

    if not response or not response.user:
        return None

    return response.user


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
                supabase_auth=_init_supabase_auth(
                    current.runtime_paths,
                    settings.supabase_url,
                    settings.supabase_anon_key,
                ),
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


def request_has_frontend_access(request: Request) -> bool:
    """Return whether the current request may load the dashboard UI."""
    authorization = request.headers.get("authorization")
    auth_state = cast("ApiAuthState", _bind_authenticated_request_snapshot(request).auth_state)
    mindroom_api_key = auth_state.settings.mindroom_api_key
    try:
        trusted_auth_user = _trusted_upstream_auth_user(
            request,
            auth_state.settings.trusted_upstream,
            auth_state.trusted_upstream_jwt_client,
        )
    except HTTPException as exc:
        if exc.status_code >= 500:
            raise
        return False
    if trusted_auth_user is not None:
        request.scope["auth_user"] = trusted_auth_user
        return True

    if auth_state.supabase_auth is None:
        if not mindroom_api_key:
            return True
        token = _get_request_token(
            request,
            authorization,
            cookie_names=(_STANDALONE_AUTH_COOKIE_NAME,),
        )
        return token is not None and secrets.compare_digest(token, mindroom_api_key)

    token = _get_request_token(
        request,
        authorization,
        cookie_names=(_PLATFORM_AUTH_COOKIE_NAME,),
    )
    user = _validate_supabase_token(token, auth_state) if token is not None else None
    return user is not None and (not auth_state.settings.account_id or user.id == auth_state.settings.account_id)


def sanitize_next_path(next_path: str | None) -> str:
    """Normalize redirect targets to an absolute in-app path."""
    if not next_path or not next_path.startswith("/") or next_path.startswith("//"):
        return "/"
    return next_path


def _request_path_with_query(request: Request) -> str:
    path = request.url.path
    query = request.url.query
    return f"{path}?{query}" if query else path


def login_redirect_for_request(request: Request, *, next_path: str | None = None) -> RedirectResponse | None:
    """Return the dashboard login redirect for one browser request when configured."""
    auth_settings = _request_auth_state(request).settings
    if auth_settings.trusted_upstream.enabled:
        return None
    if auth_settings.supabase_url and auth_settings.supabase_anon_key and auth_settings.platform_login_url:
        redirect_to = quote(str(request.url), safe="")
        return RedirectResponse(f"{auth_settings.platform_login_url}?redirect_to={redirect_to}")
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
        window.location.assign(nextPath);
        return;
      }}
      error.textContent = "Invalid API key.";
      input.select();
    }});
  </script>
</body>
</html>"""


async def verify_user(
    request: Request,
    authorization: str | None = Header(None),
    *,
    allow_public_paths: bool = True,
) -> dict[str, Any]:
    """Validate bearer or cookie auth and enforce owner if ACCOUNT_ID is set."""
    snapshot = _bind_authenticated_request_snapshot(request)
    auth_state = cast("ApiAuthState", snapshot.auth_state)
    mindroom_api_key = auth_state.settings.mindroom_api_key
    trusted_auth_user = _trusted_upstream_auth_user(
        request,
        auth_state.settings.trusted_upstream,
        auth_state.trusted_upstream_jwt_client,
    )
    if trusted_auth_user is not None:
        request.scope["auth_user"] = trusted_auth_user
        return trusted_auth_user

    if auth_state.supabase_auth is None:
        if allow_public_paths and _is_standalone_public_path(request.url.path):
            auth_user = {"user_id": "standalone", "email": None}
            request.scope["auth_user"] = auth_user
            return auth_user

        if mindroom_api_key:
            token = _get_request_token(
                request,
                authorization,
                cookie_names=(_STANDALONE_AUTH_COOKIE_NAME,),
            )
            if token is None:
                raise HTTPException(status_code=401, detail="Missing or invalid credentials")
            if not secrets.compare_digest(token, mindroom_api_key):
                raise HTTPException(status_code=401, detail="Invalid API key")
        auth_user = {"user_id": "standalone", "email": None}
        request.scope["auth_user"] = auth_user
        return auth_user

    token = _get_request_token(
        request,
        authorization,
        cookie_names=(_PLATFORM_AUTH_COOKIE_NAME,),
    )
    if token is None:
        raise HTTPException(status_code=401, detail="Missing or invalid credentials")

    user = _validate_supabase_token(token, auth_state)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid token")

    if auth_state.settings.account_id and user.id != auth_state.settings.account_id:
        raise HTTPException(status_code=403, detail="Forbidden")

    auth_user = {"user_id": user.id, "email": user.email}
    request.scope["auth_user"] = auth_user
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


@router.get("/login", include_in_schema=False)
async def standalone_login(request: Request, next: str = "/") -> Response:  # noqa: A002
    """Render the standalone dashboard login form when API-key auth is enabled."""
    if not cast("ApiAuthState", _bind_authenticated_request_snapshot(request).auth_state).settings.mindroom_api_key:
        raise HTTPException(status_code=404, detail="Not found")

    next_path = sanitize_next_path(next)
    if request_has_frontend_access(request):
        return RedirectResponse(next_path)

    return HTMLResponse(_render_standalone_login_page(next_path, config_lifecycle.api_runtime_paths(request)))
