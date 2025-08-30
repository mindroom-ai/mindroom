"""Tests for the credentials API endpoints."""

from collections.abc import Generator
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

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
        client.mock_manager = mock_credentials_manager  # type: ignore[attr-defined]
        yield client


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
