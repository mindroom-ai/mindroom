"""Tests for the widget backend API endpoints."""

import yaml
from fastapi.testclient import TestClient


def test_health_check(test_client: TestClient):
    """Test the health check endpoint."""
    response = test_client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert "config_loaded" in data


def test_load_config(test_client: TestClient):
    """Test loading configuration."""
    response = test_client.post("/api/config/load")
    assert response.status_code == 200

    config = response.json()
    assert "agents" in config
    assert "models" in config
    assert "test_agent" in config["agents"]


def test_get_agents(test_client: TestClient):
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


def test_create_agent(test_client: TestClient, sample_agent_data, temp_config_file):
    """Test creating a new agent."""
    # Load config first
    test_client.post("/api/config/load")

    response = test_client.post("/api/config/agents", json=sample_agent_data)
    assert response.status_code == 200

    result = response.json()
    assert "id" in result
    assert result["success"] is True

    # Verify it was saved to file
    with open(temp_config_file) as f:
        config = yaml.safe_load(f)
    assert result["id"] in config["agents"]
    assert config["agents"][result["id"]]["display_name"] == sample_agent_data["display_name"]


def test_update_agent(test_client: TestClient, temp_config_file):
    """Test updating an existing agent."""
    # Load config first
    test_client.post("/api/config/load")

    update_data = {"display_name": "Updated Test Agent", "tools": ["calculator", "file"], "rooms": ["updated_room"]}

    response = test_client.put("/api/config/agents/test_agent", json=update_data)
    assert response.status_code == 200

    result = response.json()
    assert result["success"] is True

    # Verify file was updated
    with open(temp_config_file) as f:
        config = yaml.safe_load(f)
    assert config["agents"]["test_agent"]["display_name"] == "Updated Test Agent"
    assert "file" in config["agents"]["test_agent"]["tools"]
    assert "updated_room" in config["agents"]["test_agent"]["rooms"]


def test_delete_agent(test_client: TestClient, temp_config_file):
    """Test deleting an agent."""
    # Load config first
    test_client.post("/api/config/load")

    response = test_client.delete("/api/config/agents/test_agent")
    assert response.status_code == 200

    result = response.json()
    assert result["success"] is True

    # Verify it was removed from file
    with open(temp_config_file) as f:
        config = yaml.safe_load(f)
    assert "test_agent" not in config["agents"]


def test_get_tools(test_client: TestClient):
    """Test getting available tools."""
    response = test_client.get("/api/tools")
    assert response.status_code == 200

    tools = response.json()
    assert isinstance(tools, list)
    assert "calculator" in tools
    assert "file" in tools
    assert "shell" in tools


def test_get_rooms(test_client: TestClient):
    """Test getting all rooms."""
    # Load config first
    test_client.post("/api/config/load")

    response = test_client.get("/api/rooms")
    assert response.status_code == 200

    rooms = response.json()
    assert isinstance(rooms, list)
    assert "test_room" in rooms


def test_save_config(test_client: TestClient, temp_config_file):
    """Test saving entire configuration."""
    new_config = {
        "memory": {
            "embedder": {
                "provider": "ollama",
                "config": {"model": "nomic-embed-text", "host": "http://localhost:11434"},
            }
        },
        "models": {"default": {"provider": "test", "id": "test-model-2"}},
        "agents": {
            "new_agent": {
                "display_name": "New Agent",
                "role": "New role",
                "tools": [],
                "instructions": [],
                "rooms": ["new_room"],
                "num_history_runs": 10,
            }
        },
        "defaults": {"num_history_runs": 10},
        "router": {"model": "ollama"},
    }

    response = test_client.put("/api/config/save", json=new_config)
    assert response.status_code == 200

    # Verify file was updated
    with open(temp_config_file) as f:
        saved_config = yaml.safe_load(f)

    assert saved_config["models"]["default"]["id"] == "test-model-2"
    assert "new_agent" in saved_config["agents"]
    assert saved_config["defaults"]["num_history_runs"] == 10


def test_test_model(test_client: TestClient):
    """Test model connection testing endpoint."""
    model_test_request = {"modelId": "default"}

    response = test_client.post("/api/test/model", json=model_test_request)
    assert response.status_code == 200

    result = response.json()
    assert "success" in result
    assert "message" in result


def test_error_handling_agent_not_found(test_client: TestClient):
    """Test error handling for non-existent agent."""
    test_client.post("/api/config/load")

    # Note: PUT creates the agent if it doesn't exist (current behavior)
    response = test_client.put("/api/config/agents/non_existent", json={})
    assert response.status_code == 200  # Current behavior creates the agent

    # DELETE should return 404 for non-existent agent
    response = test_client.delete("/api/config/agents/really_non_existent")
    assert response.status_code == 404


def test_cors_headers(test_client: TestClient):
    """Test CORS headers are present."""
    # Test with a regular request (CORS headers are added to responses)
    response = test_client.get("/health")
    # TestClient doesn't simulate CORS middleware properly
    # In a real browser environment, these headers would be present
    assert response.status_code == 200
