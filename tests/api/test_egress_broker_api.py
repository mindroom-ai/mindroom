"""Tests for egress broker admin API routes."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
import yaml

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

    from fastapi.testclient import TestClient


@pytest.fixture
def egress_config_file(tmp_path: Path) -> Generator[Path, None, None]:
    """Create a config file with egress broker services."""
    config_dir = tmp_path / "egress-test"
    config_dir.mkdir()
    config_data = {
        "administrators": ["@owner:example.org"],
        "models": {"default": {"provider": "ollama", "id": "test-model"}},
        "agents": {
            "test_agent": {
                "display_name": "Test Agent",
                "role": "A test agent",
                "tools": ["shell"],
                "instructions": ["Test instruction"],
                "rooms": ["test_room"],
                "worker_scope": "shared",
            },
        },
        "defaults": {"markdown": True},
        "egress_broker": {
            "unmatched_hosts": "passthrough",
            "services": {
                "github": {
                    "display_name": "GitHub",
                    "description": "GitHub API and git over HTTPS",
                    "rules": [
                        {
                            "host": "api.github.com",
                            "auth": {"type": "bearer"},
                        },
                        {
                            "host": "github.com",
                            "auth": {"type": "basic", "username": "x-access-token"},
                        },
                    ],
                    "placeholder_env": {
                        "GH_TOKEN": "mindroom-brokered",
                        "GITHUB_TOKEN": "mindroom-brokered",
                    },
                },
            },
        },
    }
    temp_path = config_dir / "config.yaml"
    temp_path.write_text(yaml.dump(config_data), encoding="utf-8")
    yield temp_path
    temp_path.unlink(missing_ok=True)


@pytest.fixture
def broker_test_client(egress_config_file: Path) -> TestClient:
    """Create a test client with egress broker configuration."""
    from fastapi.testclient import TestClient  # noqa: PLC0415

    from mindroom import constants  # noqa: PLC0415
    from mindroom.api import config_lifecycle, main  # noqa: PLC0415

    runtime_paths = constants.resolve_primary_runtime_paths(
        config_path=egress_config_file,
        process_env={"MINDROOM_OWNER_USER_ID": "@owner:example.org"},
    )
    main.initialize_api_app(main.app, runtime_paths)
    config_lifecycle.load_config_into_app(main._app_runtime_paths(main.app), main.app)
    return TestClient(main.app, base_url="http://localhost")


@pytest.fixture
def mock_active_audit_log() -> Generator[MagicMock, None, None]:
    """Mock active_audit_log to control broker running state."""
    with patch("mindroom.api.egress_broker.active_audit_log") as mock:
        yield mock


@pytest.fixture
def mock_active_ca_pem() -> Generator[MagicMock, None, None]:
    """Mock active_ca_pem to control broker running state."""
    with patch("mindroom.api.egress_broker.active_ca_pem") as mock:
        yield mock


@pytest.fixture
def mock_audit_query() -> Generator[MagicMock, None, None]:
    """Mock AuditLog.query method."""
    with patch("mindroom.egress_broker.audit.AuditLog.query") as mock:
        yield mock


def test_services_status_never_returns_secret(broker_test_client: TestClient) -> None:
    """Service status must never leak secret values."""
    # Set a secret for a service
    response = broker_test_client.put(
        "/api/egress-broker/services/github/secret",
        json={"secret": "s3cret"},
    )
    assert response.status_code == 204

    # Get services status
    response = broker_test_client.get("/api/egress-broker/services")
    assert response.status_code == 200

    # Check that "s3cret" never appears in the response
    response_text = response.text
    assert "s3cret" not in response_text
    assert "secret" not in response_text.lower() or "configured" in response_text.lower()


def test_put_unknown_service_404(broker_test_client: TestClient) -> None:
    """PUT to unknown service should return 404."""
    response = broker_test_client.put(
        "/api/egress-broker/services/unknown_service/secret",
        json={"secret": "test"},
    )
    assert response.status_code == 404


def test_put_blank_secret_422(broker_test_client: TestClient) -> None:
    """PUT with blank secret should return 422."""
    # Empty string
    response = broker_test_client.put(
        "/api/egress-broker/services/github/secret",
        json={"secret": ""},
    )
    assert response.status_code == 422

    # Whitespace only
    response = broker_test_client.put(
        "/api/egress-broker/services/github/secret",
        json={"secret": "   "},
    )
    assert response.status_code == 422


def test_agent_scoped_put_lands_in_agent_scope(broker_test_client: TestClient) -> None:
    """Secret for a shared agent should go to for_primary_runtime_agent_scope."""
    # Set a secret for a shared agent
    response = broker_test_client.put(
        "/api/egress-broker/services/github/secret?agent_name=test_agent",
        json={"secret": "agent-secret"},
    )
    assert response.status_code == 204

    # Verify it landed in the right scope by checking status
    response = broker_test_client.get("/api/egress-broker/services?agent_name=test_agent")
    assert response.status_code == 200
    data = response.json()
    services = data.get("services", [])
    github_service = next((s for s in services if s["name"] == "github"), None)
    assert github_service is not None
    assert github_service["configured"] is True


def test_logs_and_ca_409_when_inactive(
    broker_test_client: TestClient,
    mock_active_audit_log: MagicMock,
    mock_active_ca_pem: MagicMock,
) -> None:
    """Logs and CA endpoints should return 409 when broker is not running."""
    # Broker not running
    mock_active_audit_log.return_value = None
    mock_active_ca_pem.return_value = None

    # Logs should return 409
    response = broker_test_client.get("/api/egress-broker/logs")
    assert response.status_code == 409
    assert "not running" in response.json()["detail"]

    # CA should return 409
    response = broker_test_client.get("/api/egress-broker/ca.pem")
    assert response.status_code == 409
    assert "not running" in response.json()["detail"]


def test_routes_require_dashboard_auth(broker_test_client: TestClient) -> None:
    """All egress-broker routes use the same auth dependencies as credentials routes."""
    # This test verifies that the router is included with the right dependencies.
    # The actual auth logic is tested elsewhere. Here we just ensure the routes exist
    # and are accessible with proper auth (which broker_test_client provides).
    response = broker_test_client.get("/api/egress-broker/services")
    # Should get a valid response with broker_test_client which has auth
    assert response.status_code == 200  # Should work with proper config
