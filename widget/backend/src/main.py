import threading
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from mindroom.models import Config

app = FastAPI(title="MindRoom Widget Backend")

# Configure CORS for widget
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3003", "http://localhost:5173"],  # Vite dev server
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Path to the config.yaml file (go up to mindroom root)
CONFIG_PATH = Path(__file__).parent.parent.parent.parent / "config.yaml"


def save_config_to_file(config: dict[str, Any]) -> None:
    """Save config to YAML file with deterministic ordering."""
    with open(CONFIG_PATH, "w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=True)


# Global variable to store current config
current_config: dict[str, Any] = {}
config_lock = threading.Lock()


class TestModelRequest(BaseModel):
    modelId: str


class ConfigFileHandler(FileSystemEventHandler):
    """Watch for changes to config.yaml"""

    def on_modified(self, event):
        if event.src_path.endswith("config.yaml"):
            print(f"Config file changed: {event.src_path}")
            load_config_from_file()


def load_config_from_file():
    """Load config from YAML file"""
    global current_config
    try:
        with open(CONFIG_PATH) as f, config_lock:
            current_config = yaml.safe_load(f)
        print("Config loaded successfully")
    except Exception as e:
        print(f"Error loading config: {e}")


# Load initial config
load_config_from_file()

# Set up file watcher
observer = Observer()
observer.schedule(ConfigFileHandler(), path=str(CONFIG_PATH.parent), recursive=False)
observer.start()


@app.on_event("startup")
async def startup_event():
    """Initialize the application"""
    print(f"Loading config from: {CONFIG_PATH}")
    print(f"Config exists: {CONFIG_PATH.exists()}")


@app.on_event("shutdown")
async def shutdown_event():
    """Clean up on shutdown"""
    observer.stop()
    observer.join()


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {"status": "healthy", "config_loaded": bool(current_config)}


@app.post("/api/config/load")
async def load_config():
    """Load configuration from file"""
    with config_lock:
        if not current_config:
            raise HTTPException(status_code=500, detail="Failed to load configuration")
        return current_config


@app.put("/api/config/save")
async def save_config(config: Config):
    """Save configuration to file"""
    try:
        config_dict = config.model_dump()

        # Write to YAML file
        save_config_to_file(config_dict)

        # Update current config
        with config_lock:
            current_config.update(config_dict)

        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save configuration: {str(e)}") from e


@app.get("/api/config/agents")
async def get_agents():
    """Get all agents"""
    with config_lock:
        agents = current_config.get("agents", {})
        # Convert to list format with IDs
        agent_list = []
        for agent_id, agent_data in agents.items():
            agent = {"id": agent_id, **agent_data}
            agent_list.append(agent)
        return agent_list


@app.put("/api/config/agents/{agent_id}")
async def update_agent(agent_id: str, agent_data: dict[str, Any]):
    """Update a specific agent"""
    with config_lock:
        if "agents" not in current_config:
            current_config["agents"] = {}

        # Remove ID from agent_data if present
        agent_data_copy = agent_data.copy()
        agent_data_copy.pop("id", None)

        current_config["agents"][agent_id] = agent_data_copy

    # Save to file
    try:
        save_config_to_file(current_config)
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save agent: {str(e)}") from e


@app.post("/api/config/agents")
async def create_agent(agent_data: dict[str, Any]):
    """Create a new agent"""
    agent_id = agent_data.get("display_name", "new_agent").lower().replace(" ", "_")

    with config_lock:
        if "agents" not in current_config:
            current_config["agents"] = {}

        # Check if agent already exists
        if agent_id in current_config["agents"]:
            # Generate unique ID
            counter = 1
            while f"{agent_id}_{counter}" in current_config["agents"]:
                counter += 1
            agent_id = f"{agent_id}_{counter}"

        # Remove ID from agent_data if present
        agent_data_copy = agent_data.copy()
        agent_data_copy.pop("id", None)

        current_config["agents"][agent_id] = agent_data_copy

    # Save to file
    try:
        save_config_to_file(current_config)
        return {"id": agent_id, "success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create agent: {str(e)}") from e


@app.delete("/api/config/agents/{agent_id}")
async def delete_agent(agent_id: str):
    """Delete an agent"""
    with config_lock:
        if "agents" not in current_config or agent_id not in current_config["agents"]:
            raise HTTPException(status_code=404, detail="Agent not found")

        del current_config["agents"][agent_id]

    # Save to file
    try:
        save_config_to_file(current_config)
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete agent: {str(e)}") from e


@app.get("/api/config/teams")
async def get_teams():
    """Get all teams"""
    with config_lock:
        teams = current_config.get("teams", {})
        # Convert to list format with IDs
        team_list = []
        for team_id, team_data in teams.items():
            team = {"id": team_id, **team_data}
            team_list.append(team)
        return team_list


@app.put("/api/config/teams/{team_id}")
async def update_team(team_id: str, team_data: dict[str, Any]):
    """Update a specific team"""
    with config_lock:
        if "teams" not in current_config:
            current_config["teams"] = {}

        # Remove ID from team_data if present
        team_data_copy = team_data.copy()
        team_data_copy.pop("id", None)

        current_config["teams"][team_id] = team_data_copy

    # Save to file
    try:
        save_config_to_file(current_config)
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save team: {str(e)}") from e


@app.post("/api/config/teams")
async def create_team(team_data: dict[str, Any]):
    """Create a new team"""
    team_id = team_data.get("display_name", "new_team").lower().replace(" ", "_")

    with config_lock:
        if "teams" not in current_config:
            current_config["teams"] = {}

        # Check if team already exists
        if team_id in current_config["teams"]:
            # Generate unique ID
            counter = 1
            while f"{team_id}_{counter}" in current_config["teams"]:
                counter += 1
            team_id = f"{team_id}_{counter}"

        # Remove ID from team_data if present
        team_data_copy = team_data.copy()
        team_data_copy.pop("id", None)

        current_config["teams"][team_id] = team_data_copy

    # Save to file
    try:
        save_config_to_file(current_config)
        return {"id": team_id, "success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create team: {str(e)}") from e


@app.delete("/api/config/teams/{team_id}")
async def delete_team(team_id: str):
    """Delete a team"""
    with config_lock:
        if "teams" not in current_config or team_id not in current_config["teams"]:
            raise HTTPException(status_code=404, detail="Team not found")

        del current_config["teams"][team_id]

    # Save to file
    try:
        save_config_to_file(current_config)
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete team: {str(e)}") from e


@app.get("/api/config/models")
async def get_models():
    """Get all model configurations"""
    with config_lock:
        return current_config.get("models", {})


@app.put("/api/config/models/{model_id}")
async def update_model(model_id: str, model_data: dict[str, Any]):
    """Update a model configuration"""
    with config_lock:
        if "models" not in current_config:
            current_config["models"] = {}

        current_config["models"][model_id] = model_data

    # Save to file
    try:
        save_config_to_file(current_config)
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save model: {str(e)}") from e


@app.get("/api/config/room-models")
async def get_room_models():
    """Get room-specific model overrides"""
    with config_lock:
        return current_config.get("room_models", {})


@app.put("/api/config/room-models")
async def update_room_models(room_models: dict[str, str]):
    """Update room-specific model overrides"""
    with config_lock:
        current_config["room_models"] = room_models

    # Save to file
    try:
        save_config_to_file(current_config)
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save room models: {str(e)}") from e


@app.post("/api/test/model")
async def test_model(request: TestModelRequest):
    """Test a model connection"""
    # TODO: Implement actual model testing
    # For now, just return success for demonstration
    model_id = request.modelId
    with config_lock:
        if model_id in current_config.get("models", {}):
            return {"success": True, "message": f"Model {model_id} is configured"}
        else:
            return {"success": False, "message": f"Model {model_id} not found"}


@app.get("/api/tools")
async def get_available_tools():
    """Get list of available tools"""
    # This should match the tools available in the MindRoom system
    tools = [
        "calculator",
        "file",
        "shell",
        "python",
        "csv",
        "pandas",
        "yfinance",
        "arxiv",
        "duckduckgo",
        "googlesearch",
        "tavily",
        "wikipedia",
        "newspaper",
        "website",
        "jina",
        "docker",
        "github",
        "email",
        "telegram",
    ]
    return tools


@app.get("/api/rooms")
async def get_available_rooms():
    """Get list of available rooms"""
    # Extract unique rooms from all agents
    rooms = set()
    with config_lock:
        for agent_data in current_config.get("agents", {}).values():
            agent_rooms = agent_data.get("rooms", [])
            rooms.update(agent_rooms)

    return sorted(list(rooms))


@app.post("/api/keys/encrypt")
async def encrypt_api_key(data: dict[str, str]):
    """Encrypt an API key for storage"""
    # TODO: Implement actual encryption
    # For now, just return a placeholder
    provider = data.get("provider", "")
    key = data.get("key", "")

    # In production, this would encrypt the key
    encrypted = f"encrypted_{provider}_{len(key)}"

    return {"encryptedKey": encrypted}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
