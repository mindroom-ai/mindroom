"""Pytest configuration and fixtures for dashboard backend tests."""

# Import the app after we can mock the config path
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient


@pytest.fixture
def temp_config_file(tmp_path: Path) -> Generator[Path, None, None]:
    """Create a temporary config file for testing."""
    config_dir = tmp_path / "api-runtime"
    config_dir.mkdir()
    config_data = {
        "models": {"default": {"provider": "ollama", "id": "test-model"}},
        "agents": {
            "test_agent": {
                "display_name": "Test Agent",
                "role": "A test agent",
                "tools": ["calculator"],
                "instructions": ["Test instruction"],
                "rooms": ["test_room"],
            },
        },
        "defaults": {"markdown": True},
    }
    temp_path = config_dir / "config.yaml"
    temp_path.write_text(yaml.dump(config_data), encoding="utf-8")

    yield temp_path

    # Cleanup
    temp_path.unlink(missing_ok=True)


@pytest.fixture
def test_client(temp_config_file: Path) -> TestClient:
    """Create a test client with mocked config file."""
    from mindroom import constants  # noqa: PLC0415
    from mindroom.api import main  # noqa: PLC0415

    runtime_paths = constants.resolve_primary_runtime_paths(config_path=temp_config_file, process_env={})
    main.initialize_api_app(main.app, runtime_paths)

    # Force reload of config
    main._load_config_from_file(main.app.state.runtime_paths, main.app)

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
    }
