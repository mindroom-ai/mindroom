"""Pytest configuration and fixtures for dashboard backend tests."""

# Import the app after we can mock the config path
from collections.abc import Generator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import yaml
from fastapi.testclient import TestClient

from mindroom.api import config_lifecycle

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths


def trusted_upstream_headers(
    *,
    user_id: str = "alice",
    email: str = "alice@example.com",
    matrix_user_id: str = "@alice:example.org",
) -> dict[str, str]:
    """Return trusted-upstream auth headers for API tests."""
    return {
        "X-Trusted-User": user_id,
        "X-Trusted-Email": email,
        "X-Trusted-Matrix-User": matrix_user_id,
    }


def use_trusted_upstream_runtime(api_app: object) -> "RuntimePaths":
    """Reinitialize one API app with trusted-upstream auth enabled."""
    from mindroom import constants  # noqa: PLC0415
    from mindroom.api import main  # noqa: PLC0415

    runtime_paths = main._app_runtime_paths(api_app)
    trusted_runtime_paths = constants.resolve_primary_runtime_paths(
        config_path=runtime_paths.config_path,
        storage_path=runtime_paths.storage_root,
        process_env={
            "MINDROOM_TRUSTED_UPSTREAM_AUTH_ENABLED": "true",
            "MINDROOM_TRUSTED_UPSTREAM_USER_ID_HEADER": "X-Trusted-User",
            "MINDROOM_TRUSTED_UPSTREAM_EMAIL_HEADER": "X-Trusted-Email",
            "MINDROOM_TRUSTED_UPSTREAM_MATRIX_USER_ID_HEADER": "X-Trusted-Matrix-User",
        },
    )
    main.initialize_api_app(api_app, trusted_runtime_paths)
    return trusted_runtime_paths


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
    config_lifecycle.load_config_into_app(main._app_runtime_paths(main.app), main.app)

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
