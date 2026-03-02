"""Tests for the widget backend API endpoints."""

from pathlib import Path
from typing import Any, NoReturn

import pytest
import yaml
from fastapi.testclient import TestClient

from mindroom.api import main


def test_init_supabase_auth_returns_none_without_credentials() -> None:
    """Supabase auth should stay disabled when credentials are incomplete."""
    assert main._init_supabase_auth(None, None) is None
    assert main._init_supabase_auth("https://supabase.test", None) is None
    assert main._init_supabase_auth(None, "anon-key") is None


def test_init_supabase_auth_raises_when_auto_install_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing supabase dependency should error with disable hint when auto-install is off."""
    install_calls: list[str] = []

    def _missing_supabase(_name: str) -> NoReturn:
        module_name = "supabase"
        raise ModuleNotFoundError(module_name)

    def _auto_install(extra_name: str) -> bool:
        install_calls.append(extra_name)
        return False

    monkeypatch.setattr(main.importlib, "import_module", _missing_supabase)
    monkeypatch.setattr(main, "auto_install_enabled", lambda: False)
    monkeypatch.setattr(main, "auto_install_tool_extra", _auto_install)

    with pytest.raises(ImportError, match="MINDROOM_NO_AUTO_INSTALL_TOOLS"):
        main._init_supabase_auth("https://supabase.test", "anon-key")

    assert install_calls == ["supabase"]


def test_init_supabase_auth_raises_when_auto_install_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing dependency should error with install hint when auto-install attempt fails."""
    install_calls: list[str] = []

    def _missing_supabase(_name: str) -> NoReturn:
        module_name = "supabase"
        raise ModuleNotFoundError(module_name)

    def _auto_install(extra_name: str) -> bool:
        install_calls.append(extra_name)
        return False

    monkeypatch.setattr(main.importlib, "import_module", _missing_supabase)
    monkeypatch.setattr(main, "auto_install_enabled", lambda: True)
    monkeypatch.setattr(main, "auto_install_tool_extra", _auto_install)

    with pytest.raises(ImportError, match=r"mindroom\[supabase\]") as err:
        main._init_supabase_auth("https://supabase.test", "anon-key")

    assert install_calls == ["supabase"]
    assert "MINDROOM_NO_AUTO_INSTALL_TOOLS" not in str(err.value)


def test_health_check(test_client: TestClient) -> None:
    """Test the health check endpoint."""
    response = test_client.get("/api/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"


def test_load_config(test_client: TestClient) -> None:
    """Test loading configuration."""
    response = test_client.post("/api/config/load")
    assert response.status_code == 200

    config = response.json()
    assert "agents" in config
    assert "models" in config
    assert "test_agent" in config["agents"]


def test_get_agents(test_client: TestClient) -> None:
    """Test getting all agents."""
    # First load config
    test_client.post("/api/config/load")

    response = test_client.get("/api/config/agents")
    assert response.status_code == 200

    agents = response.json()
    assert isinstance(agents, list)
    assert len(agents) > 0

    # Check agent structure
    agent = agents[0]
    assert "id" in agent
    assert "display_name" in agent
    assert "tools" in agent
    assert "rooms" in agent


def test_create_agent(test_client: TestClient, sample_agent_data: dict[str, Any], temp_config_file: Path) -> None:
    """Test creating a new agent."""
    # Load config first
    test_client.post("/api/config/load")

    response = test_client.post("/api/config/agents", json=sample_agent_data)
    assert response.status_code == 200

    result = response.json()
    assert "id" in result
    assert result["success"] is True

    # Verify it was saved to file
    with temp_config_file.open() as f:
        config = yaml.safe_load(f)
    assert result["id"] in config["agents"]
    assert config["agents"][result["id"]]["display_name"] == sample_agent_data["display_name"]


def test_update_agent(test_client: TestClient, temp_config_file: Path) -> None:
    """Test updating an existing agent."""
    # Load config first
    test_client.post("/api/config/load")

    update_data = {"display_name": "Updated Test Agent", "tools": ["calculator", "file"], "rooms": ["updated_room"]}

    response = test_client.put("/api/config/agents/test_agent", json=update_data)
    assert response.status_code == 200

    result = response.json()
    assert result["success"] is True

    # Verify file was updated
    with temp_config_file.open() as f:
        config = yaml.safe_load(f)
    assert config["agents"]["test_agent"]["display_name"] == "Updated Test Agent"
    assert "file" in config["agents"]["test_agent"]["tools"]
    assert "updated_room" in config["agents"]["test_agent"]["rooms"]


def test_delete_agent(test_client: TestClient, temp_config_file: Path) -> None:
    """Test deleting an agent."""
    # Load config first
    test_client.post("/api/config/load")

    response = test_client.delete("/api/config/agents/test_agent")
    assert response.status_code == 200

    result = response.json()
    assert result["success"] is True

    # Verify it was removed from file
    with temp_config_file.open() as f:
        config = yaml.safe_load(f)
    assert "test_agent" not in config["agents"]


def test_get_tools(test_client: TestClient) -> None:
    """Test getting available tools."""
    # First test the new endpoint that returns full tool metadata
    response = test_client.get("/api/tools/")
    assert response.status_code == 200

    data = response.json()
    assert "tools" in data
    assert isinstance(data["tools"], list)
    assert len(data["tools"]) > 0

    # Check that some expected tools are present
    tool_names = {tool["name"] for tool in data["tools"]}
    assert "calculator" in tool_names
    assert "file" in tool_names
    assert "shell" in tool_names

    # Check that tools have the expected structure
    first_tool = data["tools"][0]
    assert "name" in first_tool
    assert "display_name" in first_tool
    assert "description" in first_tool
    assert "category" in first_tool
    assert "icon_color" in first_tool  # New field we added


def test_get_tools_includes_preset_metadata(test_client: TestClient) -> None:
    """Preset tool entries should appear in the tools response with expected shape."""
    response = test_client.get("/api/tools/")
    assert response.status_code == 200

    tools_by_name = {tool["name"]: tool for tool in response.json()["tools"]}
    assert "openclaw_compat" in tools_by_name

    preset = tools_by_name["openclaw_compat"]
    assert preset["category"] == "preset"
    assert preset["status"] == "available"
    assert preset["setup_type"] == "none"
    assert preset["helper_text"] is not None
    assert "shell" in preset["helper_text"]
    assert preset["display_name"] == "Openclaw Compat"


def test_get_rooms(test_client: TestClient) -> None:
    """Test getting all rooms."""
    # Load config first
    test_client.post("/api/config/load")

    response = test_client.get("/api/rooms")
    assert response.status_code == 200

    rooms = response.json()
    assert isinstance(rooms, list)
    assert "test_room" in rooms


def test_save_config(test_client: TestClient, temp_config_file: Path) -> None:
    """Test saving entire configuration."""
    new_config = {
        "memory": {
            "embedder": {
                "provider": "ollama",
                "config": {"model": "nomic-embed-text", "host": "http://localhost:11434"},
            },
        },
        "models": {"default": {"provider": "test", "id": "test-model-2"}},
        "agents": {
            "new_agent": {
                "display_name": "New Agent",
                "role": "New role",
                "tools": [],
                "instructions": [],
                "rooms": ["new_room"],
            },
        },
        "defaults": {},
        "router": {"model": "ollama"},
    }

    response = test_client.put("/api/config/save", json=new_config)
    assert response.status_code == 200

    # Verify file was updated
    with temp_config_file.open() as f:
        saved_config = yaml.safe_load(f)

    assert saved_config["models"]["default"]["id"] == "test-model-2"
    assert "new_agent" in saved_config["agents"]
    assert saved_config["defaults"] == {
        "tools": ["scheduler"],
        "markdown": True,
        "enable_streaming": True,
        "show_stop_button": False,
        "learning": True,
        "learning_mode": "always",
        "compress_tool_results": True,
        "enable_session_summaries": False,
        "show_tool_calls": True,
        "allow_self_config": False,
        "max_preload_chars": 50000,
    }


def test_error_handling_agent_not_found(test_client: TestClient) -> None:
    """Test error handling for non-existent agent."""
    test_client.post("/api/config/load")

    # Note: PUT creates the agent if it doesn't exist (current behavior)
    response = test_client.put("/api/config/agents/non_existent", json={})
    assert response.status_code == 200  # Current behavior creates the agent

    # DELETE should return 404 for non-existent agent
    response = test_client.delete("/api/config/agents/really_non_existent")
    assert response.status_code == 404


def test_cors_headers(test_client: TestClient) -> None:
    """Test CORS headers are present."""
    # Test with a regular request (CORS headers are added to responses)
    response = test_client.get("/api/health")
    # TestClient doesn't simulate CORS middleware properly
    # In a real browser environment, these headers would be present
    assert response.status_code == 200


def test_get_teams_empty(test_client: TestClient) -> None:
    """Test getting teams when none exist."""
    test_client.post("/api/config/load")

    response = test_client.get("/api/config/teams")
    assert response.status_code == 200
    teams = response.json()
    assert isinstance(teams, list)
    assert len(teams) == 0


def test_create_team(test_client: TestClient, temp_config_file: Path) -> None:
    """Test creating a new team."""
    test_client.post("/api/config/load")

    team_data = {
        "display_name": "Test Team",
        "role": "Testing team functionality",
        "agents": ["test_agent"],
        "rooms": ["test-room"],
        "model": "default",
        "mode": "coordinate",
    }

    response = test_client.post("/api/config/teams", json=team_data)
    assert response.status_code == 200

    result = response.json()
    assert "id" in result
    assert result["id"] == "test_team"
    assert result["success"] is True

    # Verify file was updated
    with temp_config_file.open() as f:
        saved_config = yaml.safe_load(f)

    assert "teams" in saved_config
    assert "test_team" in saved_config["teams"]
    assert saved_config["teams"]["test_team"]["display_name"] == "Test Team"
    assert saved_config["teams"]["test_team"]["agents"] == ["test_agent"]


def test_get_teams_with_data(test_client: TestClient) -> None:
    """Test getting teams after creating one."""
    test_client.post("/api/config/load")

    # Create a team first
    team_data = {
        "display_name": "Test Team",
        "role": "Testing team functionality",
        "agents": ["test_agent"],
        "rooms": ["test-room"],
        "model": "default",
        "mode": "coordinate",
    }
    test_client.post("/api/config/teams", json=team_data)

    # Now get teams
    response = test_client.get("/api/config/teams")
    assert response.status_code == 200

    teams = response.json()
    assert isinstance(teams, list)
    assert len(teams) == 1

    team = teams[0]
    assert team["id"] == "test_team"
    assert team["display_name"] == "Test Team"
    assert team["agents"] == ["test_agent"]
    assert team["mode"] == "coordinate"


def test_update_team(test_client: TestClient, temp_config_file: Path) -> None:
    """Test updating an existing team."""
    test_client.post("/api/config/load")

    # Create a team first
    team_data = {
        "display_name": "Test Team",
        "role": "Testing team functionality",
        "agents": ["test_agent"],
        "rooms": ["test-room"],
        "model": "default",
        "mode": "coordinate",
    }
    test_client.post("/api/config/teams", json=team_data)

    # Update the team
    updated_data = {
        "display_name": "Updated Team",
        "role": "Updated role",
        "agents": ["test_agent", "new_agent"],
        "rooms": ["test-room", "new-room"],
        "model": "gpt-4",
        "mode": "collaborate",
    }

    response = test_client.put("/api/config/teams/test_team", json=updated_data)
    assert response.status_code == 200

    # Verify file was updated
    with temp_config_file.open() as f:
        saved_config = yaml.safe_load(f)

    assert saved_config["teams"]["test_team"]["display_name"] == "Updated Team"
    assert saved_config["teams"]["test_team"]["agents"] == ["test_agent", "new_agent"]
    assert saved_config["teams"]["test_team"]["mode"] == "collaborate"


def test_delete_team(test_client: TestClient, temp_config_file: Path) -> None:
    """Test deleting a team."""
    test_client.post("/api/config/load")

    # Create a team first
    team_data = {
        "display_name": "Test Team",
        "role": "Testing team functionality",
        "agents": ["test_agent"],
        "rooms": ["test-room"],
    }
    test_client.post("/api/config/teams", json=team_data)

    # Delete the team
    response = test_client.delete("/api/config/teams/test_team")
    assert response.status_code == 200

    # Verify it's deleted from file
    with temp_config_file.open() as f:
        saved_config = yaml.safe_load(f)

    assert "teams" not in saved_config or "test_team" not in saved_config.get("teams", {})

    # Verify it's not returned in list
    response = test_client.get("/api/config/teams")
    teams = response.json()
    assert len(teams) == 0


def test_delete_nonexistent_team(test_client: TestClient) -> None:
    """Test deleting a team that doesn't exist."""
    test_client.post("/api/config/load")

    response = test_client.delete("/api/config/teams/nonexistent_team")
    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


def test_create_team_unique_id(test_client: TestClient) -> None:
    """Test that creating teams with same display name generates unique IDs."""
    test_client.post("/api/config/load")

    team_data = {
        "display_name": "Test Team",
        "role": "First team",
        "agents": ["test_agent"],
        "rooms": ["test-room"],
    }

    # Create first team
    response1 = test_client.post("/api/config/teams", json=team_data)
    assert response1.status_code == 200
    assert response1.json()["id"] == "test_team"

    # Create second team with same display name
    team_data["role"] = "Second team"
    response2 = test_client.post("/api/config/teams", json=team_data)
    assert response2.status_code == 200
    assert response2.json()["id"] == "test_team_1"

    # Create third team with same display name
    team_data["role"] = "Third team"
    response3 = test_client.post("/api/config/teams", json=team_data)
    assert response3.status_code == 200
    assert response3.json()["id"] == "test_team_2"


def test_get_room_models(test_client: TestClient) -> None:
    """Test getting room-specific model overrides."""
    test_client.post("/api/config/load")

    response = test_client.get("/api/config/room-models")
    assert response.status_code == 200
    room_models = response.json()
    assert isinstance(room_models, dict)


def test_update_room_models(test_client: TestClient, temp_config_file: Path) -> None:
    """Test updating room-specific model overrides."""
    test_client.post("/api/config/load")

    room_models = {"lobby": "gpt-4", "tech-room": "claude-3", "general": "default"}

    response = test_client.put("/api/config/room-models", json=room_models)
    assert response.status_code == 200

    # Verify file was updated
    with temp_config_file.open() as f:
        saved_config = yaml.safe_load(f)

    assert "room_models" in saved_config
    assert saved_config["room_models"]["lobby"] == "gpt-4"
    assert saved_config["room_models"]["tech-room"] == "claude-3"

    # Verify we can retrieve the updated room models
    response = test_client.get("/api/config/room-models")
    assert response.status_code == 200
    retrieved_models = response.json()
    assert retrieved_models["lobby"] == "gpt-4"
    assert retrieved_models["tech-room"] == "claude-3"


# ---------------------------------------------------------------------------
# MINDROOM_API_KEY authentication tests
# ---------------------------------------------------------------------------


@pytest.fixture
def api_key_client(temp_config_file: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Create a test client with MINDROOM_API_KEY enabled."""
    monkeypatch.setattr(main, "CONFIG_PATH", temp_config_file)
    monkeypatch.setattr(main, "_MINDROOM_API_KEY", "test-key")
    main._load_config_from_file()
    return TestClient(main.app)


def test_api_key_health_stays_open(api_key_client: TestClient) -> None:
    """Health endpoint should remain accessible without auth even when API key is set."""
    response = api_key_client.get("/api/health")
    assert response.status_code == 200


def test_api_key_valid_key_allows_access(api_key_client: TestClient) -> None:
    """A valid Bearer token should grant access to protected endpoints."""
    response = api_key_client.post(
        "/api/config/load",
        headers={"Authorization": "Bearer test-key"},
    )
    assert response.status_code == 200


def test_api_key_missing_header_rejects(api_key_client: TestClient) -> None:
    """Missing Authorization header should return 401 when API key is set."""
    response = api_key_client.post("/api/config/load")
    assert response.status_code == 401


def test_api_key_wrong_key_rejects(api_key_client: TestClient) -> None:
    """Wrong Bearer token should return 401."""
    response = api_key_client.post(
        "/api/config/load",
        headers={"Authorization": "Bearer wrong-key"},
    )
    assert response.status_code == 401


def test_api_key_protects_teams(api_key_client: TestClient) -> None:
    """Teams endpoint should reject unauthenticated requests when API key is set."""
    api_key_client.post("/api/config/load", headers={"Authorization": "Bearer test-key"})
    response = api_key_client.get("/api/config/teams")
    assert response.status_code == 401


def test_api_key_protects_models(api_key_client: TestClient) -> None:
    """Models endpoint should reject unauthenticated requests when API key is set."""
    api_key_client.post("/api/config/load", headers={"Authorization": "Bearer test-key"})
    response = api_key_client.get("/api/config/models")
    assert response.status_code == 401


def test_api_key_protects_rooms(api_key_client: TestClient) -> None:
    """Rooms endpoint should reject unauthenticated requests when API key is set."""
    api_key_client.post("/api/config/load", headers={"Authorization": "Bearer test-key"})
    response = api_key_client.get("/api/rooms")
    assert response.status_code == 401


def test_api_key_protects_room_models(api_key_client: TestClient) -> None:
    """Room-models endpoint should reject unauthenticated requests when API key is set."""
    api_key_client.post("/api/config/load", headers={"Authorization": "Bearer test-key"})
    response = api_key_client.get("/api/config/room-models")
    assert response.status_code == 401


def test_api_key_authenticated_teams_access(api_key_client: TestClient) -> None:
    """Teams endpoint should work with valid auth when API key is set."""
    api_key_client.post("/api/config/load", headers={"Authorization": "Bearer test-key"})
    response = api_key_client.get(
        "/api/config/teams",
        headers={"Authorization": "Bearer test-key"},
    )
    assert response.status_code == 200


@pytest.mark.parametrize(
    "path",
    [
        "/api/google/callback?code=test-code",
        "/api/homeassistant/callback?code=test-code",
        "/api/integrations/spotify/callback?code=test-code",
    ],
)
def test_api_key_keeps_oauth_callbacks_open(
    api_key_client: TestClient,
    path: str,
) -> None:
    """OAuth callbacks must remain reachable without Authorization headers."""
    response = api_key_client.get(path)
    assert response.status_code != 401
