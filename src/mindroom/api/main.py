# ruff: noqa: D100
import asyncio
import html
import importlib
import os
import secrets
import shutil
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Protocol, cast
from urllib.parse import quote

import yaml
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from watchfiles import awatch

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
from mindroom.config.main import Config
from mindroom.constants import CONFIG_PATH, CONFIG_TEMPLATE_PATH, safe_replace
from mindroom.credentials_sync import sync_env_to_credentials
from mindroom.frontend_assets import ensure_frontend_dist_dir
from mindroom.tool_system.dependencies import auto_install_enabled, auto_install_tool_extra


async def _watch_config(stop_event: asyncio.Event) -> None:
    """Watch config.yaml for changes using watchfiles."""
    async for changes in awatch(CONFIG_PATH.parent, stop_event=stop_event):
        for _change, path in changes:
            if path.endswith("config.yaml"):
                print(f"Config file changed: {path}")
                _load_config_from_file()


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Manage application startup and shutdown."""
    print(f"Loading config from: {CONFIG_PATH}")
    print(f"Config exists: {CONFIG_PATH.exists()}")

    # Sync API keys from environment to CredentialsManager
    print("Syncing API keys from environment to CredentialsManager...")
    sync_env_to_credentials()

    stop_event = asyncio.Event()
    watch_task = asyncio.create_task(_watch_config(stop_event))

    yield

    stop_event.set()
    watch_task.cancel()
    with suppress(asyncio.CancelledError):
        await watch_task


app = FastAPI(title="MindRoom Dashboard API", _lifespan=_lifespan)

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
_PLATFORM_LOGIN_URL = os.getenv("MINDROOM_PLATFORM_LOGIN_URL")


def load_runtime_config() -> tuple[Config, Path]:
    """Load the current runtime config and return it with its path."""
    return Config.from_yaml(CONFIG_PATH), CONFIG_PATH


def _resolve_frontend_dist_dir() -> Path | None:
    """Return the built dashboard directory when bundled or locally built."""
    return ensure_frontend_dist_dir()


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


def _ensure_writable_config() -> None:
    """Ensure the config file exists at a writable location.

    In managed deployments the writable config is placed on a persistent
    volume while a read-only template is mounted separately. When the final
    config file is missing we seed it from the template so both the bot and
    API read/write the same path.
    """
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)

    if CONFIG_PATH.exists():
        return

    if CONFIG_TEMPLATE_PATH != CONFIG_PATH and CONFIG_TEMPLATE_PATH.exists():
        shutil.copyfile(CONFIG_TEMPLATE_PATH, CONFIG_PATH)
        CONFIG_PATH.chmod(0o600)
        print(f"Seeded config from template {CONFIG_TEMPLATE_PATH} -> {CONFIG_PATH}")
        return

    # Fallback: create a minimal valid YAML structure so initial loads succeed
    CONFIG_PATH.write_text("agents: {}\nmodels: {}\n", encoding="utf-8")
    CONFIG_PATH.chmod(0o600)
    print(f"Created new config file at {CONFIG_PATH}")


def _save_config_to_file(config: dict[str, Any]) -> None:
    """Save config to YAML file with deterministic ordering."""
    tmp_path = CONFIG_PATH.with_suffix(CONFIG_PATH.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        yaml.dump(
            config,
            f,
            default_flow_style=False,
            sort_keys=True,
            allow_unicode=True,
        )
    safe_replace(tmp_path, CONFIG_PATH)


# Global variable to store current config
config: dict[str, Any] = {}
config_lock = threading.Lock()


def _run_config_write[T](
    mutate: Callable[[], T],
    *,
    error_prefix: str,
    save_payload: dict[str, Any] | None = None,
) -> T:
    """Mutate config under lock and persist atomically."""
    try:
        with config_lock:
            result = mutate()
            _save_config_to_file(config if save_payload is None else save_payload)
            return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{error_prefix}: {e!s}") from e


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


# =========================
# Supabase JWT verification
# =========================
_SUPABASE_URL = os.getenv("SUPABASE_URL")
_SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY")
_ACCOUNT_ID = os.getenv("ACCOUNT_ID")  # optional: enforce instance ownership
_MINDROOM_API_KEY = os.getenv("MINDROOM_API_KEY")  # optional: dashboard auth for standalone mode

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


_supabase_auth: _SupabaseClientProtocol | None = _init_supabase_auth(_SUPABASE_URL, _SUPABASE_ANON_KEY)


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


def _validate_supabase_token(token: str) -> _SupabaseUserProtocol | None:
    """Validate a Supabase access token and return the authenticated user."""
    if _supabase_auth is None:
        return None

    try:
        response = _supabase_auth.auth.get_user(token)
    except Exception:
        return None

    if not response or not response.user:
        return None

    return response.user


def _request_has_frontend_access(request: Request) -> bool:
    """Return whether the current request may load the dashboard UI."""
    authorization = request.headers.get("authorization")

    if _supabase_auth is None:
        if not _MINDROOM_API_KEY:
            return True
        token = _get_request_token(
            request,
            authorization,
            cookie_names=(_STANDALONE_AUTH_COOKIE_NAME,),
        )
        return token is not None and secrets.compare_digest(token, _MINDROOM_API_KEY)

    token = _get_request_token(
        request,
        authorization,
        cookie_names=(_PLATFORM_AUTH_COOKIE_NAME,),
    )
    return token is not None


def _sanitize_next_path(next_path: str | None) -> str:
    """Normalize redirect targets to an absolute in-app path."""
    if not next_path or not next_path.startswith("/") or next_path.startswith("//"):
        return "/"
    return next_path


def _render_standalone_login_page(next_path: str) -> str:
    """Return the standalone dashboard login page."""
    escaped_next_path = html.escape(next_path, quote=True)
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


async def verify_user(request: Request, authorization: str | None = Header(None)) -> dict:
    """Validate bearer or cookie auth and enforce owner if ACCOUNT_ID is set.

    In standalone mode (no Supabase), returns a default user to allow access.
    """
    if _supabase_auth is None:
        # Standalone mode
        if request.url.path in _STANDALONE_PUBLIC_PATHS:
            return {"user_id": "standalone", "email": None}

        if _MINDROOM_API_KEY:
            token = _get_request_token(
                request,
                authorization,
                cookie_names=(_STANDALONE_AUTH_COOKIE_NAME,),
            )
            if token is None:
                raise HTTPException(status_code=401, detail="Missing or invalid credentials")
            if not secrets.compare_digest(token, _MINDROOM_API_KEY):
                raise HTTPException(status_code=401, detail="Invalid API key")
        return {"user_id": "standalone", "email": None}

    token = _get_request_token(
        request,
        authorization,
        cookie_names=(_PLATFORM_AUTH_COOKIE_NAME,),
    )
    if token is None:
        raise HTTPException(status_code=401, detail="Missing or invalid credentials")

    user = _validate_supabase_token(token)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid token")

    if _ACCOUNT_ID and user.id != _ACCOUNT_ID:
        raise HTTPException(status_code=403, detail="Forbidden")

    return {"user_id": user.id, "email": user.email}


def _load_config_from_file() -> None:
    """Load config from YAML file."""
    global config
    try:
        with CONFIG_PATH.open() as f, config_lock:
            config = yaml.safe_load(f)
        print("Config loaded successfully")
    except Exception as e:
        print(f"Error loading config: {e}")


_ensure_writable_config()

# Load initial config
_load_config_from_file()

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
app.include_router(openai_compat_router)  # Uses its own bearer auth, not verify_user


@app.get("/api/health")
async def health_check() -> dict[str, str]:
    """Health check endpoint for testing."""
    return {"status": "healthy"}


@app.post("/api/auth/session", include_in_schema=False)
async def create_auth_session(request: Request, payload: dict[str, str], response: Response) -> dict[str, bool]:
    """Set a same-origin cookie for standalone dashboard auth."""
    if not _MINDROOM_API_KEY:
        raise HTTPException(status_code=404, detail="Dashboard auth is not enabled")

    api_key = payload.get("api_key", "")
    if not api_key or not secrets.compare_digest(api_key, _MINDROOM_API_KEY):
        raise HTTPException(status_code=401, detail="Invalid API key")

    response.set_cookie(
        key=_STANDALONE_AUTH_COOKIE_NAME,
        value=api_key,
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
    if not _MINDROOM_API_KEY:
        raise HTTPException(status_code=404, detail="Not found")

    next_path = _sanitize_next_path(next)
    if _request_has_frontend_access(request):
        return RedirectResponse(next_path)

    return HTMLResponse(_render_standalone_login_page(next_path))


@app.post("/api/config/load")
async def load_config(_user: Annotated[dict, Depends(verify_user)]) -> dict[str, Any]:
    """Load configuration from file."""
    with config_lock:
        if not config:
            raise HTTPException(status_code=500, detail="Failed to load configuration")
        return config


@app.put("/api/config/save")
async def save_config(new_config: Config, _user: Annotated[dict, Depends(verify_user)]) -> dict[str, bool]:
    """Save configuration to file."""
    config_dict = new_config.model_dump(exclude_none=True)

    def mutate() -> None:
        config.update(config_dict)

    _run_config_write(mutate, error_prefix="Failed to save configuration", save_payload=config_dict)
    return {"success": True}


@app.get("/api/config/agents")
async def get_agents(_user: Annotated[dict, Depends(verify_user)]) -> list[dict[str, Any]]:
    """Get all agents."""
    with config_lock:
        agents = config.get("agents", {})
        # Convert to list format with IDs
        agent_list = []
        for agent_id, agent_data in agents.items():
            agent = {"id": agent_id, **agent_data}
            agent_list.append(agent)
        return agent_list


@app.put("/api/config/agents/{agent_id}")
async def update_agent(
    agent_id: str,
    agent_data: dict[str, Any],
    _user: Annotated[dict, Depends(verify_user)],
) -> dict[str, bool]:
    """Update a specific agent."""

    def mutate() -> None:
        if "agents" not in config:
            config["agents"] = {}
        config["agents"][agent_id] = _sanitize_entity_payload(agent_data)

    _run_config_write(mutate, error_prefix="Failed to save agent")
    return {"success": True}


@app.post("/api/config/agents")
async def create_agent(agent_data: dict[str, Any], _user: Annotated[dict, Depends(verify_user)]) -> dict[str, Any]:
    """Create a new agent."""
    base_agent_id = agent_data.get("display_name", "new_agent").lower().replace(" ", "_")

    def mutate() -> str:
        if "agents" not in config:
            config["agents"] = {}
        agent_id = _resolve_unique_entity_id(base_agent_id, config["agents"])
        config["agents"][agent_id] = _sanitize_entity_payload(agent_data)
        return agent_id

    agent_id = _run_config_write(mutate, error_prefix="Failed to create agent")
    return {"id": agent_id, "success": True}


@app.delete("/api/config/agents/{agent_id}")
async def delete_agent(agent_id: str, _user: Annotated[dict, Depends(verify_user)]) -> dict[str, bool]:
    """Delete an agent."""

    def mutate() -> None:
        if "agents" not in config or agent_id not in config["agents"]:
            raise HTTPException(status_code=404, detail="Agent not found")
        del config["agents"][agent_id]

    _run_config_write(mutate, error_prefix="Failed to delete agent")
    return {"success": True}


@app.get("/api/config/teams")
async def get_teams(_user: Annotated[dict, Depends(verify_user)]) -> list[dict[str, Any]]:
    """Get all teams."""
    with config_lock:
        teams = config.get("teams", {})
        # Convert to list format with IDs
        team_list = []
        for team_id, team_data in teams.items():
            team = {"id": team_id, **team_data}
            team_list.append(team)
        return team_list


@app.put("/api/config/teams/{team_id}")
async def update_team(
    team_id: str,
    team_data: dict[str, Any],
    _user: Annotated[dict, Depends(verify_user)],
) -> dict[str, bool]:
    """Update a specific team."""

    def mutate() -> None:
        if "teams" not in config:
            config["teams"] = {}
        config["teams"][team_id] = _sanitize_entity_payload(team_data)

    _run_config_write(mutate, error_prefix="Failed to save team")
    return {"success": True}


@app.post("/api/config/teams")
async def create_team(team_data: dict[str, Any], _user: Annotated[dict, Depends(verify_user)]) -> dict[str, Any]:
    """Create a new team."""
    base_team_id = team_data.get("display_name", "new_team").lower().replace(" ", "_")

    def mutate() -> str:
        if "teams" not in config:
            config["teams"] = {}
        team_id = _resolve_unique_entity_id(base_team_id, config["teams"])
        config["teams"][team_id] = _sanitize_entity_payload(team_data)
        return team_id

    team_id = _run_config_write(mutate, error_prefix="Failed to create team")
    return {"id": team_id, "success": True}


@app.delete("/api/config/teams/{team_id}")
async def delete_team(team_id: str, _user: Annotated[dict, Depends(verify_user)]) -> dict[str, bool]:
    """Delete a team."""

    def mutate() -> None:
        if "teams" not in config or team_id not in config["teams"]:
            raise HTTPException(status_code=404, detail="Team not found")
        del config["teams"][team_id]

    _run_config_write(mutate, error_prefix="Failed to delete team")
    return {"success": True}


@app.get("/api/config/models")
async def get_models(_user: Annotated[dict, Depends(verify_user)]) -> dict[str, Any]:
    """Get all model configurations."""
    with config_lock:
        models = config.get("models", {})
        return dict(models) if models else {}


@app.put("/api/config/models/{model_id}")
async def update_model(
    model_id: str,
    model_data: dict[str, Any],
    _user: Annotated[dict, Depends(verify_user)],
) -> dict[str, bool]:
    """Update a model configuration."""

    def mutate() -> None:
        if "models" not in config:
            config["models"] = {}
        config["models"][model_id] = model_data

    _run_config_write(mutate, error_prefix="Failed to save model")
    return {"success": True}


@app.get("/api/config/room-models")
async def get_room_models(_user: Annotated[dict, Depends(verify_user)]) -> dict[str, Any]:
    """Get room-specific model overrides."""
    with config_lock:
        room_models = config.get("room_models", {})
        return dict(room_models) if room_models else {}


@app.put("/api/config/room-models")
async def update_room_models(
    room_models: dict[str, str],
    _user: Annotated[dict, Depends(verify_user)],
) -> dict[str, bool]:
    """Update room-specific model overrides."""

    def mutate() -> None:
        config["room_models"] = room_models

    _run_config_write(mutate, error_prefix="Failed to save room models")
    return {"success": True}


@app.get("/api/rooms")
async def get_available_rooms(_user: Annotated[dict, Depends(verify_user)]) -> list[str]:
    """Get list of available rooms."""
    # Extract unique rooms from all agents
    rooms = set()
    with config_lock:
        for agent_data in config.get("agents", {}).values():
            agent_rooms = agent_data.get("rooms", [])
            rooms.update(agent_rooms)

    return sorted(rooms)


@app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
@app.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
async def serve_frontend(request: Request, path: str = "") -> Response:
    """Serve the bundled dashboard and SPA routes from the backend."""
    first_segment = path.split("/", 1)[0] if path else ""
    if first_segment in _API_ROUTE_PREFIXES:
        raise HTTPException(status_code=404, detail="Not found")

    if not _request_has_frontend_access(request):
        target_path = _sanitize_next_path(f"/{path}" if path else "/")
        if _supabase_auth is not None and _PLATFORM_LOGIN_URL:
            redirect_to = quote(str(request.url), safe="")
            return RedirectResponse(f"{_PLATFORM_LOGIN_URL}?redirect_to={redirect_to}")
        if _MINDROOM_API_KEY:
            login_target = quote(target_path, safe="/?=&")
            return RedirectResponse(f"/login?next={login_target}")

        raise HTTPException(status_code=401, detail="Authentication required")

    frontend_dir = _resolve_frontend_dist_dir()
    if frontend_dir is None:
        raise HTTPException(status_code=404, detail="Frontend assets are not available")

    asset_path = _resolve_frontend_asset(frontend_dir, path)
    if asset_path is None:
        raise HTTPException(status_code=404, detail="Frontend asset not found")

    return FileResponse(asset_path)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8765)  # noqa: S104
