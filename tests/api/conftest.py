"""Pytest configuration and fixtures for widget backend tests."""

# Import the app after we can mock the config path
import tempfile
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient


@pytest.fixture
def temp_config_file() -> Generator[Path, None, None]:
    """Create a temporary config file for testing."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        config_data = {
            "models": {"default": {"provider": "ollama", "id": "test-model"}},
            "agents": {
                "test_agent": {
                    "display_name": "Test Agent",
                    "role": "A test agent",
                    "tools": ["calculator"],
                    "instructions": ["Test instruction"],
                    "rooms": ["test_room"],
                    "num_history_runs": 5,
                },
            },
            "defaults": {"num_history_runs": 5, "markdown": True},
        }
        yaml.dump(config_data, f)
        temp_path = Path(f.name)

    yield temp_path

    # Cleanup
    temp_path.unlink(missing_ok=True)


@pytest.fixture
def test_client(temp_config_file: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Create a test client with mocked config file."""
    # Mock the config file path before importing
    from mindroom.api import main  # noqa: PLC0415

    monkeypatch.setattr(main, "CONFIG_PATH", temp_config_file)

    # Force reload of config
    main.load_config_from_file()

    # Create test client
    return TestClient(main.app)


@pytest.fixture
def sample_agent_data() -> dict[str, Any]:
    """Sample agent data for testing."""
    return {
        "display_name": "New Test Agent",
        "role": "A new test agent for testing",
        "tools": ["file", "shell"],
        "instructions": ["Do something", "Do something else"],
        "rooms": ["lobby", "dev"],
        "num_history_runs": 3,
    }
