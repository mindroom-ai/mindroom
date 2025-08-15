"""Tests for the credentials API endpoints."""

from collections.abc import Generator
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from mindroom.api.main import app


@pytest.fixture
def client() -> TestClient:
    """Create a test client for the API."""
    return TestClient(app)


@pytest.fixture
def mock_credentials_manager() -> Generator[MagicMock, None, None]:
    """Mock the credentials manager."""
    with patch("mindroom.api.credentials.get_credentials_manager") as mock:
        mock_manager = MagicMock()
        mock.return_value = mock_manager
        yield mock_manager


class TestCredentialsAPI:
    """Test the credentials API endpoints."""

    def test_set_credentials_endpoint(
        self,
        client: TestClient,
        mock_credentials_manager: MagicMock,
    ) -> None:
        """Test setting multiple credentials for a service."""
        response = client.post(
            "/api/credentials/email",
            json={
                "credentials": {
                    "SMTP_HOST": "smtp.example.com",
                    "SMTP_PORT": "587",
                    "SMTP_USERNAME": "user@example.com",
                    "SMTP_PASSWORD": "secret",
                },
            },
        )

        assert response.status_code == 200
        assert response.json() == {
            "status": "success",
            "message": "Credentials saved for email",
        }

        # Verify the manager was called correctly
        mock_credentials_manager.save_credentials.assert_called_once_with(
            "email",
            {
                "SMTP_HOST": "smtp.example.com",
                "SMTP_PORT": "587",
                "SMTP_USERNAME": "user@example.com",
                "SMTP_PASSWORD": "secret",
            },
        )

    def test_set_api_key_endpoint(
        self,
        client: TestClient,
        mock_credentials_manager: MagicMock,
    ) -> None:
        """Test setting a single API key."""
        response = client.post(
            "/api/credentials/openai/api-key",
            json={
                "service": "openai",
                "api_key": "sk-test123",
                "key_name": "api_key",
            },
        )

        assert response.status_code == 200
        assert response.json() == {
            "status": "success",
            "message": "API key set for openai",
        }

        mock_credentials_manager.set_api_key.assert_called_once_with(
            "openai",
            "sk-test123",
            "api_key",
        )

    def test_get_credentials(
        self,
        client: TestClient,
        mock_credentials_manager: MagicMock,
    ) -> None:
        """Test getting credentials for a service (for editing)."""
        mock_credentials_manager.load_credentials.return_value = {
            "TELEGRAM_TOKEN": "test-token-123",
        }

        response = client.get("/api/credentials/telegram")

        assert response.status_code == 200
        assert response.json() == {
            "service": "telegram",
            "credentials": {
                "TELEGRAM_TOKEN": "test-token-123",
            },
        }

    def test_get_credentials_empty(
        self,
        client: TestClient,
        mock_credentials_manager: MagicMock,
    ) -> None:
        """Test getting credentials when none exist."""
        mock_credentials_manager.load_credentials.return_value = None

        response = client.get("/api/credentials/telegram")

        assert response.status_code == 200
        assert response.json() == {
            "service": "telegram",
            "credentials": {},
        }

    def test_get_credential_status(
        self,
        client: TestClient,
        mock_credentials_manager: MagicMock,
    ) -> None:
        """Test getting credential status."""
        mock_credentials_manager.load_credentials.return_value = {
            "SMTP_HOST": "smtp.example.com",
            "SMTP_PORT": "587",
        }

        response = client.get("/api/credentials/email/status")

        assert response.status_code == 200
        data = response.json()
        assert data["service"] == "email"
        assert data["has_credentials"] is True
        assert set(data["key_names"]) == {"SMTP_HOST", "SMTP_PORT"}

    def test_delete_credentials(
        self,
        client: TestClient,
        mock_credentials_manager: MagicMock,
    ) -> None:
        """Test deleting credentials."""
        response = client.delete("/api/credentials/email")

        assert response.status_code == 200
        assert response.json() == {
            "status": "success",
            "message": "Credentials deleted for email",
        }

        mock_credentials_manager.delete_credentials.assert_called_once_with("email")

    def test_list_services(
        self,
        client: TestClient,
        mock_credentials_manager: MagicMock,
    ) -> None:
        """Test listing services with credentials."""
        mock_credentials_manager.list_services.return_value = ["email", "openai", "github"]

        response = client.get("/api/credentials/list")

        assert response.status_code == 200
        assert response.json() == ["email", "openai", "github"]
