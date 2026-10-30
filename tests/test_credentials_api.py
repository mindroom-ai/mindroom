"""Tests for the credentials API endpoints."""

from collections.abc import Generator
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mindroom.api.main import initialize_api_app
from mindroom.config.main import Config
from mindroom.config.models import RouterConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.credentials.manager import CredentialsManager
from mindroom.embeddings.embedder_health import capture_embedder_health_recorder, get_embedder_failure
from tests.api.conftest import publish_api_config


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
    initialize_api_app(
        app,
        resolve_runtime_paths(storage_path=mock_credentials_manager.storage_root),
    )

    with patch("mindroom.api.credentials_target.get_runtime_credentials_manager") as mock_get:
        mock_get.return_value = mock_credentials_manager
        yield TestClient(app)


def _embedder_config(*, credentials_service: str | None = None) -> Config:
    """Return a config whose embedder reads its key from the credential store."""
    embedder_config: dict[str, str] = {"model": "text-embedding-3-small"}
    if credentials_service is not None:
        embedder_config["credentials_service"] = credentials_service
    return Config(
        memory={"backend": "mem0", "embedder": {"provider": "openai", "config": embedder_config}},
        router=RouterConfig(model="default"),
    )


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

    def test_set_embedder_api_key_invalidates_inflight_health_writers(self, test_client: TestClient) -> None:
        """A dashboard key rotation starts a new health generation."""
        old_recorder = capture_embedder_health_recorder()
        old_recorder.record("embedder authentication failed (HTTP 401)")

        publish_api_config(test_client.app, _embedder_config())
        with (
            patch(
                "mindroom.embeddings.embedder_health.create_background_task",
                side_effect=lambda coroutine, **_kwargs: coroutine.close(),
            ),
        ):
            response = test_client.post(
                "/api/credentials/embedder/api-key",
                json={"service": "embedder", "api_key": "new-key", "key_name": "api_key"},
            )

        assert response.status_code == 200
        assert get_embedder_failure() is None
        assert not old_recorder.record("embedder authentication failed (HTTP 401)")

    def test_unchanged_effective_embedder_key_keeps_health_generation(
        self,
        test_client: TestClient,
        mock_credentials_manager: CredentialsManager,
    ) -> None:
        """Saving the same key never invalidates recorders bound to cached embedders."""
        mock_credentials_manager.save_credentials("embedder", {"api_key": "same-key"})
        recorder = capture_embedder_health_recorder()
        recorder.record("embedder authentication failed (HTTP 401)")

        publish_api_config(test_client.app, _embedder_config())
        response = test_client.post(
            "/api/credentials/embedder/api-key",
            json={"service": "embedder", "api_key": "same-key", "key_name": "api_key"},
        )

        assert response.status_code == 200
        assert get_embedder_failure() == "embedder authentication failed (HTTP 401)"
        assert recorder.record(None)

    def test_dedicated_embedder_key_shields_health_from_openai_rotation(
        self,
        test_client: TestClient,
        mock_credentials_manager: CredentialsManager,
    ) -> None:
        """Changing a shadowed fallback key leaves embedder health untouched."""
        mock_credentials_manager.save_credentials("embedder", {"api_key": "dedicated-key"})
        mock_credentials_manager.save_credentials("openai", {"api_key": "old-openai-key"})
        recorder = capture_embedder_health_recorder()
        recorder.record("embedder authentication failed (HTTP 401)")

        publish_api_config(test_client.app, _embedder_config())
        response = test_client.post(
            "/api/credentials/openai/api-key",
            json={"service": "openai", "api_key": "new-openai-key", "key_name": "api_key"},
        )

        assert response.status_code == 200
        assert get_embedder_failure() == "embedder authentication failed (HTTP 401)"
        assert recorder.record(None)

    def test_named_embedder_service_controls_rotation_invalidation(
        self,
        test_client: TestClient,
        mock_credentials_manager: CredentialsManager,
    ) -> None:
        """Invalidation follows the configured binding without hardcoded service names."""
        mock_credentials_manager.save_credentials("embedding-production", {"api_key": "old-key"})
        recorder = capture_embedder_health_recorder()
        recorder.record("embedder authentication failed (HTTP 401)")
        config = _embedder_config(credentials_service="embedding-production")

        publish_api_config(test_client.app, config)
        with (
            patch(
                "mindroom.embeddings.embedder_health.create_background_task",
                side_effect=lambda coroutine, **_kwargs: coroutine.close(),
            ),
        ):
            unrelated_response = test_client.post(
                "/api/credentials/openai/api-key",
                json={"service": "openai", "api_key": "unrelated-key", "key_name": "api_key"},
            )
            assert unrelated_response.status_code == 200
            assert recorder.record("embedder authentication failed (HTTP 401)")

            bound_response = test_client.post(
                "/api/credentials/embedding-production/api-key",
                json={"service": "embedding-production", "api_key": "new-key", "key_name": "api_key"},
            )

        assert bound_response.status_code == 200
        assert get_embedder_failure() is None
        assert not recorder.record("embedder authentication failed (HTTP 401)")

    def test_effective_embedder_key_change_schedules_immediate_probe(self, test_client: TestClient) -> None:
        """Health is revalidated immediately after a real key rotation."""
        config = Config(memory={"backend": "mem0"}, router=RouterConfig(model="default"))

        publish_api_config(test_client.app, config)
        with (
            patch("mindroom.embeddings.embedder_health.check_embedder_health", return_value=object()) as check_health,
            patch(
                "mindroom.embeddings.embedder_health.create_background_task",
                side_effect=lambda coroutine, **_kwargs: coroutine.close(),
            ) as create_task,
        ):
            response = test_client.post(
                "/api/credentials/embedder/api-key",
                json={"service": "embedder", "api_key": "rotated-key", "key_name": "api_key"},
            )

        assert response.status_code == 200
        check_health.assert_called_once()
        assert check_health.call_args.kwargs["reason"] == "credential_change"
        create_task.assert_called_once()

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
        mock_credentials_manager.save_credentials("openai", {"api_key": "sk-test-key-123456789"})

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
        mock_credentials_manager.save_credentials("openai", {"api_key": "short"})

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
        mock_credentials_manager.save_credentials("openai", {"api_key": "old-key"})

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
