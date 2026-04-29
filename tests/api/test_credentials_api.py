"""Tests for the credentials API endpoints."""

from collections.abc import Generator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from mindroom import constants
from mindroom.api import credentials as credentials_api
from mindroom.api import main
from mindroom.api.main import app, initialize_api_app
from mindroom.config.main import Config
from mindroom.credentials import get_runtime_credentials_manager
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, resolve_worker_key


def _config_with_worker_scope(
    worker_scope: str | None,
    *,
    worker_grantable_credentials: list[str] | None = None,
) -> Config:
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
            "defaults": {
                "markdown": True,
                "worker_grantable_credentials": worker_grantable_credentials,
            },
        },
    )
    config.agents["general"].worker_scope = worker_scope
    return config


def _publish_committed_runtime_config(api_app: object, config: Config) -> None:
    """Publish one committed config snapshot for dashboard credential tests."""
    context = main._app_context(api_app)
    context.config_data = config.authored_model_dump()
    context.runtime_config = config
    context.config_load_result = main.ConfigLoadResult(success=True)


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    """Create a test client for the API."""
    initialize_api_app(
        app,
        constants.resolve_primary_runtime_paths(
            config_path=tmp_path / "config.yaml",
            storage_path=tmp_path / "mindroom_data",
            process_env={},
        ),
    )
    return TestClient(app)


@pytest.fixture
def mock_credentials_manager() -> Generator[MagicMock, None, None]:
    """Mock the credentials manager."""
    with patch("mindroom.api.credentials.get_runtime_credentials_manager") as mock:
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
        _publish_committed_runtime_config(client.app, config)
        response = client.get("/api/credentials/openai/api-key?agent_name=general")

        assert response.status_code == 400
        assert "worker_scope=user" in response.json()["detail"]

    def test_agent_name_rejects_user_agent_scope(
        self,
        client: TestClient,
    ) -> None:
        """Dashboard credential management should reject user-agent scoped agents."""
        config = _config_with_worker_scope("user_agent")
        _publish_committed_runtime_config(client.app, config)
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
        runtime_paths = main._app_runtime_paths(client.app)
        main.initialize_api_app(
            client.app,
            constants.resolve_primary_runtime_paths(
                config_path=runtime_paths.config_path,
                storage_path=runtime_paths.storage_root,
                process_env={
                    **dict(runtime_paths.process_env),
                    "CUSTOMER_ID": "tenant-123",
                    "ACCOUNT_ID": "account-456",
                },
            ),
        )
        main._app_context(client.app).auth_state = None

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
        _publish_committed_runtime_config(client.app, config)
        response = client.get("/api/credentials/openai/api-key?agent_name=general")

        assert response.status_code == 200
        mock_credentials_manager.for_worker.assert_called_once_with(expected_worker_key)

    def test_rejects_shared_only_integration_services_for_isolating_scope(
        self,
        client: TestClient,
    ) -> None:
        """Dashboard credential management should fail early for unsupported worker scopes."""
        config = _config_with_worker_scope("user")
        _publish_committed_runtime_config(client.app, config)
        response = client.get("/api/credentials/google?agent_name=general")

        assert response.status_code == 400
        assert "worker_scope=user" in response.json()["detail"]

    def test_list_services_rejects_unsupported_isolating_scope(
        self,
        client: TestClient,
    ) -> None:
        """Dashboard service listing should reject unsupported worker scopes."""
        config = _config_with_worker_scope("user")
        _publish_committed_runtime_config(client.app, config)
        response = client.get("/api/credentials/list?agent_name=general")

        assert response.status_code == 400
        assert "worker_scope=user" in response.json()["detail"]

    def test_execution_scope_override_rejects_draft_isolating_scope(
        self,
        client: TestClient,
    ) -> None:
        """Credential management must reject draft-only execution-scope overrides."""
        config = _config_with_worker_scope(None)
        _publish_committed_runtime_config(client.app, config)
        response = client.get("/api/credentials/google?agent_name=general&execution_scope=user")

        assert response.status_code == 409
        assert "Save the configuration before managing credentials" in response.json()["detail"]
        assert "execution_scope=user" in response.json()["detail"]

    def test_execution_scope_override_rejects_draft_unscoped_scope(
        self,
        client: TestClient,
    ) -> None:
        """Credential management must reject draft unscoped overrides too."""
        config = _config_with_worker_scope("shared")
        _publish_committed_runtime_config(client.app, config)
        response = client.get(
            "/api/credentials/openai/api-key?agent_name=general&execution_scope=unscoped",
        )

        assert response.status_code == 409
        assert "execution_scope=unscoped" in response.json()["detail"]
        assert "Persisted scope is worker_scope=shared" in response.json()["detail"]

    def test_oauth_tool_settings_do_not_touch_global_token_service(
        self,
        client: TestClient,
    ) -> None:
        """Saving OAuth-backed tool options should not write or overwrite OAuth tokens."""
        runtime_paths = main._app_runtime_paths(client.app)
        manager = get_runtime_credentials_manager(runtime_paths)
        expected_access_value = "drive-access-value"
        expected_refresh_value = "drive-refresh-value"
        manager.save_credentials(
            "google_drive_oauth",
            {
                "token": expected_access_value,
                "refresh_token": expected_refresh_value,
                "_oauth_provider": "google_drive",
                "_source": "oauth",
            },
        )

        response = client.post(
            "/api/credentials/google_drive",
            json={
                "credentials": {
                    "token": "posted-token",
                    "list_files": False,
                    "max_read_size": 42,
                },
            },
        )

        assert response.status_code == 200
        saved_tokens = manager.load_credentials("google_drive_oauth")
        saved_settings = manager.load_credentials("google_drive")
        assert saved_tokens["token"] == expected_access_value
        assert saved_tokens["refresh_token"] == expected_refresh_value
        assert saved_tokens["_oauth_provider"] == "google_drive"
        assert saved_tokens["_source"] == "oauth"
        assert saved_settings == {
            "list_files": False,
            "max_read_size": 42,
            "_source": "ui",
        }

    def test_get_oauth_credentials_filters_token_fields(
        self,
        client: TestClient,
    ) -> None:
        """OAuth-backed config reads should expose editable tool settings only."""
        runtime_paths = main._app_runtime_paths(client.app)
        manager = get_runtime_credentials_manager(runtime_paths)
        manager.save_credentials(
            "google_drive_oauth",
            {
                "token": "drive-access-value",
                "refresh_token": "drive-refresh-value",
                "client_id": "client-id",
                "token_uri": "https://oauth2.googleapis.com/token",
                "scopes": ["https://www.googleapis.com/auth/drive.metadata.readonly"],
                "expires_at": 1234.0,
                "_oauth_provider": "google_drive",
                "_source": "oauth",
            },
        )
        manager.save_credentials(
            "google_drive",
            {
                "list_files": False,
                "max_read_size": 42,
                "_source": "ui",
            },
        )

        response = client.get("/api/credentials/google_drive")
        status_response = client.get("/api/credentials/google_drive/status")
        token_response = client.get("/api/credentials/google_drive_oauth")
        token_status_response = client.get("/api/credentials/google_drive_oauth/status")

        assert response.status_code == 200
        assert response.json() == {
            "service": "google_drive",
            "credentials": {
                "list_files": False,
                "max_read_size": 42,
            },
        }
        assert status_response.status_code == 200
        assert status_response.json()["has_credentials"] is True
        assert set(status_response.json()["key_names"]) == {"list_files", "max_read_size"}
        assert token_response.status_code == 200
        assert token_response.json() == {"service": "google_drive_oauth", "credentials": {}}
        assert token_status_response.status_code == 200
        assert token_status_response.json()["has_credentials"] is True
        assert token_status_response.json()["key_names"] is None

    def test_oauth_token_service_rejects_generic_credential_writes(
        self,
        client: TestClient,
    ) -> None:
        """OAuth token services should only be written by the OAuth callback path."""
        response = client.post(
            "/api/credentials/google_drive_oauth",
            json={"credentials": {"token": "posted-token"}},
        )

        assert response.status_code == 400
        assert "OAuth token credentials" in response.json()["detail"]

    def test_oauth_tool_settings_do_not_touch_private_token_service(
        self,
        client: TestClient,
    ) -> None:
        """OAuth-backed tool options may save in private scopes without replacing tokens."""
        config = _config_with_worker_scope("user_agent")
        _publish_committed_runtime_config(client.app, config)
        runtime_paths = main._app_runtime_paths(client.app)
        manager = get_runtime_credentials_manager(runtime_paths)
        identity = ToolExecutionIdentity(
            channel="matrix",
            agent_name="general",
            requester_id="standalone",
            room_id=None,
            thread_id=None,
            resolved_thread_id=None,
            session_id=None,
        )
        worker_key = resolve_worker_key("user_agent", identity, agent_name="general")
        assert worker_key is not None
        scoped_manager = manager.for_worker(worker_key)
        expected_access_value = "scoped-drive-access-value"
        expected_refresh_value = "scoped-drive-refresh-value"
        scoped_manager.save_credentials(
            "google_drive_oauth",
            {
                "token": expected_access_value,
                "refresh_token": expected_refresh_value,
                "_oauth_provider": "google_drive",
                "_source": "oauth",
            },
        )

        response = client.post(
            "/api/credentials/google_drive?agent_name=general",
            json={"credentials": {"list_files": False, "max_read_size": 42}},
        )

        assert response.status_code == 200
        saved_tokens = scoped_manager.load_credentials("google_drive_oauth")
        saved_settings = scoped_manager.load_credentials("google_drive")
        assert saved_tokens["token"] == expected_access_value
        assert saved_tokens["refresh_token"] == expected_refresh_value
        assert saved_tokens["_oauth_provider"] == "google_drive"
        assert saved_tokens["_source"] == "oauth"
        assert saved_settings == {
            "list_files": False,
            "max_read_size": 42,
            "_source": "ui",
        }

    def test_get_private_oauth_credentials_filters_token_fields(
        self,
        client: TestClient,
    ) -> None:
        """Private-scope OAuth config reads should not return stored OAuth tokens."""
        config = _config_with_worker_scope("user_agent")
        _publish_committed_runtime_config(client.app, config)
        runtime_paths = main._app_runtime_paths(client.app)
        manager = get_runtime_credentials_manager(runtime_paths)
        identity = ToolExecutionIdentity(
            channel="matrix",
            agent_name="general",
            requester_id="standalone",
            room_id=None,
            thread_id=None,
            resolved_thread_id=None,
            session_id=None,
        )
        worker_key = resolve_worker_key("user_agent", identity, agent_name="general")
        assert worker_key is not None
        scoped_manager = manager.for_worker(worker_key)
        scoped_manager.save_credentials(
            "google_drive_oauth",
            {
                "token": "scoped-drive-access-value",
                "refresh_token": "scoped-drive-refresh-value",
                "client_id": "client-id",
                "token_uri": "https://oauth2.googleapis.com/token",
                "scopes": ["https://www.googleapis.com/auth/drive.metadata.readonly"],
                "expires_at": 1234.0,
                "_oauth_provider": "google_drive",
                "_source": "oauth",
            },
        )
        scoped_manager.save_credentials(
            "google_drive",
            {
                "list_files": False,
                "max_read_size": 42,
                "_source": "ui",
            },
        )

        response = client.get("/api/credentials/google_drive?agent_name=general")
        status_response = client.get("/api/credentials/google_drive/status?agent_name=general")
        token_response = client.get("/api/credentials/google_drive_oauth?agent_name=general")

        assert response.status_code == 200
        assert response.json()["credentials"] == {"list_files": False, "max_read_size": 42}
        assert status_response.status_code == 200
        assert status_response.json()["has_credentials"] is True
        assert set(status_response.json()["key_names"]) == {"list_files", "max_read_size"}
        assert token_response.status_code == 200
        assert token_response.json() == {"service": "google_drive_oauth", "credentials": {}}

    def test_non_oauth_tool_settings_still_reject_private_scopes(
        self,
        client: TestClient,
    ) -> None:
        """Private-scope writes stay limited to registered OAuth credential services."""
        config = _config_with_worker_scope("user_agent")
        _publish_committed_runtime_config(client.app, config)

        response = client.post(
            "/api/credentials/weather?agent_name=general",
            json={"credentials": {"api_key": "weather-key"}},
        )

        assert response.status_code == 400
        assert "worker_scope=user_agent" in response.json()["detail"]

    def test_resolve_request_credentials_target_keeps_one_runtime_for_identity(
        self,
        tmp_path: Path,
    ) -> None:
        """Credential targeting should derive worker identity from the same bound runtime it validated."""
        runtime_a = constants.resolve_primary_runtime_paths(
            config_path=tmp_path / "first.yaml",
            storage_path=tmp_path / "first-store",
            process_env={"CUSTOMER_ID": "tenant-a", "ACCOUNT_ID": "account-a"},
        )
        runtime_b = constants.resolve_primary_runtime_paths(
            config_path=tmp_path / "second.yaml",
            storage_path=tmp_path / "second-store",
            process_env={"CUSTOMER_ID": "tenant-b", "ACCOUNT_ID": "account-b"},
        )
        initialize_api_app(app, runtime_a)
        request = Request(
            {
                "type": "http",
                "app": app,
                "headers": [],
                "query_string": b"",
                "auth_user": {"user_id": "dashboard-user"},
            },
        )
        config = _config_with_worker_scope("shared")
        base_manager = MagicMock()
        base_manager.for_worker.return_value = MagicMock()
        _publish_committed_runtime_config(app, config)

        def _swap_runtime_on_manager_lookup(runtime_paths: object) -> MagicMock:
            assert runtime_paths == runtime_a
            initialize_api_app(app, runtime_b)
            _publish_committed_runtime_config(app, config)
            return base_manager

        with (
            patch(
                "mindroom.api.credentials.get_runtime_credentials_manager",
                side_effect=_swap_runtime_on_manager_lookup,
            ),
        ):
            target = credentials_api.resolve_request_credentials_target(request, agent_name="general")

        assert target.runtime_paths == runtime_a
        assert target.execution_identity is not None
        assert target.execution_identity.tenant_id == "tenant-a"
        assert target.execution_identity.account_id == "account-a"

    def test_credentials_routes_use_committed_snapshot_until_reload(
        self,
        client: TestClient,
    ) -> None:
        """Credential routes should ignore newer on-disk edits until a snapshot reload is published."""
        config = _config_with_worker_scope("shared")
        _publish_committed_runtime_config(client.app, config)
        runtime_paths = main._app_runtime_paths(client.app)
        runtime_paths.config_path.write_text(
            ("models:\n  default:\n    provider: openai\n    id: gpt-4o-mini\nrouter:\n  model: default\nagents: {}\n"),
            encoding="utf-8",
        )

        response = client.get("/api/credentials/openai/api-key?agent_name=general")

        assert response.status_code == 200

    def test_unknown_agent_rejected_for_dashboard_credentials(self, client: TestClient) -> None:
        """Dashboard credentials must reject unknown agents instead of falling back to shared state."""
        config = _config_with_worker_scope("shared")
        _publish_committed_runtime_config(client.app, config)
        response = client.get("/api/credentials/openai/api-key?agent_name=missing")

        assert response.status_code == 404
        assert response.json()["detail"] == "Unknown agent: missing"

    def test_shared_agent_name_hides_non_allowlisted_shared_credentials(
        self,
        client: TestClient,
    ) -> None:
        """Shared worker scope should not expose shared credentials outside the worker allowlist."""
        config = _config_with_worker_scope("shared")
        runtime_paths = main._app_runtime_paths(client.app)
        manager = get_runtime_credentials_manager(runtime_paths)
        manager.save_credentials("openai", {"api_key": "sk-global-ui", "_source": "ui"})

        _publish_committed_runtime_config(client.app, config)
        response = client.get("/api/credentials/openai/api-key?agent_name=general")

        assert response.status_code == 200
        assert response.json()["has_key"] is False

    def test_shared_agent_name_merges_allowlisted_shared_credentials(
        self,
        client: TestClient,
    ) -> None:
        """Shared worker scope should inherit allowlisted shared credentials regardless of source."""
        config = _config_with_worker_scope("shared", worker_grantable_credentials=["openai"])
        runtime_paths = main._app_runtime_paths(client.app)
        manager = get_runtime_credentials_manager(runtime_paths)
        manager.save_credentials("openai", {"api_key": "sk-global-ui", "_source": "ui"})

        _publish_committed_runtime_config(client.app, config)
        response = client.get("/api/credentials/openai/api-key?agent_name=general")

        assert response.status_code == 200
        assert response.json()["has_key"] is True
        assert response.json()["source"] == "ui"

    def test_shared_agent_name_local_shared_credentials_bypass_worker_allowlist(
        self,
        client: TestClient,
    ) -> None:
        """Shared-scope local integrations should stay visible without worker mirroring allowlists."""
        config = _config_with_worker_scope("shared")
        runtime_paths = main._app_runtime_paths(client.app)
        manager = get_runtime_credentials_manager(runtime_paths)
        manager.save_credentials(
            "homeassistant",
            {
                "instance_url": "http://homeassistant.local:8123",
                "access_token": "ha-token",
                "_source": "ui",
            },
        )
        manager.save_credentials(
            "google",
            {
                "token": "token-value",
                "scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
                "_source": "ui",
            },
        )

        _publish_committed_runtime_config(client.app, config)

        list_response = client.get("/api/credentials/list?agent_name=general")
        ha_status_response = client.get("/api/credentials/homeassistant/status?agent_name=general")
        google_status_response = client.get("/api/credentials/google/status?agent_name=general")

        assert list_response.status_code == 200
        assert "homeassistant" in list_response.json()
        assert "google" in list_response.json()

        assert ha_status_response.status_code == 200
        assert ha_status_response.json()["has_credentials"] is True
        assert set(ha_status_response.json()["key_names"]) == {"instance_url", "access_token"}

        assert google_status_response.status_code == 200
        assert google_status_response.json()["has_credentials"] is True
        assert set(google_status_response.json()["key_names"]) == {"token", "scopes"}

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
