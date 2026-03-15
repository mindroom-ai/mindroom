"""Tests for the credentials API endpoints."""

from collections.abc import Generator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from mindroom import constants
from mindroom.api.main import app
from mindroom.config.main import Config
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, resolve_worker_key


def _config_with_worker_scope(worker_scope: str | None) -> Config:
    config = Config.model_validate(
        {
            "models": {"default": {"provider": "openai", "id": "gpt-4o-mini"}},
            "agents": {
                "general": {
                    "display_name": "General",
                    "role": "test",
                    "tools": ["calculator"],
                    "instructions": ["hi"],
                    "rooms": ["lobby"],
                },
            },
            "defaults": {"markdown": True},
        },
    )
    config.agents["general"].worker_scope = worker_scope
    return config


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    """Create a test client for the API."""
    app.state.runtime_paths = constants.resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={},
    )
    app.state.auth_state = None
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

        # Verify the manager was called correctly (includes _source: ui)
        mock_credentials_manager.save_credentials.assert_called_once_with(
            "email",
            {
                "SMTP_HOST": "smtp.example.com",
                "SMTP_PORT": "587",
                "SMTP_USERNAME": "user@example.com",
                "SMTP_PASSWORD": "secret",
                "_source": "ui",
            },
        )

    def test_set_api_key_endpoint(
        self,
        client: TestClient,
        mock_credentials_manager: MagicMock,
    ) -> None:
        """Test setting a single API key."""
        mock_credentials_manager.load_credentials.return_value = None

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

        mock_credentials_manager.save_credentials.assert_called_once_with(
            "openai",
            {"api_key": "sk-test123", "_source": "ui"},
        )

    def test_rejects_raw_worker_key_query_param(
        self,
        client: TestClient,
    ) -> None:
        """Dashboard credential API should not accept raw worker_key targeting."""
        response = client.post(
            "/api/credentials/openai/api-key?worker_key=worker-a",
            json={
                "service": "openai",
                "api_key": "sk-test123",
                "key_name": "api_key",
            },
        )

        assert response.status_code == 400
        assert "worker_key" in response.json()["detail"]

    def test_agent_name_rejects_user_scope(
        self,
        client: TestClient,
    ) -> None:
        """Dashboard credential management should reject user-scoped agents."""
        config = _config_with_worker_scope("user")

        with patch("mindroom.api.main.load_runtime_config", return_value=(config, Path("config.yaml"))):
            response = client.get("/api/credentials/openai/api-key?agent_name=general")

        assert response.status_code == 400
        assert "worker_scope=user" in response.json()["detail"]

    def test_agent_name_rejects_user_agent_scope(
        self,
        client: TestClient,
    ) -> None:
        """Dashboard credential management should reject user-agent scoped agents."""
        config = _config_with_worker_scope("user_agent")

        with patch("mindroom.api.main.load_runtime_config", return_value=(config, Path("config.yaml"))):
            response = client.get("/api/credentials/openai/api-key?agent_name=general")

        assert response.status_code == 400
        assert "worker_scope=user_agent" in response.json()["detail"]

    def test_shared_agent_name_uses_customer_id_for_worker_tenant(
        self,
        client: TestClient,
        mock_credentials_manager: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Dashboard worker targeting must use the same tenant identity as runtime routing."""
        config = _config_with_worker_scope("shared")
        worker_manager = MagicMock()
        worker_manager.load_credentials.return_value = {
            "api_key": "sk-worker-scope",
            "_source": "ui",
        }
        mock_credentials_manager.for_worker.return_value = worker_manager
        monkeypatch.setenv("CUSTOMER_ID", "tenant-123")
        monkeypatch.setenv("ACCOUNT_ID", "account-456")
        runtime_paths = client.app.state.runtime_paths
        client.app.state.runtime_paths = constants.resolve_primary_runtime_paths(
            config_path=runtime_paths.config_path,
            storage_path=runtime_paths.storage_root,
            process_env={
                **dict(runtime_paths.process_env),
                "CUSTOMER_ID": "tenant-123",
                "ACCOUNT_ID": "account-456",
            },
        )
        client.app.state.auth_state = None

        expected_worker_key = resolve_worker_key(
            "shared",
            ToolExecutionIdentity(
                channel="matrix",
                agent_name="general",
                requester_id=None,
                room_id=None,
                thread_id=None,
                resolved_thread_id=None,
                session_id=None,
                tenant_id="tenant-123",
                account_id="account-456",
            ),
            agent_name="general",
        )
        assert expected_worker_key is not None

        with patch("mindroom.api.main.load_runtime_config", return_value=(config, Path("config.yaml"))):
            response = client.get("/api/credentials/openai/api-key?agent_name=general")

        assert response.status_code == 200
        mock_credentials_manager.for_worker.assert_called_once_with(expected_worker_key)

    def test_rejects_shared_only_integration_services_for_isolating_scope(
        self,
        client: TestClient,
    ) -> None:
        """Dashboard credential management should fail early for unsupported worker scopes."""
        config = _config_with_worker_scope("user")

        with patch("mindroom.api.main.load_runtime_config", return_value=(config, Path("config.yaml"))):
            response = client.get("/api/credentials/google?agent_name=general")

        assert response.status_code == 400
        assert "worker_scope=user" in response.json()["detail"]

    def test_list_services_rejects_unsupported_isolating_scope(
        self,
        client: TestClient,
    ) -> None:
        """Dashboard service listing should reject unsupported worker scopes."""
        config = _config_with_worker_scope("user")

        with patch("mindroom.api.main.load_runtime_config", return_value=(config, Path("config.yaml"))):
            response = client.get("/api/credentials/list?agent_name=general")

        assert response.status_code == 400
        assert "worker_scope=user" in response.json()["detail"]

    def test_shared_agent_name_does_not_merge_global_ui_credentials(
        self,
        client: TestClient,
        mock_credentials_manager: MagicMock,
    ) -> None:
        """Shared worker scope should not inherit UI-saved global credentials."""
        config = _config_with_worker_scope("shared")
        worker_manager = MagicMock()
        worker_manager.load_credentials.return_value = None
        mock_credentials_manager.for_worker.return_value = worker_manager
        mock_credentials_manager.load_credentials.return_value = {
            "api_key": "sk-global-ui",
            "_source": "ui",
        }

        with patch("mindroom.api.main.load_runtime_config", return_value=(config, Path("config.yaml"))):
            response = client.get("/api/credentials/openai/api-key?agent_name=general")

        assert response.status_code == 200
        assert response.json()["has_key"] is False

    def test_shared_agent_name_still_merges_env_credentials(
        self,
        client: TestClient,
        mock_credentials_manager: MagicMock,
    ) -> None:
        """Shared worker scope should still see env-backed base credentials."""
        config = _config_with_worker_scope("shared")
        worker_manager = MagicMock()
        worker_manager.load_credentials.return_value = None
        mock_credentials_manager.for_worker.return_value = worker_manager
        mock_credentials_manager.load_credentials.return_value = {
            "api_key": "sk-global-env",
            "_source": "env",
        }

        with patch("mindroom.api.main.load_runtime_config", return_value=(config, Path("config.yaml"))):
            response = client.get("/api/credentials/openai/api-key?agent_name=general")

        assert response.status_code == 200
        assert response.json()["has_key"] is True
        assert response.json()["source"] == "env"

    def test_rejects_raw_source_worker_key_query_param(
        self,
        client: TestClient,
    ) -> None:
        """Credential copy API should not accept raw source_worker_key targeting."""
        response = client.post("/api/credentials/model:new/copy-from/model:old?source_worker_key=worker-a")

        assert response.status_code == 400
        assert "source_worker_key" in response.json()["detail"]

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

    def test_get_api_key_returns_source_env(
        self,
        client: TestClient,
        mock_credentials_manager: MagicMock,
    ) -> None:
        """Test that get_api_key returns source field for env-sourced keys."""
        mock_credentials_manager.load_credentials.return_value = {
            "api_key": "sk-test-long-key-value",
            "_source": "env",
        }

        response = client.get("/api/credentials/openai/api-key")

        assert response.status_code == 200
        data = response.json()
        assert data["has_key"] is True
        assert data["source"] == "env"
        assert data["masked_key"] == "sk-t...alue"

    def test_get_api_key_returns_source_ui(
        self,
        client: TestClient,
        mock_credentials_manager: MagicMock,
    ) -> None:
        """Test that get_api_key returns source field for UI-sourced keys."""
        mock_credentials_manager.load_credentials.return_value = {
            "api_key": "sk-test-long-key-value",
            "_source": "ui",
        }

        response = client.get("/api/credentials/openai/api-key")

        assert response.status_code == 200
        data = response.json()
        assert data["has_key"] is True
        assert data["source"] == "ui"

    def test_get_api_key_include_value(
        self,
        client: TestClient,
        mock_credentials_manager: MagicMock,
    ) -> None:
        """Test that get_api_key can return the full value when explicitly requested."""
        mock_credentials_manager.load_credentials.return_value = {
            "api_key": "sk-real-value",
            "_source": "ui",
        }

        response = client.get("/api/credentials/openai/api-key?include_value=true")

        assert response.status_code == 200
        data = response.json()
        assert data["has_key"] is True
        assert data["api_key"] == "sk-real-value"
        assert data["source"] == "ui"

    def test_get_api_key_returns_source_none_for_legacy(
        self,
        client: TestClient,
        mock_credentials_manager: MagicMock,
    ) -> None:
        """Test that get_api_key returns null source for legacy credentials."""
        mock_credentials_manager.load_credentials.return_value = {
            "api_key": "sk-test-long-key-value",
        }

        response = client.get("/api/credentials/openai/api-key")

        assert response.status_code == 200
        data = response.json()
        assert data["has_key"] is True
        assert data["source"] is None

    def test_get_credentials_filters_internal_keys(
        self,
        client: TestClient,
        mock_credentials_manager: MagicMock,
    ) -> None:
        """Test that get_credentials filters out _source and other internal keys."""
        mock_credentials_manager.load_credentials.return_value = {
            "api_key": "sk-test123",
            "_source": "ui",
        }

        response = client.get("/api/credentials/openai")

        assert response.status_code == 200
        data = response.json()
        assert data["credentials"] == {"api_key": "sk-test123"}
        assert "_source" not in data["credentials"]

    def test_get_credential_status_filters_internal_keys(
        self,
        client: TestClient,
        mock_credentials_manager: MagicMock,
    ) -> None:
        """Test that credential status key_names excludes internal keys."""
        mock_credentials_manager.load_credentials.return_value = {
            "api_key": "sk-test123",
            "_source": "env",
        }

        response = client.get("/api/credentials/openai/status")

        assert response.status_code == 200
        data = response.json()
        assert data["has_credentials"] is True
        assert data["key_names"] == ["api_key"]

    def test_set_api_key_merges_with_existing(
        self,
        client: TestClient,
        mock_credentials_manager: MagicMock,
    ) -> None:
        """Test that set_api_key merges into existing credentials and flips source to ui."""
        mock_credentials_manager.load_credentials.return_value = {
            "api_key": "old-key",
            "_source": "env",
        }

        response = client.post(
            "/api/credentials/openai/api-key",
            json={
                "service": "openai",
                "api_key": "new-key-from-ui",
                "key_name": "api_key",
            },
        )

        assert response.status_code == 200
        mock_credentials_manager.save_credentials.assert_called_once_with(
            "openai",
            {"api_key": "new-key-from-ui", "_source": "ui"},
        )

    def test_rejects_invalid_service_name(
        self,
        client: TestClient,
        mock_credentials_manager: MagicMock,
    ) -> None:
        """Test that invalid service names are rejected server-side."""
        response = client.get("/api/credentials/bad!service/status")

        assert response.status_code == 400
        assert "Service name can only include" in response.json()["detail"]
        mock_credentials_manager.load_credentials.assert_not_called()
