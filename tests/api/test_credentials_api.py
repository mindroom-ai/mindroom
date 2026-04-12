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


def _openai_test_connections() -> dict[str, dict[str, str]]:
    return {
        "openai/default": {"provider": "openai", "service": "openai", "auth_kind": "api_key"},
        "openai/embeddings": {"provider": "openai", "service": "openai", "auth_kind": "api_key"},
        "openai/stt": {"provider": "openai", "service": "openai", "auth_kind": "api_key"},
    }


def _config_with_worker_scope(worker_scope: str | None) -> Config:
    config = Config.model_validate(
        {
            "connections": _openai_test_connections(),
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
        """Backend-managed Google token storage stays hidden even for isolating worker scopes."""
        config = _config_with_worker_scope("user")
        _publish_committed_runtime_config(client.app, config)
        response = client.get("/api/credentials/google?agent_name=general")

        assert response.status_code == 403
        assert "not available through the generic credentials API" in response.json()["detail"]

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
        """Backend-managed Google token storage stays hidden before draft scope checks run."""
        config = _config_with_worker_scope(None)
        _publish_committed_runtime_config(client.app, config)
        response = client.get("/api/credentials/google?agent_name=general&execution_scope=user")

        assert response.status_code == 403
        assert "not available through the generic credentials API" in response.json()["detail"]

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

    def test_resolve_request_credentials_target_keeps_one_runtime_for_identity(
        self,
        monkeypatch: pytest.MonkeyPatch,
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
        runtime_lookup = MagicMock(side_effect=[runtime_a, runtime_b])
        monkeypatch.setattr("mindroom.api.main.api_runtime_paths", runtime_lookup)
        _publish_committed_runtime_config(app, config)

        with (
            patch("mindroom.api.credentials.get_runtime_credentials_manager", return_value=base_manager),
        ):
            target = credentials_api.resolve_request_credentials_target(request, agent_name="general")

        assert runtime_lookup.call_count == 1
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
        _publish_committed_runtime_config(client.app, config)
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
        _publish_committed_runtime_config(client.app, config)
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
        assert response.json() == ["email", "github", "openai"]

    def test_list_services_hides_backend_managed_services(
        self,
        client: TestClient,
        mock_credentials_manager: MagicMock,
    ) -> None:
        """Backend-managed OAuth/ADC services should not appear in the generic listing."""
        config = Config.model_validate(
            {
                "connections": {
                    **_openai_test_connections(),
                    "google/oauth": {
                        "provider": "google",
                        "service": "google_oauth_custom",
                        "auth_kind": "oauth_client",
                    },
                    "vertexai_claude/default": {
                        "provider": "vertexai_claude",
                        "service": "google_vertex_adc_custom",
                        "auth_kind": "google_adc",
                    },
                },
                "models": {"default": {"provider": "openai", "id": "gpt-4o-mini"}},
                "agents": {},
            },
        )
        _publish_committed_runtime_config(client.app, config)
        mock_credentials_manager.list_services.return_value = [
            "google_oauth_custom",
            "openai",
            "google_vertex_adc_custom",
        ]

        response = client.get("/api/credentials/list")

        assert response.status_code == 200
        assert response.json() == ["google_vertex_adc_custom", "openai"]

    def test_backend_managed_service_rejects_generic_get_credentials(
        self,
        client: TestClient,
    ) -> None:
        """Backend-managed Google auth services should not expose raw payloads via generic endpoints."""
        config = Config.model_validate(
            {
                "connections": {
                    **_openai_test_connections(),
                    "google/oauth": {
                        "provider": "google",
                        "service": "google_oauth_custom",
                        "auth_kind": "oauth_client",
                    },
                },
                "models": {"default": {"provider": "openai", "id": "gpt-4o-mini"}},
                "agents": {},
            },
        )
        _publish_committed_runtime_config(client.app, config)
        response = client.get("/api/credentials/google_oauth_custom")

        assert response.status_code == 403
        assert "not available through the generic credentials API" in response.json()["detail"]

    def test_google_adc_service_allows_generic_status(
        self,
        client: TestClient,
        mock_credentials_manager: MagicMock,
    ) -> None:
        """Google ADC services should remain editable through the generic credentials API."""
        config = Config.model_validate(
            {
                "connections": {
                    **_openai_test_connections(),
                    "vertexai_claude/default": {
                        "provider": "vertexai_claude",
                        "service": "google_vertex_adc_custom",
                        "auth_kind": "google_adc",
                    },
                },
                "models": {"default": {"provider": "openai", "id": "gpt-4o-mini"}},
                "agents": {},
            },
        )
        _publish_committed_runtime_config(client.app, config)
        mock_credentials_manager.load_credentials.return_value = {
            "application_credentials_path": "/tmp/google-adc.json",
            "_source": "ui",
        }
        response = client.get("/api/credentials/google_vertex_adc_custom/status")

        assert response.status_code == 200
        assert response.json() == {
            "service": "google_vertex_adc_custom",
            "has_credentials": True,
            "key_names": ["application_credentials_path"],
        }

    def test_google_token_bucket_rejects_generic_get_credentials(
        self,
        client: TestClient,
    ) -> None:
        """Stored Google OAuth tokens must stay hidden from the generic credentials API."""
        config = Config.model_validate(
            {
                "connections": _openai_test_connections(),
                "models": {"default": {"provider": "openai", "id": "gpt-4o-mini"}},
                "agents": {},
            },
        )
        _publish_committed_runtime_config(client.app, config)
        get_runtime_credentials_manager(main._app_runtime_paths(client.app)).save_credentials(
            "google",
            {
                "refresh_token": "refresh-token",
                "client_id": "client-id",
                "client_secret": "client-secret",
                "_source": "ui",
            },
        )

        response = client.get("/api/credentials/google")

        assert response.status_code == 403
        assert "not available through the generic credentials API" in response.json()["detail"]

    def test_google_gemini_service_remains_visible_in_generic_listing(
        self,
        client: TestClient,
        mock_credentials_manager: MagicMock,
    ) -> None:
        """The default Gemini API-key bucket must stay visible through the generic credentials API."""
        config = Config.validate_with_runtime(
            {
                "models": {
                    "default": {
                        "provider": "google",
                        "id": "gemini-2.5-flash",
                    },
                },
                "agents": {},
            },
            main._app_runtime_paths(client.app),
            strict_connection_validation=True,
        )
        assert config.connections["google/default"].service == "google_gemini"
        _publish_committed_runtime_config(client.app, config)
        mock_credentials_manager.list_services.return_value = [
            "google",
            "google_gemini",
            "openai",
        ]

        response = client.get("/api/credentials/list")

        assert response.status_code == 200
        assert response.json() == ["google_gemini", "openai"]

    def test_google_gemini_service_allows_generic_api_key_endpoint(
        self,
        client: TestClient,
        mock_credentials_manager: MagicMock,
    ) -> None:
        """The default Gemini API-key bucket must not be treated as backend-managed."""
        config = Config.validate_with_runtime(
            {
                "models": {
                    "default": {
                        "provider": "gemini",
                        "id": "gemini-2.5-flash",
                    },
                },
                "agents": {},
            },
            main._app_runtime_paths(client.app),
            strict_connection_validation=True,
        )
        assert config.connections["google/default"].service == "google_gemini"
        _publish_committed_runtime_config(client.app, config)
        mock_credentials_manager.load_credentials.return_value = {
            "api_key": "test-google-gemini-key",
            "_source": "ui",
        }

        response = client.get("/api/credentials/google_gemini/api-key")

        assert response.status_code == 200
        data = response.json()
        assert data["service"] == "google_gemini"
        assert data["has_key"] is True

    def test_implicit_default_vertex_connection_keeps_adc_service_visible(
        self,
        client: TestClient,
        mock_credentials_manager: MagicMock,
    ) -> None:
        """Synthesized Vertex defaults should not hide ADC services from the generic editor."""
        config = Config.validate_with_runtime(
            {
                "models": {
                    "default": {
                        "provider": "vertexai_claude",
                        "id": "claude-sonnet-4-6",
                    },
                },
                "agents": {},
            },
            main._app_runtime_paths(client.app),
        )
        assert "vertexai_claude/default" in config.connections
        assert "connections" not in config.authored_model_dump()
        _publish_committed_runtime_config(client.app, config)
        mock_credentials_manager.list_services.return_value = [
            "google_vertex_adc",
            "openai",
        ]
        mock_credentials_manager.load_credentials.return_value = {
            "application_credentials_path": "/tmp/google-adc.json",
            "_source": "ui",
        }

        list_response = client.get("/api/credentials/list")
        get_response = client.get("/api/credentials/google_vertex_adc")
        status_response = client.get("/api/credentials/google_vertex_adc/status")

        assert list_response.status_code == 200
        assert list_response.json() == ["google_vertex_adc", "openai"]
        assert get_response.status_code == 200
        assert get_response.json() == {
            "service": "google_vertex_adc",
            "credentials": {
                "application_credentials_path": "/tmp/google-adc.json",
            },
        }
        assert status_response.status_code == 200
        assert status_response.json() == {
            "service": "google_vertex_adc",
            "has_credentials": True,
            "key_names": ["application_credentials_path"],
        }

    def test_openai_only_config_hides_stale_google_env_seeded_credentials(
        self,
        client: TestClient,
    ) -> None:
        """Only the dedicated Google OAuth client service should stay hidden without config references."""
        config = Config.model_validate(
            {
                "connections": _openai_test_connections(),
                "models": {"default": {"provider": "openai", "id": "gpt-4o-mini"}},
                "agents": {},
            },
        )
        _publish_committed_runtime_config(client.app, config)
        credentials_manager = get_runtime_credentials_manager(main._app_runtime_paths(client.app))
        adc_path = str(main._app_runtime_paths(client.app).storage_root / "google-adc.json")
        credentials_manager.save_credentials(
            "google_vertex_adc",
            {
                "application_credentials_path": adc_path,
                "_source": "env",
            },
        )
        credentials_manager.save_credentials(
            "google_oauth_client",
            {
                "client_id": "client-id",
                "client_secret": "client-secret",
                "_source": "env",
            },
        )

        list_response = client.get("/api/credentials/list")
        vertex_response = client.get("/api/credentials/google_vertex_adc")
        oauth_response = client.get("/api/credentials/google_oauth_client")

        assert list_response.status_code == 200
        assert "google_vertex_adc" in list_response.json()
        assert "google_oauth_client" not in list_response.json()
        assert vertex_response.status_code == 200
        assert oauth_response.status_code == 403
        assert "not available through the generic credentials API" in oauth_response.json()["detail"]

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
