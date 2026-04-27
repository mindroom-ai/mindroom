# ruff: noqa: D100
from __future__ import annotations

import html
import importlib
import secrets
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Protocol, cast

from fastapi import APIRouter, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

from mindroom.api import config_lifecycle
from mindroom.api.config_lifecycle import ApiSnapshot, ApiState
from mindroom.api.config_lifecycle import request_snapshot as request_api_snapshot
from mindroom.api.config_lifecycle import store_request_snapshot as store_request_api_snapshot
from mindroom.tool_system.dependencies import auto_install_enabled, auto_install_tool_extra

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths

router = APIRouter(tags=["auth"])

_PLATFORM_AUTH_COOKIE_NAME = "mindroom_jwt"
_STANDALONE_AUTH_COOKIE_NAME = "mindroom_api_key"
_STANDALONE_PUBLIC_PATHS = frozenset(
    {
        "/api/google/callback",
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
class ApiAuthSettings:
    """Dashboard authentication settings for one runtime."""

    platform_login_url: str | None
    supabase_url: str | None
    supabase_anon_key: str | None
    account_id: str | None
    mindroom_api_key: str | None


@dataclass(frozen=True)
class ApiAuthState:
    """Cached authentication client state for one runtime."""

    runtime_paths: RuntimePaths
    settings: ApiAuthSettings
    supabase_auth: _SupabaseClientProtocol | None


def _app_state(api_app: FastAPI) -> ApiState:
    """Return the committed API state holder for one app instance."""
    try:
        state = api_app.state.api_state
    except AttributeError:
        state = None
    if not isinstance(state, ApiState):
        msg = "API context is not initialized"
        raise TypeError(msg)
    return state


def _app_account_id(api_app: FastAPI) -> str | None:
    """Return the API ingress-bound account id, when platform auth is configured."""
    try:
        account_id = api_app.state.api_auth_account_id
    except AttributeError:
        return None
    return cast("str | None", account_id)


def build_auth_settings(runtime_paths: RuntimePaths, *, account_id: str | None = None) -> ApiAuthSettings:
    """Read dashboard auth settings from one explicit runtime context."""
    return ApiAuthSettings(
        platform_login_url=runtime_paths.env_value("MINDROOM_PLATFORM_LOGIN_URL"),
        supabase_url=runtime_paths.env_value("SUPABASE_URL"),
        supabase_anon_key=runtime_paths.env_value("SUPABASE_ANON_KEY"),
        account_id=account_id,
        mindroom_api_key=runtime_paths.env_value("MINDROOM_API_KEY"),
    )


def app_auth_state(api_app: FastAPI) -> ApiAuthState:
    """Return the committed auth state for one API app instance."""
    app_state = _app_state(api_app)
    with app_state.config_lock:
        snapshot = app_state.snapshot
        state = cast("ApiAuthState | None", snapshot.auth_state)
        if state is not None and state.runtime_paths == snapshot.runtime_paths:
            return state
        settings = build_auth_settings(snapshot.runtime_paths, account_id=_app_account_id(api_app))
        state = ApiAuthState(
            runtime_paths=snapshot.runtime_paths,
            settings=settings,
            supabase_auth=_init_supabase_auth(
                snapshot.runtime_paths,
                settings.supabase_url,
                settings.supabase_anon_key,
            ),
        )
        app_state.snapshot = replace(snapshot, auth_state=state)
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


def _validate_supabase_token(token: str, auth_state: ApiAuthState) -> _SupabaseUserProtocol | None:
    """Validate a Supabase access token and return the authenticated user."""
    if auth_state.supabase_auth is None:
        return None

    try:
        response = auth_state.supabase_auth.auth.get_user(token)
    except Exception:
        return None

    if not response or not response.user:
        return None

    return response.user


def bind_authenticated_request_snapshot(request: Request) -> ApiSnapshot:
    """Bind one coherent auth/runtime/config snapshot to the request."""
    existing = request_api_snapshot(request)
    bound_auth_state = cast("ApiAuthState | None", existing.auth_state) if existing is not None else None
    if (
        existing is not None
        and bound_auth_state is not None
        and bound_auth_state.runtime_paths == existing.runtime_paths
    ):
        return existing

    app_state = _app_state(request.app)
    with app_state.config_lock:
        current = app_state.snapshot
        auth_state = cast("ApiAuthState | None", current.auth_state)
        if auth_state is None or auth_state.runtime_paths != current.runtime_paths:
            settings = build_auth_settings(current.runtime_paths, account_id=_app_account_id(request.app))
            auth_state = ApiAuthState(
                runtime_paths=current.runtime_paths,
                settings=settings,
                supabase_auth=_init_supabase_auth(
                    current.runtime_paths,
                    settings.supabase_url,
                    settings.supabase_anon_key,
                ),
            )
            current = replace(current, auth_state=auth_state)
            app_state.snapshot = current
        return store_request_api_snapshot(request, current)


def request_auth_state(request: Request) -> ApiAuthState:
    """Return the request-bound auth state when available."""
    snapshot = request_api_snapshot(request)
    if snapshot is None:
        return app_auth_state(request.app)
    auth_state = cast("ApiAuthState | None", snapshot.auth_state)
    if auth_state is None or auth_state.runtime_paths != snapshot.runtime_paths:
        return cast("ApiAuthState", bind_authenticated_request_snapshot(request).auth_state)
    return auth_state


def request_has_frontend_access(request: Request) -> bool:
    """Return whether the current request may load the dashboard UI."""
    authorization = request.headers.get("authorization")
    auth_state = cast("ApiAuthState", bind_authenticated_request_snapshot(request).auth_state)
    mindroom_api_key = auth_state.settings.mindroom_api_key

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
    if token is None:
        return False
    user = _validate_supabase_token(token, auth_state)
    if user is None:
        return False
    return not auth_state.settings.account_id or user.id == auth_state.settings.account_id


def sanitize_next_path(next_path: str | None) -> str:
    """Normalize redirect targets to an absolute in-app path."""
    if not next_path or not next_path.startswith("/") or next_path.startswith("//"):
        return "/"
    return next_path


def _render_standalone_login_page(
    next_path: str,
    runtime_paths: RuntimePaths,
) -> str:
    """Return the standalone dashboard login page."""
    escaped_next_path = html.escape(next_path, quote=True)
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
    const nextPath = {escaped_next_path!r};
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
    snapshot = bind_authenticated_request_snapshot(request)
    auth_state = cast("ApiAuthState", snapshot.auth_state)
    mindroom_api_key = auth_state.settings.mindroom_api_key

    if auth_state.supabase_auth is None:
        if allow_public_paths and request.url.path in _STANDALONE_PUBLIC_PATHS:
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
    mindroom_api_key = app_auth_state(request.app).settings.mindroom_api_key
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
    if not cast("ApiAuthState", bind_authenticated_request_snapshot(request).auth_state).settings.mindroom_api_key:
        raise HTTPException(status_code=404, detail="Not found")

    next_path = sanitize_next_path(next)
    if request_has_frontend_access(request):
        return RedirectResponse(next_path)

    return HTMLResponse(_render_standalone_login_page(next_path, config_lifecycle.api_runtime_paths(request)))
