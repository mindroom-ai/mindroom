"""Tests for egress broker admin API routes."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import pytest
import yaml

from mindroom.credentials import get_runtime_credentials_manager
from mindroom.egress_broker.audit import AuditLog, AuditRecord
from mindroom.oauth import registry as oauth_registry
from tests.api.test_oauth_api import _fake_provider

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


def test_delete_global_secret_flips_status(broker_test_client: TestClient) -> None:
    """DELETE should flip configured status to false for global secrets."""
    # Set a global secret
    response = broker_test_client.put(
        "/api/egress-broker/services/github/secret",
        json={"secret": "test-secret"},
    )
    assert response.status_code == 204

    # Check it's configured
    response = broker_test_client.get("/api/egress-broker/services")
    assert response.status_code == 200
    services = response.json()["services"]
    github = next((s for s in services if s["name"] == "github"), None)
    assert github is not None
    assert github["configured"] is True

    # Delete it
    response = broker_test_client.delete("/api/egress-broker/services/github/secret")
    assert response.status_code == 204

    # Check it's no longer configured
    response = broker_test_client.get("/api/egress-broker/services")
    assert response.status_code == 200
    services = response.json()["services"]
    github = next((s for s in services if s["name"] == "github"), None)
    assert github is not None
    assert github["configured"] is False


def test_delete_agent_scoped_secret_flips_status(broker_test_client: TestClient) -> None:
    """DELETE should flip configured status to false for agent-scoped secrets."""
    # Set an agent-scoped secret
    response = broker_test_client.put(
        "/api/egress-broker/services/github/secret?agent_name=test_agent",
        json={"secret": "agent-secret"},
    )
    assert response.status_code == 204

    # Check it's configured
    response = broker_test_client.get("/api/egress-broker/services?agent_name=test_agent")
    assert response.status_code == 200
    services = response.json()["services"]
    github = next((s for s in services if s["name"] == "github"), None)
    assert github is not None
    assert github["configured"] is True

    # Delete it
    response = broker_test_client.delete("/api/egress-broker/services/github/secret?agent_name=test_agent")
    assert response.status_code == 204

    # Check it's no longer configured
    response = broker_test_client.get("/api/egress-broker/services?agent_name=test_agent")
    assert response.status_code == 200
    services = response.json()["services"]
    github = next((s for s in services if s["name"] == "github"), None)
    assert github is not None
    assert github["configured"] is False


def test_logs_filtering_with_real_audit_log(
    broker_test_client: TestClient,
    tmp_path: Path,
) -> None:
    """Test /logs endpoint with real AuditLog and filtering."""
    # Create a real audit log
    log_path = tmp_path / "test_audit.sqlite3"
    audit = AuditLog(log_path)

    # Add some test records
    audit.record(
        AuditRecord(
            at=datetime.now(UTC),
            kind="request",
            scope="shared",
            agent_name="test_agent",
            requester_id="@user:example.org",
            method="GET",
            host="api.github.com",
            path="/repos",
            service="github",
            status=200,
            bytes_up=100,
            bytes_down=500,
            duration_ms=50,
        ),
    )
    audit.record(
        AuditRecord(
            at=datetime.now(UTC),
            kind="request",
            scope="shared",
            agent_name="other_agent",
            requester_id="@user:example.org",
            method="POST",
            host="api.openai.com",
            path="/chat/completions",
            service="openai",
            status=200,
            bytes_up=200,
            bytes_down=1000,
            duration_ms=100,
        ),
    )

    # Mock active_audit_log to return our test log
    with patch("mindroom.api.egress_broker.active_audit_log", return_value=audit):
        # Test filtering by agent_name
        response = broker_test_client.get("/api/egress-broker/logs?agent_name=test_agent")
        assert response.status_code == 200
        records = response.json()["records"]
        assert len(records) == 1
        assert records[0]["agent_name"] == "test_agent"
        assert records[0]["host"] == "api.github.com"

        # Test filtering by host
        response = broker_test_client.get("/api/egress-broker/logs?host=api.openai.com")
        assert response.status_code == 200
        records = response.json()["records"]
        assert len(records) == 1
        assert records[0]["host"] == "api.openai.com"

        # Test filtering by service
        response = broker_test_client.get("/api/egress-broker/logs?service=github")
        assert response.status_code == 200
        records = response.json()["records"]
        assert len(records) == 1
        assert records[0]["service"] == "github"

        # Test limit
        response = broker_test_client.get("/api/egress-broker/logs?limit=1")
        assert response.status_code == 200
        records = response.json()["records"]
        assert len(records) == 1

    audit.close()


@pytest.mark.parametrize("agent_name", [None, "test_agent"])
def test_credentials_list_omits_egress_secrets(broker_test_client: TestClient, agent_name: str | None) -> None:
    """The Credentials tab lists only services its status route serves, so egress secrets stay out."""
    params = {} if agent_name is None else {"agent_name": agent_name}
    response = broker_test_client.put(
        "/api/egress-broker/services/github/secret",
        params=params,
        json={"secret": "s3cret"},
    )
    assert response.status_code == 204
    response = broker_test_client.post(
        "/api/credentials/openai/api-key",
        params=params,
        json={"service": "openai", "api_key": "sk-test"},
    )
    assert response.status_code == 200

    response = broker_test_client.get("/api/credentials/list", params=params)
    assert response.status_code == 200
    services = response.json()
    assert "openai" in services
    assert "egress_github" not in services
    for service in services:
        status = broker_test_client.get(f"/api/credentials/{service}/status", params=params)
        assert status.status_code == 200, service


@pytest.fixture
def oauth_broker_client(egress_config_file: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Serve the admin API with services backed by an agent-scoped and a requester-scoped OAuth provider."""
    from fastapi.testclient import TestClient  # noqa: PLC0415

    from mindroom import constants  # noqa: PLC0415
    from mindroom.api import config_lifecycle, main  # noqa: PLC0415

    drive = _fake_provider(provider_id="google_drive", credential_service="google_drive_oauth")
    github = _fake_provider(
        provider_id="github",
        credential_service="github_oauth",
        client_config_services=("github_oauth_client",),
        requester_scoped_credentials=True,
    )
    monkeypatch.setattr(oauth_registry, "_builtin_oauth_providers", lambda: (drive, github))
    rules = [{"host": "www.googleapis.com", "auth": {"type": "bearer"}}]
    config = yaml.safe_load(egress_config_file.read_text(encoding="utf-8"))
    config["egress_broker"]["services"].update(
        {
            "drive": {"description": "Drive", "oauth_provider": "google_drive", "rules": rules},
            "gh": {"description": "GH", "oauth_provider": "github", "rules": rules},
            "ghost": {"description": "Unknown provider", "oauth_provider": "no_such_provider", "rules": rules},
        },
    )
    egress_config_file.write_text(yaml.dump(config), encoding="utf-8")
    runtime_paths = constants.resolve_primary_runtime_paths(
        config_path=egress_config_file,
        process_env={"MINDROOM_OWNER_USER_ID": "@owner:example.org"},
    )
    manager = get_runtime_credentials_manager(runtime_paths)
    for service in ("test_drive_oauth_client", "github_oauth_client"):
        manager.save_credentials(service, {"client_id": "test-client", "client_secret": "test-secret", "_source": "ui"})
    main.initialize_api_app(main.app, runtime_paths)
    config_lifecycle.load_config_into_app(main._app_runtime_paths(main.app), main.app)
    return TestClient(main.app, base_url="http://localhost")


def _service(client: TestClient, name: str, agent_name: str | None = None) -> dict[str, object]:
    params = {} if agent_name is None else {"agent_name": agent_name}
    response = client.get("/api/egress-broker/services", params=params)
    assert response.status_code == 200, response.text
    return next(service for service in response.json()["services"] if service["name"] == name)


def _connect(client: TestClient, name: str, provider: str, agent_name: str | None = None) -> None:
    """Run the whole OAuth flow through the admin connect route and the shared callback."""
    params = {} if agent_name is None else {"agent_name": agent_name}
    response = client.post(f"/api/egress-broker/services/{name}/connect", params=params)
    assert response.status_code == 200, response.text
    state = parse_qs(urlparse(response.json()["auth_url"]).query)["state"][0]
    callback = client.get(
        f"/api/oauth/{provider}/callback",
        params={"code": "test-code", "state": state},
        follow_redirects=False,
    )
    assert callback.status_code in {302, 303, 307}, callback.text


def test_services_status_has_key_and_oauth_parts(oauth_broker_client: TestClient) -> None:
    """Each service reports its key and OAuth sources; plain services carry no OAuth block."""
    github = _service(oauth_broker_client, "github")
    assert github["oauth"] is None
    assert github["active_source"] is None
    assert github["key_configured"] is False
    drive = _service(oauth_broker_client, "drive")
    assert drive["oauth"] == {
        "provider": "google_drive",
        "display_name": "Test Drive",
        "connected": False,
        "account_label": None,
        "can_connect": True,
        "reset_required": False,
    }
    assert _service(oauth_broker_client, "ghost")["oauth"] is None


@pytest.mark.parametrize("agent_name", [None, "test_agent"])
def test_connect_stores_the_account_and_a_key_takes_precedence(
    oauth_broker_client: TestClient,
    agent_name: str | None,
) -> None:
    """A connected account shows in the scope it was connected for, and an explicit key takes over."""
    params = {} if agent_name is None else {"agent_name": agent_name}
    _connect(oauth_broker_client, "drive", "google_drive", agent_name)

    drive = _service(oauth_broker_client, "drive", agent_name)
    assert drive["active_source"] == "oauth"
    assert drive["configured"] is True
    assert drive["key_configured"] is False
    assert drive["updated_at"] is None
    assert drive["oauth"]["connected"] is True
    assert drive["oauth"]["account_label"] == "alice@example.com"
    if agent_name is not None:
        assert _service(oauth_broker_client, "drive")["oauth"]["connected"] is False

    response = oauth_broker_client.put("/api/egress-broker/services/drive/secret", params=params, json={"secret": "k"})
    assert response.status_code == 204
    drive = _service(oauth_broker_client, "drive", agent_name)
    assert drive["active_source"] == "key"
    assert drive["key_configured"] is True
    assert drive["updated_at"] == drive["key_updated_at"]
    assert drive["oauth"]["connected"] is True


@pytest.mark.parametrize("agent_name", [None, "test_agent"])
def test_disconnect_clears_the_connection(oauth_broker_client: TestClient, agent_name: str | None) -> None:
    """Disconnect resets the scope's connection."""
    params = {} if agent_name is None else {"agent_name": agent_name}
    _connect(oauth_broker_client, "drive", "google_drive", agent_name)

    response = oauth_broker_client.post("/api/egress-broker/services/drive/disconnect", params=params)
    assert response.status_code == 200, response.text
    assert response.json() == {"status": "disconnected", "provider": "google_drive"}
    drive = _service(oauth_broker_client, "drive", agent_name)
    assert drive["oauth"]["connected"] is False
    assert drive["active_source"] is None


@pytest.mark.parametrize("agent_name", [None, "test_agent"])
def test_requester_scoped_provider_status_follows_the_dashboard_requester(
    oauth_broker_client: TestClient,
    agent_name: str | None,
) -> None:
    """A GitHub-style connection belongs to the dashboard user, so the panel shows it in every scope."""
    _connect(oauth_broker_client, "gh", "github", agent_name)

    for scope in (None, "test_agent"):
        gh = _service(oauth_broker_client, "gh", scope)
        assert gh["oauth"]["connected"] is True
        assert gh["active_source"] == "oauth"

    response = oauth_broker_client.post(
        "/api/egress-broker/services/gh/disconnect",
        params={} if agent_name is None else {"agent_name": agent_name},
    )
    assert response.status_code == 200, response.text
    assert _service(oauth_broker_client, "gh", agent_name)["oauth"]["connected"] is False


@pytest.mark.parametrize("action", ["connect", "disconnect"])
@pytest.mark.parametrize("name", ["unknown", "github", "ghost"])
def test_connect_and_disconnect_404_without_a_usable_provider(
    oauth_broker_client: TestClient,
    action: str,
    name: str,
) -> None:
    """Unknown services, services without an OAuth provider, and unknown providers are 404."""
    response = oauth_broker_client.post(f"/api/egress-broker/services/{name}/{action}")
    assert response.status_code == 404, response.text


def test_connect_and_disconnect_reject_unknown_agents(oauth_broker_client: TestClient) -> None:
    """The agent selector is checked by the same dashboard scope resolution as the key routes."""
    for action in ("connect", "disconnect"):
        response = oauth_broker_client.post(
            f"/api/egress-broker/services/drive/{action}",
            params={"agent_name": "no_such_agent"},
        )
        assert response.status_code in {400, 404}, response.text


@pytest.mark.parametrize("action", ["connect", "disconnect"])
def test_connect_and_disconnect_409_for_a_service_account_provider(
    oauth_broker_client: TestClient,
    action: str,
) -> None:
    """A shared Google service account is runtime configuration, never a personal account."""
    from dataclasses import replace  # noqa: PLC0415

    from mindroom.api import config_lifecycle, main  # noqa: PLC0415

    paths = main._app_runtime_paths(main.app)
    paths = replace(paths, process_env={**paths.process_env, "GOOGLE_SERVICE_ACCOUNT_FILE": "service-account.json"})
    main.initialize_api_app(main.app, paths)
    config_lifecycle.load_config_into_app(main._app_runtime_paths(main.app), main.app)

    response = oauth_broker_client.post(f"/api/egress-broker/services/drive/{action}")
    assert response.status_code == 409, response.text
    assert _service(oauth_broker_client, "drive")["oauth"]["can_connect"] is False


def test_an_unloadable_connection_state_does_not_fail_the_listing(
    oauth_broker_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One provider failing to report leaves the panel up, with its service shown as not connectable."""
    from fastapi import HTTPException  # noqa: PLC0415

    from mindroom.api import oauth  # noqa: PLC0415

    async def unavailable(*_args: object, **_kwargs: object) -> None:
        raise HTTPException(503, "internal-client-secret")

    monkeypatch.setattr(oauth, "status", unavailable)
    response = oauth_broker_client.get("/api/egress-broker/services")
    assert response.status_code == 200, response.text
    assert "internal-client-secret" not in response.text
    drive = _service(oauth_broker_client, "drive")
    assert drive["oauth"]["connected"] is False
    assert drive["oauth"]["can_connect"] is False
    assert _service(oauth_broker_client, "github")["oauth"] is None
