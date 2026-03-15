# ruff: noqa: D100
from __future__ import annotations

import asyncio
import html
import importlib
import os
import secrets
import threading
from contextlib import asynccontextmanager, suppress
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Annotated, Any, Protocol, cast
from urllib.parse import quote

import yaml
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, ValidationError

from mindroom import constants

# Import routers
from mindroom.api.credentials import router as credentials_router
from mindroom.api.google_integration import router as google_router
from mindroom.api.homeassistant_integration import router as homeassistant_router
from mindroom.api.integrations import router as integrations_router
from mindroom.api.knowledge import router as knowledge_router
from mindroom.api.matrix_operations import router as matrix_router
from mindroom.api.openai_compat import router as openai_compat_router
from mindroom.api.schedules import router as schedules_router
from mindroom.api.skills import router as skills_router
from mindroom.api.tools import router as tools_router
from mindroom.api.workers import router as workers_router
from mindroom.config.main import Config
from mindroom.config.main import load_config as load_runtime_config_model
from mindroom.credentials_sync import sync_env_to_credentials
from mindroom.file_watcher import watch_file
from mindroom.frontend_assets import ensure_frontend_dist_dir
from mindroom.logging_config import get_logger
from mindroom.runtime_state import get_runtime_state
from mindroom.tool_system import sandbox_proxy as sandbox_proxy_module
from mindroom.tool_system.dependencies import auto_install_enabled, auto_install_tool_extra
from mindroom.workers.runtime import get_primary_worker_manager, primary_worker_backend_available

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

logger = get_logger(__name__)
_WORKER_CLEANUP_INTERVAL_ENV = "MINDROOM_WORKER_CLEANUP_INTERVAL_SECONDS"


class _ApiState(Protocol):
    runtime_paths: constants.RuntimePaths
    config_data: dict[str, Any]
    config_lock: threading.Lock
    auth_state: _ApiAuthState | None


@dataclass(frozen=True)
class _ApiAuthSettings:
    platform_login_url: str | None
    supabase_url: str | None
    supabase_anon_key: str | None
    account_id: str | None
    mindroom_api_key: str | None


@dataclass(frozen=True)
class _ApiAuthState:
    settings: _ApiAuthSettings
    supabase_auth: _SupabaseClientProtocol | None


def _worker_cleanup_interval_seconds() -> float:
    """Return the configured background idle-worker cleanup interval."""
    raw = os.getenv(_WORKER_CLEANUP_INTERVAL_ENV, "0").strip()
    try:
        interval = float(raw)
    except ValueError:
        return 0.0
    return max(0.0, interval)


def _cleanup_workers_once(runtime_paths: constants.RuntimePaths) -> int:
    """Run one idle-worker cleanup pass when a backend is configured."""
    if not primary_worker_backend_available(
        proxy_url=sandbox_proxy_module._PROXY_URL,
        proxy_token=sandbox_proxy_module._PROXY_TOKEN,
    ):
        return 0

    worker_manager = get_primary_worker_manager(
        proxy_url=sandbox_proxy_module._PROXY_URL,
        proxy_token=sandbox_proxy_module._PROXY_TOKEN,
        storage_root=runtime_paths.storage_root,
    )
    cleaned_workers = worker_manager.cleanup_idle_workers()
    if cleaned_workers:
        logger.info(
            "Cleaned idle workers",
            count=len(cleaned_workers),
            backend=worker_manager.backend_name,
        )
    return len(cleaned_workers)


async def _worker_cleanup_loop(stop_event: asyncio.Event, runtime_paths: constants.RuntimePaths) -> None:
    """Periodically clean idle workers in the primary runtime."""
    interval_seconds = _worker_cleanup_interval_seconds()
    if interval_seconds <= 0:
        return

    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
            break
        except TimeoutError:
            try:
                await asyncio.to_thread(_cleanup_workers_once, runtime_paths)
            except Exception:
                logger.exception("Background worker cleanup failed")


def api_runtime_paths(request: Request) -> constants.RuntimePaths:
    """Return the API request's committed runtime paths."""
    return _app_runtime_paths(request.app)


def _app_runtime_paths(api_app: FastAPI) -> constants.RuntimePaths:
    """Return the committed runtime paths for one API app instance."""
    runtime_paths = api_app.state.__dict__.get("runtime_paths")
    if not isinstance(runtime_paths, constants.RuntimePaths):
        msg = "API runtime paths are not initialized"
        raise TypeError(msg)
    return runtime_paths


def api_config_data(request: Request) -> dict[str, Any]:
    """Return the mutable API config cache for one request."""
    return _app_config_data(request.app)


def api_config_lock(request: Request) -> threading.Lock:
    """Return the API config lock for one request."""
    return _app_config_lock(request.app)


def _app_config_data(api_app: FastAPI) -> dict[str, Any]:
    """Return the mutable config cache for one app instance."""
    return cast("_ApiState", api_app.state).config_data


def _app_config_lock(api_app: FastAPI) -> threading.Lock:
    """Return the config lock for one app instance."""
    return cast("_ApiState", api_app.state).config_lock


def _build_auth_settings_from_env() -> _ApiAuthSettings:
    """Read dashboard auth settings from the real process environment."""
    return _ApiAuthSettings(
        platform_login_url=os.getenv("MINDROOM_PLATFORM_LOGIN_URL"),
        supabase_url=os.getenv("SUPABASE_URL"),
        supabase_anon_key=os.getenv("SUPABASE_ANON_KEY"),
        account_id=os.getenv("ACCOUNT_ID"),
        mindroom_api_key=os.getenv("MINDROOM_API_KEY"),
    )


def _app_auth_state(api_app: FastAPI) -> _ApiAuthState:
    """Return the committed auth state for one API app instance."""
    state = cast("_ApiState", api_app.state).auth_state
    if state is not None:
        return state
    settings = _build_auth_settings_from_env()
    state = _ApiAuthState(
        settings=settings,
        supabase_auth=_init_supabase_auth(settings.supabase_url, settings.supabase_anon_key),
    )
    api_app.state.auth_state = state
    return state


async def _watch_config(
    stop_event: asyncio.Event,
    api_app: FastAPI,
    runtime_paths: constants.RuntimePaths,
) -> None:
    """Watch config.yaml for changes."""

    async def _on_config_change() -> None:
        logger.info("Config file changed", path=str(runtime_paths.config_path))
        _load_config_from_file(runtime_paths, api_app)

    await watch_file(runtime_paths.config_path, _on_config_change, stop_event=stop_event)


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Manage application startup and shutdown."""
    if not hasattr(_app.state, "runtime_paths"):
        _app.state.runtime_paths = constants.resolve_primary_runtime_paths(process_env=constants.exported_process_env())
    runtime_paths = _app_runtime_paths(_app)
    constants.sync_runtime_env_to_process(runtime_paths, sync_path_env=True)
    constants.ensure_writable_config_path(create_minimal=True, runtime_paths=runtime_paths)
    _load_config_from_file(runtime_paths, _app)
    print(f"Loading config from: {runtime_paths.config_path}")
    print(f"Config exists: {runtime_paths.config_path.exists()}")

    # Sync API keys from environment to CredentialsManager
    print("Syncing API keys from environment to CredentialsManager...")
    sync_env_to_credentials(runtime_paths=runtime_paths)

    stop_event = asyncio.Event()
    watch_task = asyncio.create_task(_watch_config(stop_event, _app, runtime_paths))
    worker_cleanup_task = asyncio.create_task(_worker_cleanup_loop(stop_event, runtime_paths))

    yield

    stop_event.set()
    watch_task.cancel()
    worker_cleanup_task.cancel()
    with suppress(asyncio.CancelledError):
        await watch_task
    with suppress(asyncio.CancelledError):
        await worker_cleanup_task


app = FastAPI(title="MindRoom Dashboard API", lifespan=_lifespan)
app.state.config_data = {}
app.state.config_lock = threading.Lock()
app.state.auth_state = None

# Configure CORS for the standalone frontend dev server.
app.add_middleware(
    CORSMiddleware,  # ty: ignore[invalid-argument-type]
    allow_origins=[
        "http://localhost:3003",  # Frontend dev server alternative port
        "http://localhost:5173",  # Vite dev server default
        "http://127.0.0.1:3003",  # Alternative localhost
        "http://127.0.0.1:5173",
        "*",  # Allow all origins for development (remove in production)
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)

_API_ROUTE_PREFIXES = frozenset({"api", "v1"})
_PLATFORM_AUTH_COOKIE_NAME = "mindroom_jwt"
_STANDALONE_AUTH_COOKIE_NAME = "mindroom_api_key"


def load_runtime_config(
    runtime_paths: constants.RuntimePaths,
) -> tuple[Config, Path]:
    """Load the current runtime config and return it with its path."""
    return load_runtime_config_model(runtime_paths), runtime_paths.config_path


def _resolve_frontend_asset(frontend_dir: Path, request_path: str) -> Path | None:
    """Resolve a request path to a static asset or SPA fallback."""
    normalized_path = request_path.strip("/")
    index_path = frontend_dir / "index.html"
    if not normalized_path:
        return index_path if index_path.is_file() else None

    candidate_parts = PurePosixPath(normalized_path).parts
    if ".." in candidate_parts:
        return None

    candidate = frontend_dir.joinpath(*candidate_parts)
    if candidate.is_file():
        return candidate

    if candidate.is_dir():
        nested_index_path = candidate / "index.html"
        if nested_index_path.is_file():
            return nested_index_path

    if PurePosixPath(normalized_path).suffix:
        return None

    return index_path if index_path.is_file() else None


def _save_config_to_file(
    config: dict[str, Any],
    runtime_paths: constants.RuntimePaths,
) -> None:
    """Save config to YAML file with deterministic ordering."""
    config_path = runtime_paths.config_path
    tmp_path = config_path.with_suffix(config_path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        yaml.dump(
            config,
            f,
            default_flow_style=False,
            sort_keys=True,
            allow_unicode=True,
        )
    constants.safe_replace(tmp_path, config_path)


def _validated_config_payload(
    raw_config: dict[str, Any],
    runtime_paths: constants.RuntimePaths,
) -> dict[str, Any]:
    """Normalize and validate one config payload against the active runtime."""
    validated_config = Config.validate_with_runtime(raw_config, runtime_paths)
    return validated_config.model_dump(exclude_none=True)


def _run_config_write[T](
    api_app: FastAPI,
    mutate: Callable[[], T],
    *,
    runtime_paths: constants.RuntimePaths,
    error_prefix: str,
    save_payload: dict[str, Any] | None = None,
) -> T:
    """Mutate config under lock and persist atomically."""
    config_data = _app_config_data(api_app)
    with _app_config_lock(api_app):
        try:
            original_config = deepcopy(config_data)
            result = mutate()
            candidate_config = config_data if save_payload is None else save_payload
            validated_payload = _validated_config_payload(candidate_config, runtime_paths)
            config_data.clear()
            config_data.update(validated_payload)
            _save_config_to_file(validated_payload, runtime_paths=runtime_paths)
        except HTTPException:
            config_data.clear()
            config_data.update(original_config)
            raise
        except ValidationError as e:
            config_data.clear()
            config_data.update(original_config)
            raise HTTPException(status_code=422, detail=e.errors(include_context=False)) from e
        except Exception as e:
            config_data.clear()
            config_data.update(original_config)
            raise HTTPException(status_code=500, detail=f"{error_prefix}: {e!s}") from e
        else:
            return result


def _sanitize_entity_payload(entity_data: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of entity data without API-only ID fields."""
    payload = entity_data.copy()
    payload.pop("id", None)
    return payload


def _resolve_unique_entity_id(base_id: str, entities: dict[str, Any]) -> str:
    """Return a unique ID, appending a numeric suffix when needed."""
    if base_id not in entities:
        return base_id
    counter = 1
    while f"{base_id}_{counter}" in entities:
        counter += 1
    return f"{base_id}_{counter}"


class _AuthSessionRequest(BaseModel):
    """Standalone dashboard login payload."""

    api_key: str


_STANDALONE_PUBLIC_PATHS = frozenset(
    {
        "/api/google/callback",
        "/api/homeassistant/callback",
        "/api/integrations/spotify/callback",
    },
)


class _SupabaseUserProtocol(Protocol):
    id: str
    email: str | None


class _SupabaseUserResponseProtocol(Protocol):
    user: _SupabaseUserProtocol | None


class _SupabaseAuthProtocol(Protocol):
    def get_user(self, token: str) -> _SupabaseUserResponseProtocol | None: ...


class _SupabaseClientProtocol(Protocol):
    auth: _SupabaseAuthProtocol


def _init_supabase_auth(
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
        if not auto_install_enabled():
            disabled_hint = " Auto-install is disabled by MINDROOM_NO_AUTO_INSTALL_TOOLS."
        if not auto_install_tool_extra("supabase"):
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


def _validate_supabase_token(token: str, api_app: FastAPI) -> _SupabaseUserProtocol | None:
    """Validate a Supabase access token and return the authenticated user."""
    auth_state = _app_auth_state(api_app)
    if auth_state.supabase_auth is None:
        return None

    try:
        response = auth_state.supabase_auth.auth.get_user(token)
    except Exception:
        return None

    if not response or not response.user:
        return None

    return response.user


def _request_has_frontend_access(request: Request) -> bool:
    """Return whether the current request may load the dashboard UI."""
    authorization = request.headers.get("authorization")
    auth_state = _app_auth_state(request.app)
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
    return token is not None and _validate_supabase_token(token, request.app) is not None


def _sanitize_next_path(next_path: str | None) -> str:
    """Normalize redirect targets to an absolute in-app path."""
    if not next_path or not next_path.startswith("/") or next_path.startswith("//"):
        return "/"
    return next_path


def _render_standalone_login_page(
    next_path: str,
    runtime_paths: constants.RuntimePaths,
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
) -> dict:
    """Validate bearer or cookie auth and enforce owner if ACCOUNT_ID is set.

    In standalone mode (no Supabase), returns a default user to allow access.
    """
    auth_state = _app_auth_state(request.app)
    mindroom_api_key = auth_state.settings.mindroom_api_key

    if auth_state.supabase_auth is None:
        # Standalone mode
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

    user = _validate_supabase_token(token, request.app)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid token")

    if auth_state.settings.account_id and user.id != auth_state.settings.account_id:
        raise HTTPException(status_code=403, detail="Forbidden")

    auth_user = {"user_id": user.id, "email": user.email}
    request.scope["auth_user"] = auth_user
    return auth_user


def _load_config_from_file(runtime_paths: constants.RuntimePaths, api_app: FastAPI) -> None:
    """Load config from YAML file."""
    try:
        validated_payload = load_runtime_config_model(runtime_paths).model_dump(exclude_none=True)
        with _app_config_lock(api_app):
            config_data = _app_config_data(api_app)
            config_data.clear()
            config_data.update(validated_payload)
        print("Config loaded successfully")
    except Exception as e:
        print(f"Error loading config: {e}")


# Include routers
app.include_router(credentials_router, dependencies=[Depends(verify_user)])
app.include_router(google_router, dependencies=[Depends(verify_user)])
app.include_router(homeassistant_router, dependencies=[Depends(verify_user)])
app.include_router(integrations_router, dependencies=[Depends(verify_user)])
app.include_router(matrix_router, dependencies=[Depends(verify_user)])
app.include_router(schedules_router, dependencies=[Depends(verify_user)])
app.include_router(knowledge_router, dependencies=[Depends(verify_user)])
app.include_router(skills_router, dependencies=[Depends(verify_user)])
app.include_router(tools_router, dependencies=[Depends(verify_user)])
app.include_router(workers_router, dependencies=[Depends(verify_user)])
app.include_router(openai_compat_router)  # Uses its own bearer auth, not verify_user


@app.get("/api/health")
async def health_check() -> dict[str, str]:
    """Health check endpoint for testing."""
    return {"status": "healthy"}


@app.get("/api/ready")
async def readiness_check() -> JSONResponse:
    """Readiness endpoint tied to successful orchestrator startup."""
    state = get_runtime_state()
    if state.phase == "ready":
        return JSONResponse({"status": "ready"})
    return JSONResponse(
        status_code=503,
        content={"status": state.phase, "detail": state.detail or "MindRoom is not ready"},
    )


@app.post("/api/auth/session", include_in_schema=False)
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


@app.delete("/api/auth/session", include_in_schema=False)
async def clear_auth_session(response: Response) -> dict[str, bool]:
    """Clear the standalone dashboard auth cookie."""
    response.delete_cookie(key=_STANDALONE_AUTH_COOKIE_NAME, path="/")
    return {"success": True}


@app.get("/login", include_in_schema=False)
async def standalone_login(request: Request, next: str = "/") -> Response:  # noqa: A002
    """Render the standalone dashboard login form when API-key auth is enabled."""
    if not _app_auth_state(request.app).settings.mindroom_api_key:
        raise HTTPException(status_code=404, detail="Not found")

    next_path = _sanitize_next_path(next)
    if _request_has_frontend_access(request):
        return RedirectResponse(next_path)

    return HTMLResponse(_render_standalone_login_page(next_path, api_runtime_paths(request)))


@app.post("/api/config/load")
async def load_config(request: Request, _user: Annotated[dict, Depends(verify_user)]) -> dict[str, Any]:
    """Load configuration from file."""
    with _app_config_lock(request.app):
        if not _app_config_data(request.app):
            raise HTTPException(status_code=500, detail="Failed to load configuration")
        return _app_config_data(request.app)


@app.put("/api/config/save")
async def save_config(
    request: Request,
    new_config: dict[str, Any],
    _user: Annotated[dict, Depends(verify_user)],
) -> dict[str, bool]:
    """Save configuration to file."""
    runtime_paths = _app_runtime_paths(request.app)

    config_data = _app_config_data(request.app)

    def mutate() -> None:
        config_data.clear()
        config_data.update(new_config)

    _run_config_write(
        request.app,
        mutate,
        runtime_paths=runtime_paths,
        error_prefix="Failed to save configuration",
    )
    return {"success": True}


@app.get("/api/config/agents")
async def get_agents(request: Request, _user: Annotated[dict, Depends(verify_user)]) -> list[dict[str, Any]]:
    """Get all agents."""
    with _app_config_lock(request.app):
        agents = _app_config_data(request.app).get("agents", {})
        # Convert to list format with IDs
        agent_list = []
        for agent_id, agent_data in agents.items():
            agent = {"id": agent_id, **agent_data}
            agent_list.append(agent)
        return agent_list


@app.put("/api/config/agents/{agent_id}")
async def update_agent(
    request: Request,
    agent_id: str,
    agent_data: dict[str, Any],
    _user: Annotated[dict, Depends(verify_user)],
) -> dict[str, bool]:
    """Update a specific agent."""
    config_data = _app_config_data(request.app)

    def mutate() -> None:
        if "agents" not in config_data:
            config_data["agents"] = {}
        config_data["agents"][agent_id] = _sanitize_entity_payload(agent_data)

    _run_config_write(
        request.app,
        mutate,
        runtime_paths=_app_runtime_paths(request.app),
        error_prefix="Failed to save agent",
    )
    return {"success": True}


@app.post("/api/config/agents")
async def create_agent(
    request: Request,
    agent_data: dict[str, Any],
    _user: Annotated[dict, Depends(verify_user)],
) -> dict[str, Any]:
    """Create a new agent."""
    base_agent_id = agent_data.get("display_name", "new_agent").lower().replace(" ", "_")
    config_data = _app_config_data(request.app)

    def mutate() -> str:
        if "agents" not in config_data:
            config_data["agents"] = {}
        agent_id = _resolve_unique_entity_id(base_agent_id, config_data["agents"])
        config_data["agents"][agent_id] = _sanitize_entity_payload(agent_data)
        return agent_id

    agent_id = _run_config_write(
        request.app,
        mutate,
        runtime_paths=_app_runtime_paths(request.app),
        error_prefix="Failed to create agent",
    )
    return {"id": agent_id, "success": True}


@app.delete("/api/config/agents/{agent_id}")
async def delete_agent(
    request: Request,
    agent_id: str,
    _user: Annotated[dict, Depends(verify_user)],
) -> dict[str, bool]:
    """Delete an agent."""
    config_data = _app_config_data(request.app)

    def mutate() -> None:
        if "agents" not in config_data or agent_id not in config_data["agents"]:
            raise HTTPException(status_code=404, detail="Agent not found")
        del config_data["agents"][agent_id]

    _run_config_write(
        request.app,
        mutate,
        runtime_paths=_app_runtime_paths(request.app),
        error_prefix="Failed to delete agent",
    )
    return {"success": True}


@app.get("/api/config/teams")
async def get_teams(request: Request, _user: Annotated[dict, Depends(verify_user)]) -> list[dict[str, Any]]:
    """Get all teams."""
    with _app_config_lock(request.app):
        teams = _app_config_data(request.app).get("teams", {})
        # Convert to list format with IDs
        team_list = []
        for team_id, team_data in teams.items():
            team = {"id": team_id, **team_data}
            team_list.append(team)
        return team_list


@app.put("/api/config/teams/{team_id}")
async def update_team(
    request: Request,
    team_id: str,
    team_data: dict[str, Any],
    _user: Annotated[dict, Depends(verify_user)],
) -> dict[str, bool]:
    """Update a specific team."""
    config_data = _app_config_data(request.app)

    def mutate() -> None:
        if "teams" not in config_data:
            config_data["teams"] = {}
        config_data["teams"][team_id] = _sanitize_entity_payload(team_data)

    _run_config_write(
        request.app,
        mutate,
        runtime_paths=_app_runtime_paths(request.app),
        error_prefix="Failed to save team",
    )
    return {"success": True}


@app.post("/api/config/teams")
async def create_team(
    request: Request,
    team_data: dict[str, Any],
    _user: Annotated[dict, Depends(verify_user)],
) -> dict[str, Any]:
    """Create a new team."""
    base_team_id = team_data.get("display_name", "new_team").lower().replace(" ", "_")
    config_data = _app_config_data(request.app)

    def mutate() -> str:
        if "teams" not in config_data:
            config_data["teams"] = {}
        team_id = _resolve_unique_entity_id(base_team_id, config_data["teams"])
        config_data["teams"][team_id] = _sanitize_entity_payload(team_data)
        return team_id

    team_id = _run_config_write(
        request.app,
        mutate,
        runtime_paths=_app_runtime_paths(request.app),
        error_prefix="Failed to create team",
    )
    return {"id": team_id, "success": True}


@app.delete("/api/config/teams/{team_id}")
async def delete_team(
    request: Request,
    team_id: str,
    _user: Annotated[dict, Depends(verify_user)],
) -> dict[str, bool]:
    """Delete a team."""
    config_data = _app_config_data(request.app)

    def mutate() -> None:
        if "teams" not in config_data or team_id not in config_data["teams"]:
            raise HTTPException(status_code=404, detail="Team not found")
        del config_data["teams"][team_id]

    _run_config_write(
        request.app,
        mutate,
        runtime_paths=_app_runtime_paths(request.app),
        error_prefix="Failed to delete team",
    )
    return {"success": True}


@app.get("/api/config/models")
async def get_models(request: Request, _user: Annotated[dict, Depends(verify_user)]) -> dict[str, Any]:
    """Get all model configurations."""
    with _app_config_lock(request.app):
        models = _app_config_data(request.app).get("models", {})
        return dict(models) if models else {}


@app.put("/api/config/models/{model_id}")
async def update_model(
    request: Request,
    model_id: str,
    model_data: dict[str, Any],
    _user: Annotated[dict, Depends(verify_user)],
) -> dict[str, bool]:
    """Update a model configuration."""
    config_data = _app_config_data(request.app)

    def mutate() -> None:
        if "models" not in config_data:
            config_data["models"] = {}
        config_data["models"][model_id] = model_data

    _run_config_write(
        request.app,
        mutate,
        runtime_paths=_app_runtime_paths(request.app),
        error_prefix="Failed to save model",
    )
    return {"success": True}


@app.get("/api/config/room-models")
async def get_room_models(request: Request, _user: Annotated[dict, Depends(verify_user)]) -> dict[str, Any]:
    """Get room-specific model overrides."""
    with _app_config_lock(request.app):
        room_models = _app_config_data(request.app).get("room_models", {})
        return dict(room_models) if room_models else {}


@app.put("/api/config/room-models")
async def update_room_models(
    request: Request,
    room_models: dict[str, str],
    _user: Annotated[dict, Depends(verify_user)],
) -> dict[str, bool]:
    """Update room-specific model overrides."""
    config_data = _app_config_data(request.app)

    def mutate() -> None:
        config_data["room_models"] = room_models

    _run_config_write(
        request.app,
        mutate,
        runtime_paths=_app_runtime_paths(request.app),
        error_prefix="Failed to save room models",
    )
    return {"success": True}


@app.get("/api/rooms")
async def get_available_rooms(request: Request, _user: Annotated[dict, Depends(verify_user)]) -> list[str]:
    """Get list of available rooms."""
    # Extract unique rooms from all agents
    rooms = set()
    with _app_config_lock(request.app):
        for agent_data in _app_config_data(request.app).get("agents", {}).values():
            agent_rooms = agent_data.get("rooms", [])
            rooms.update(agent_rooms)

    return sorted(rooms)


@app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
@app.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
async def serve_frontend(request: Request, path: str = "") -> Response:
    """Serve the bundled dashboard and SPA routes from the MindRoom runtime."""
    first_segment = path.split("/", 1)[0] if path else ""
    if first_segment in _API_ROUTE_PREFIXES:
        raise HTTPException(status_code=404, detail="Not found")

    if not _request_has_frontend_access(request):
        auth_settings = _app_auth_state(request.app).settings
        target_path = _sanitize_next_path(f"/{path}" if path else "/")
        if auth_settings.supabase_url and auth_settings.supabase_anon_key and auth_settings.platform_login_url:
            redirect_to = quote(str(request.url), safe="")
            return RedirectResponse(f"{auth_settings.platform_login_url}?redirect_to={redirect_to}")
        if auth_settings.mindroom_api_key:
            login_target = quote(target_path, safe="/?=&")
            return RedirectResponse(f"/login?next={login_target}")

        raise HTTPException(status_code=401, detail="Authentication required")

    frontend_dir = ensure_frontend_dist_dir()
    if frontend_dir is None:
        raise HTTPException(status_code=404, detail="Frontend assets are not available")

    asset_path = _resolve_frontend_asset(frontend_dir, path)
    if asset_path is None:
        raise HTTPException(status_code=404, detail="Frontend asset not found")

    return FileResponse(asset_path)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8765)  # noqa: S104
