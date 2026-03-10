"""Tests for the credentials API endpoints."""

from collections.abc import Generator
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from mindroom.api import credentials as credentials_api
from mindroom.credentials import CredentialsManager


@pytest.fixture
def temp_credentials_dir(tmp_path: Path) -> Path:
    """Create a temporary directory for credentials."""
    return tmp_path / "credentials"


@pytest.fixture
def mock_credentials_manager(temp_credentials_dir: Path) -> CredentialsManager:
    """Create a CredentialsManager with a temporary directory."""
    return CredentialsManager(temp_credentials_dir)


@pytest.fixture
def test_client(mock_credentials_manager: CredentialsManager) -> Generator[TestClient, None, None]:
    """Create a test client with mocked credentials manager."""
    # Import here to avoid circular dependencies
    from mindroom.api.credentials import router  # noqa: PLC0415

    app = FastAPI()
    app.include_router(router)

    # Mock the get_credentials_manager function
    with patch("mindroom.api.credentials.get_credentials_manager") as mock_get:
        mock_get.return_value = mock_credentials_manager
        client = TestClient(app)
        # Store the mock for use in tests
        client.mock_manager = mock_credentials_manager
        yield client


@pytest.fixture(autouse=True)
def clear_pending_oauth_state() -> Generator[None, None, None]:
    """Reset pending OAuth state between tests."""
    credentials_api._pending_oauth_states.clear()
    yield
    credentials_api._pending_oauth_states.clear()


class TestCredentialsAPI:
    """Test the credentials API endpoints."""

    def test_list_services_empty(self, test_client: TestClient) -> None:
        """Test listing services when none exist."""
        response = test_client.get("/api/credentials/list")
        assert response.status_code == 200
        assert response.json() == []

    def test_list_services_with_credentials(
        self,
        test_client: TestClient,
        mock_credentials_manager: CredentialsManager,
    ) -> None:
        """Test listing services with stored credentials."""
        # Add some credentials
        mock_credentials_manager.save_credentials("openai", {"api_key": "test-key"})
        mock_credentials_manager.save_credentials("anthropic", {"api_key": "test-key2"})

        response = test_client.get("/api/credentials/list")
        assert response.status_code == 200
        services = response.json()
        assert len(services) == 2
        assert "anthropic" in services
        assert "openai" in services

    def test_get_credential_status_not_found(self, test_client: TestClient) -> None:
        """Test getting status for a service without credentials."""
        response = test_client.get("/api/credentials/openai/status")
        assert response.status_code == 200
        data = response.json()
        assert data["service"] == "openai"
        assert data["has_credentials"] is False
        assert data["key_names"] is None

    def test_get_credential_status_exists(
        self,
        test_client: TestClient,
        mock_credentials_manager: CredentialsManager,
    ) -> None:
        """Test getting status for a service with credentials."""
        mock_credentials_manager.save_credentials(
            "openai",
            {"api_key": "test-key", "other_field": "value"},
        )

        response = test_client.get("/api/credentials/openai/status")
        assert response.status_code == 200
        data = response.json()
        assert data["service"] == "openai"
        assert data["has_credentials"] is True
        assert set(data["key_names"]) == {"api_key", "other_field"}

    def test_set_api_key(
        self,
        test_client: TestClient,
        mock_credentials_manager: CredentialsManager,
    ) -> None:
        """Test setting an API key."""
        response = test_client.post(
            "/api/credentials/openai/api-key",
            json={"service": "openai", "api_key": "sk-test123", "key_name": "api_key"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert "openai" in data["message"]

        # Verify the key was saved
        assert mock_credentials_manager.get_api_key("openai") == "sk-test123"

    def test_rejects_raw_worker_key_query_param(
        self,
        test_client: TestClient,
    ) -> None:
        """Credential API should reject raw worker_key targeting from callers."""
        response = test_client.post(
            "/api/credentials/openai?worker_key=worker-a",
            json={"credentials": {"api_key": "scoped-key"}},
        )
        assert response.status_code == 400
        assert "worker_key" in response.json()["detail"]

    def test_rejects_raw_source_worker_key_query_param(self, test_client: TestClient) -> None:
        """Credential copy endpoint should reject raw source_worker_key targeting."""
        response = test_client.post("/api/credentials/model:new/copy-from/model:old?source_worker_key=worker-a")
        assert response.status_code == 400
        assert "source_worker_key" in response.json()["detail"]

    def test_set_api_key_service_mismatch(self, test_client: TestClient) -> None:
        """Test setting an API key with mismatched service."""
        response = test_client.post(
            "/api/credentials/openai/api-key",
            json={"service": "anthropic", "api_key": "sk-test123", "key_name": "api_key"},
        )
        assert response.status_code == 400
        assert "Service mismatch" in response.json()["detail"]

    def test_get_api_key_not_found(self, test_client: TestClient) -> None:
        """Test getting API key status when not found."""
        response = test_client.get("/api/credentials/openai/api-key")
        assert response.status_code == 200
        data = response.json()
        assert data["service"] == "openai"
        assert data["has_key"] is False
        assert data["key_name"] == "api_key"

    def test_get_api_key_exists(
        self,
        test_client: TestClient,
        mock_credentials_manager: CredentialsManager,
    ) -> None:
        """Test getting API key status when it exists."""
        mock_credentials_manager.set_api_key("openai", "sk-test-key-123456789")

        response = test_client.get("/api/credentials/openai/api-key")
        assert response.status_code == 200
        data = response.json()
        assert data["service"] == "openai"
        assert data["has_key"] is True
        assert data["key_name"] == "api_key"
        assert data["masked_key"] == "sk-t...6789"

    def test_get_api_key_short(
        self,
        test_client: TestClient,
        mock_credentials_manager: CredentialsManager,
    ) -> None:
        """Test getting API key status with a short key."""
        mock_credentials_manager.set_api_key("openai", "short")

        response = test_client.get("/api/credentials/openai/api-key")
        assert response.status_code == 200
        data = response.json()
        assert data["masked_key"] == "****"

    def test_get_api_key_custom_name(
        self,
        test_client: TestClient,
        mock_credentials_manager: CredentialsManager,
    ) -> None:
        """Test getting API key with custom key name."""
        mock_credentials_manager.save_credentials("service", {"token": "my-token"})

        response = test_client.get("/api/credentials/service/api-key?key_name=token")
        assert response.status_code == 200
        data = response.json()
        assert data["has_key"] is True
        assert data["key_name"] == "token"

    def test_get_api_key_include_value(
        self,
        test_client: TestClient,
        mock_credentials_manager: CredentialsManager,
    ) -> None:
        """Test getting API key value when include_value=true."""
        mock_credentials_manager.save_credentials(
            "openai",
            {"api_key": "sk-real-secret", "_source": "ui"},
        )

        response = test_client.get("/api/credentials/openai/api-key?include_value=true")
        assert response.status_code == 200
        data = response.json()
        assert data["has_key"] is True
        assert data["api_key"] == "sk-real-secret"
        assert data["source"] == "ui"

    def test_delete_credentials(
        self,
        test_client: TestClient,
        mock_credentials_manager: CredentialsManager,
    ) -> None:
        """Test deleting credentials."""
        # First save some credentials
        mock_credentials_manager.save_credentials("openai", {"api_key": "test"})
        assert mock_credentials_manager.load_credentials("openai") is not None

        # Delete them
        response = test_client.delete("/api/credentials/openai")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert "deleted" in data["message"]

        # Verify they're gone
        assert mock_credentials_manager.load_credentials("openai") is None

    def test_delete_nonexistent_credentials(self, test_client: TestClient) -> None:
        """Test deleting credentials that don't exist."""
        response = test_client.delete("/api/credentials/nonexistent")
        assert response.status_code == 200
        # Should succeed even if nothing to delete

    def test_test_credentials_not_found(self, test_client: TestClient) -> None:
        """Test testing credentials when none exist."""
        response = test_client.post("/api/credentials/openai/test")
        assert response.status_code == 404
        assert "No credentials found" in response.json()["detail"]

    def test_test_credentials_exists(
        self,
        test_client: TestClient,
        mock_credentials_manager: CredentialsManager,
    ) -> None:
        """Test testing credentials when they exist."""
        mock_credentials_manager.save_credentials("openai", {"api_key": "test"})

        response = test_client.post("/api/credentials/openai/test")
        assert response.status_code == 200
        data = response.json()
        assert data["service"] == "openai"
        assert data["status"] == "success"
        assert "validation not implemented" in data["message"]

    def test_set_api_key_with_update(
        self,
        test_client: TestClient,
        mock_credentials_manager: CredentialsManager,
    ) -> None:
        """Test updating an existing API key."""
        # Set initial key
        mock_credentials_manager.set_api_key("openai", "old-key")

        # Update it
        response = test_client.post(
            "/api/credentials/openai/api-key",
            json={"service": "openai", "api_key": "new-key", "key_name": "api_key"},
        )
        assert response.status_code == 200

        # Verify it was updated
        assert mock_credentials_manager.get_api_key("openai") == "new-key"

    def test_set_api_key_preserves_other_fields(
        self,
        test_client: TestClient,
        mock_credentials_manager: CredentialsManager,
    ) -> None:
        """Test that setting an API key preserves other fields."""
        # Save initial credentials with multiple fields
        mock_credentials_manager.save_credentials(
            "service",
            {"api_key": "old", "other_field": "value"},
        )

        # Update just the API key
        response = test_client.post(
            "/api/credentials/service/api-key",
            json={"service": "service", "api_key": "new", "key_name": "api_key"},
        )
        assert response.status_code == 200

        # Verify both fields are present
        creds = mock_credentials_manager.load_credentials("service")
        assert creds is not None
        assert creds["api_key"] == "new"
        assert creds["other_field"] == "value"

    def test_rejects_invalid_service_name(self, test_client: TestClient) -> None:
        """Test that invalid service names are rejected by the API."""
        response = test_client.get("/api/credentials/bad!service/status")
        assert response.status_code == 400
        assert "Service name can only include" in response.json()["detail"]


def test_pending_oauth_state_binds_agent_name_and_user() -> None:
    """Pending OAuth state should resolve only for the issuing user and target."""
    app = FastAPI()

    @app.post("/issue/{service}")
    async def issue(service: str, request: Request, user_id: str, agent_name: str | None = None) -> dict[str, str]:
        request.scope["auth_user"] = {"user_id": user_id}
        return {"state": credentials_api.issue_pending_oauth_state(request, service, agent_name)}

    @app.post("/consume/{service}")
    async def consume(service: str, request: Request, state: str, user_id: str) -> dict[str, str | None]:
        request.scope["auth_user"] = {"user_id": user_id}
        return {"agent_name": credentials_api.consume_pending_oauth_request(request, service, state).agent_name}

    client = TestClient(app)
    issue_response = client.post("/issue/google?user_id=alice&agent_name=general")
    assert issue_response.status_code == 200

    state = issue_response.json()["state"]
    consume_response = client.post(f"/consume/google?user_id=alice&state={state}")
    assert consume_response.status_code == 200
    assert consume_response.json() == {"agent_name": "general"}


def test_pending_oauth_state_rejects_different_user() -> None:
    """Pending OAuth state should stay valid for the issuer after a different user is rejected."""
    app = FastAPI()

    @app.post("/issue/{service}")
    async def issue(service: str, request: Request, user_id: str, agent_name: str | None = None) -> dict[str, str]:
        request.scope["auth_user"] = {"user_id": user_id}
        return {"state": credentials_api.issue_pending_oauth_state(request, service, agent_name)}

    @app.post("/consume/{service}")
    async def consume(service: str, request: Request, state: str, user_id: str) -> dict[str, str | None]:
        request.scope["auth_user"] = {"user_id": user_id}
        return {"agent_name": credentials_api.consume_pending_oauth_request(request, service, state).agent_name}

    client = TestClient(app)
    issue_response = client.post("/issue/google?user_id=alice&agent_name=general")
    state = issue_response.json()["state"]

    consume_response = client.post(f"/consume/google?user_id=bob&state={state}")
    assert consume_response.status_code == 403
    assert "current user" in consume_response.json()["detail"]

    issuer_response = client.post(f"/consume/google?user_id=alice&state={state}")
    assert issuer_response.status_code == 200
    assert issuer_response.json() == {"agent_name": "general"}


def test_pending_oauth_request_preserves_payload() -> None:
    """Pending OAuth state should round-trip service-specific callback payload."""
    app = FastAPI()

    @app.post("/issue/{service}")
    async def issue(service: str, request: Request) -> dict[str, str]:
        request.scope["auth_user"] = {"user_id": "alice"}
        return {
            "state": credentials_api.issue_pending_oauth_state(
                request,
                service,
                "general",
                payload={"instance_url": "https://ha.example.com", "client_id": "client-id"},
            ),
        }

    @app.post("/consume/{service}")
    async def consume(service: str, request: Request, state: str) -> dict[str, str | dict[str, str] | None]:
        request.scope["auth_user"] = {"user_id": "alice"}
        pending = credentials_api.consume_pending_oauth_request(request, service, state)
        return {
            "agent_name": pending.agent_name,
            "payload": pending.payload,
        }

    client = TestClient(app)
    state = client.post("/issue/homeassistant").json()["state"]
    response = client.post(f"/consume/homeassistant?state={state}")

    assert response.status_code == 200
    assert response.json() == {
        "agent_name": "general",
        "payload": {"instance_url": "https://ha.example.com", "client_id": "client-id"},
    }
