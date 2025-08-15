"""Tests for file watching functionality."""

import time
from pathlib import Path

import yaml
from fastapi.testclient import TestClient


def test_file_watcher_detects_changes(test_client: TestClient, temp_config_file: Path) -> None:
    """Test that external config changes can be loaded."""
    # Load initial config
    response = test_client.post("/api/config/load")
    assert response.status_code == 200
    response.json()  # Ensure config is loaded

    # Modify the config file externally
    with temp_config_file.open() as f:
        config = yaml.safe_load(f)

    config["agents"]["external_agent"] = {
        "display_name": "External Agent",
        "role": "Added externally",
        "tools": [],
        "instructions": [],
        "rooms": ["external"],
        "num_history_runs": 5,
    }

    with temp_config_file.open("w") as f:
        yaml.dump(config, f)

    # In a real scenario, the file watcher would auto-reload
    # For testing, we manually trigger a reload
    from mindroom.api import main  # noqa: PLC0415

    main.load_config_from_file()

    # Check that the config was reloaded
    response = test_client.get("/api/config/agents")
    assert response.status_code == 200
    agents = response.json()

    # Find the externally added agent
    external_agent = next((a for a in agents if a["id"] == "external_agent"), None)
    assert external_agent is not None
    assert external_agent["display_name"] == "External Agent"


def test_config_format_validation(test_client: TestClient, temp_config_file: Path) -> None:
    """Test that invalid config format is handled gracefully."""
    # Write invalid YAML
    with temp_config_file.open("w") as f:
        f.write("invalid: yaml: content: [")

    # The app should handle this gracefully
    response = test_client.get("/api/health")
    assert response.status_code == 200

    # Fix the config
    valid_config = {
        "models": {"default": {"provider": "test", "id": "test"}},
        "agents": {},
        "defaults": {"num_history_runs": 5},
    }

    with temp_config_file.open("w") as f:
        yaml.dump(valid_config, f)

    time.sleep(0.5)

    # Should be able to load the fixed config
    response = test_client.post("/api/config/load")
    assert response.status_code == 200
