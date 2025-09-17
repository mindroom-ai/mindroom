# ruff: noqa: D100
import os
import shutil
import threading
from pathlib import Path
from typing import Annotated, Any

import yaml
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

# Import routers
from mindroom.api.credentials import router as credentials_router
from mindroom.api.google_integration import router as google_router
from mindroom.api.homeassistant_integration import router as homeassistant_router
from mindroom.api.integrations import router as integrations_router
from mindroom.api.matrix_operations import router as matrix_router
from mindroom.api.tools import router as tools_router
from mindroom.config import Config
from mindroom.constants import DEFAULT_AGENTS_CONFIG, DEFAULT_CONFIG_TEMPLATE
from mindroom.credentials_sync import sync_env_to_credentials

# Load environment variables from .env file
# Look for .env in the widget directory (parent of backend)
env_path = Path(__file__).parent.parent.parent / ".env"
load_dotenv(env_path)

app = FastAPI(title="MindRoom Widget Backend")

# Configure CORS for widget - allow multiple origins including port forwarding
app.add_middleware(
    CORSMiddleware,
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

# Resolve configurable config paths
CONFIG_PATH = DEFAULT_AGENTS_CONFIG
CONFIG_TEMPLATE_PATH = DEFAULT_CONFIG_TEMPLATE


def ensure_writable_config() -> None:
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


def save_config_to_file(config: dict[str, Any]) -> None:
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
    tmp_path.replace(CONFIG_PATH)


# Global variable to store current config
config: dict[str, Any] = {}
config_lock = threading.Lock()


# =========================
# Supabase JWT verification
# =========================
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY")
ACCOUNT_ID = os.getenv("ACCOUNT_ID")  # optional: enforce instance ownership

_supabase_auth = None
if SUPABASE_URL and SUPABASE_ANON_KEY:
    try:
        from supabase import create_client

        _supabase_auth = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)
    except Exception:
        _supabase_auth = None


async def verify_user(authorization: str | None = Header(None)) -> dict:
    """Validate Supabase JWT from Authorization header; enforce owner if ACCOUNT_ID set.

    In standalone mode (no Supabase), returns a default user to allow access.
    """
    if _supabase_auth is None:
        # Standalone mode - no auth configured, allow access
        return {"user_id": "standalone", "email": None}

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

    token = authorization.removeprefix("Bearer ").strip()
    try:
        user = _supabase_auth.auth.get_user(token)
    except Exception as err:
        raise HTTPException(status_code=401, detail="Invalid token") from err

    if not user or not user.user:
        raise HTTPException(status_code=401, detail="Invalid token")

    if ACCOUNT_ID and user.user.id != ACCOUNT_ID:
        raise HTTPException(status_code=403, detail="Forbidden")

    return {"user_id": user.user.id, "email": user.user.email}


class TestModelRequest(BaseModel):
    """Request model for testing AI model connections."""

    modelId: str  # noqa: N815


class ConfigFileHandler(FileSystemEventHandler):
    """Watch for changes to config.yaml."""

    def on_modified(self, event: FileSystemEvent) -> None:
        """Handle file modification events."""
        src_path = event.src_path
        if isinstance(src_path, bytes):
            src_path = src_path.decode("utf-8")
        if src_path.endswith("config.yaml"):
            print(f"Config file changed: {src_path}")
            load_config_from_file()


def load_config_from_file() -> None:
    """Load config from YAML file."""
    global config
    try:
        with CONFIG_PATH.open() as f, config_lock:
            config = yaml.safe_load(f)
        print("Config loaded successfully")
    except Exception as e:
        print(f"Error loading config: {e}")


ensure_writable_config()

# Load initial config
load_config_from_file()

# Set up file watcher
observer = Observer()
observer.schedule(ConfigFileHandler(), path=str(CONFIG_PATH.parent), recursive=False)
observer.start()

# Include routers
app.include_router(credentials_router, dependencies=[Depends(verify_user)])
app.include_router(google_router, dependencies=[Depends(verify_user)])
app.include_router(homeassistant_router, dependencies=[Depends(verify_user)])
app.include_router(integrations_router, dependencies=[Depends(verify_user)])
app.include_router(matrix_router, dependencies=[Depends(verify_user)])
app.include_router(tools_router, dependencies=[Depends(verify_user)])


@app.get("/api/health")
async def health_check() -> dict[str, str]:
    """Health check endpoint for testing."""
    return {"status": "healthy"}


@app.on_event("startup")
async def startup_event() -> None:
    """Initialize the application."""
    print(f"Loading config from: {CONFIG_PATH}")
    print(f"Config exists: {CONFIG_PATH.exists()}")

    # Sync API keys from environment to CredentialsManager
    print("Syncing API keys from environment to CredentialsManager...")
    sync_env_to_credentials()


@app.on_event("shutdown")
async def shutdown_event() -> None:
    """Clean up on shutdown."""
    observer.stop()
    observer.join()


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
    try:
        config_dict = new_config.model_dump(exclude_none=True)
        save_config_to_file(config_dict)

        # Update current config
        with config_lock:
            config.update(config_dict)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save configuration: {e!s}") from e
    else:
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
    with config_lock:
        if "agents" not in config:
            config["agents"] = {}

        # Remove ID from agent_data if present
        agent_data_copy = agent_data.copy()
        agent_data_copy.pop("id", None)

        config["agents"][agent_id] = agent_data_copy

    # Save to file
    try:
        save_config_to_file(config)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save agent: {e!s}") from e
    else:
        return {"success": True}


@app.post("/api/config/agents")
async def create_agent(agent_data: dict[str, Any], _user: Annotated[dict, Depends(verify_user)]) -> dict[str, Any]:
    """Create a new agent."""
    agent_id = agent_data.get("display_name", "new_agent").lower().replace(" ", "_")

    with config_lock:
        if "agents" not in config:
            config["agents"] = {}

        # Check if agent already exists
        if agent_id in config["agents"]:
            # Generate unique ID
            counter = 1
            while f"{agent_id}_{counter}" in config["agents"]:
                counter += 1
            agent_id = f"{agent_id}_{counter}"

        # Remove ID from agent_data if present
        agent_data_copy = agent_data.copy()
        agent_data_copy.pop("id", None)

        config["agents"][agent_id] = agent_data_copy

    # Save to file
    try:
        save_config_to_file(config)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create agent: {e!s}") from e
    else:
        return {"id": agent_id, "success": True}


@app.delete("/api/config/agents/{agent_id}")
async def delete_agent(agent_id: str, _user: Annotated[dict, Depends(verify_user)]) -> dict[str, bool]:
    """Delete an agent."""
    with config_lock:
        if "agents" not in config or agent_id not in config["agents"]:
            raise HTTPException(status_code=404, detail="Agent not found")

        del config["agents"][agent_id]

    # Save to file
    try:
        save_config_to_file(config)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete agent: {e!s}") from e
    else:
        return {"success": True}


@app.get("/api/config/teams")
async def get_teams() -> list[dict[str, Any]]:
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
async def update_team(team_id: str, team_data: dict[str, Any]) -> dict[str, bool]:
    """Update a specific team."""
    with config_lock:
        if "teams" not in config:
            config["teams"] = {}

        # Remove ID from team_data if present
        team_data_copy = team_data.copy()
        team_data_copy.pop("id", None)

        config["teams"][team_id] = team_data_copy

    # Save to file
    try:
        save_config_to_file(config)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save team: {e!s}") from e
    else:
        return {"success": True}


@app.post("/api/config/teams")
async def create_team(team_data: dict[str, Any]) -> dict[str, Any]:
    """Create a new team."""
    team_id = team_data.get("display_name", "new_team").lower().replace(" ", "_")

    with config_lock:
        if "teams" not in config:
            config["teams"] = {}

        # Check if team already exists
        if team_id in config["teams"]:
            # Generate unique ID
            counter = 1
            while f"{team_id}_{counter}" in config["teams"]:
                counter += 1
            team_id = f"{team_id}_{counter}"

        # Remove ID from team_data if present
        team_data_copy = team_data.copy()
        team_data_copy.pop("id", None)

        config["teams"][team_id] = team_data_copy

    # Save to file
    try:
        save_config_to_file(config)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create team: {e!s}") from e
    else:
        return {"id": team_id, "success": True}


@app.delete("/api/config/teams/{team_id}")
async def delete_team(team_id: str) -> dict[str, bool]:
    """Delete a team."""
    with config_lock:
        if "teams" not in config or team_id not in config["teams"]:
            raise HTTPException(status_code=404, detail="Team not found")

        del config["teams"][team_id]

    # Save to file
    try:
        save_config_to_file(config)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete team: {e!s}") from e
    else:
        return {"success": True}


@app.get("/api/config/models")
async def get_models() -> dict[str, Any]:
    """Get all model configurations."""
    with config_lock:
        models = config.get("models", {})
        return dict(models) if models else {}


@app.put("/api/config/models/{model_id}")
async def update_model(model_id: str, model_data: dict[str, Any]) -> dict[str, bool]:
    """Update a model configuration."""
    with config_lock:
        if "models" not in config:
            config["models"] = {}

        config["models"][model_id] = model_data

    # Save to file
    try:
        save_config_to_file(config)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save model: {e!s}") from e
    else:
        return {"success": True}


@app.get("/api/config/room-models")
async def get_room_models() -> dict[str, Any]:
    """Get room-specific model overrides."""
    with config_lock:
        room_models = config.get("room_models", {})
        return dict(room_models) if room_models else {}


@app.put("/api/config/room-models")
async def update_room_models(room_models: dict[str, str]) -> dict[str, bool]:
    """Update room-specific model overrides."""
    with config_lock:
        config["room_models"] = room_models

    # Save to file
    try:
        save_config_to_file(config)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save room models: {e!s}") from e
    else:
        return {"success": True}


@app.post("/api/test/model")
async def test_model(request: TestModelRequest) -> dict[str, Any]:
    """Test a model connection."""
    # TODO: Implement actual model testing
    # For now, just return success for demonstration
    model_id = request.modelId
    with config_lock:
        if model_id in config.get("models", {}):
            return {"success": True, "message": f"Model {model_id} is configured"}
        return {"success": False, "message": f"Model {model_id} not found"}


@app.get("/api/rooms")
async def get_available_rooms() -> list[str]:
    """Get list of available rooms."""
    # Extract unique rooms from all agents
    rooms = set()
    with config_lock:
        for agent_data in config.get("agents", {}).values():
            agent_rooms = agent_data.get("rooms", [])
            rooms.update(agent_rooms)

    return sorted(rooms)


@app.post("/api/keys/encrypt")
async def encrypt_api_key(data: dict[str, str]) -> dict[str, str]:
    """Encrypt an API key for storage."""
    # TODO: Implement actual encryption
    # For now, just return a placeholder
    provider = data.get("provider", "")
    key = data.get("key", "")

    # In production, this would encrypt the key
    encrypted = f"encrypted_{provider}_{len(key)}"

    return {"encryptedKey": encrypted}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8765)  # noqa: S104
