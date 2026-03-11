"""Tests for the centralized credentials manager."""

from pathlib import Path
from typing import Any

import pytest

import mindroom.credentials
from mindroom.constants import CREDENTIALS_DIR
from mindroom.credentials import (
    CredentialsManager,
    get_credentials_manager,
    load_scoped_credentials,
    merge_scoped_credentials,
    save_scoped_credentials,
)
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, tool_execution_identity


@pytest.fixture
def temp_credentials_dir(tmp_path: Path) -> Path:
    """Create a temporary directory for testing credentials."""
    creds_dir = tmp_path / "test_credentials"
    creds_dir.mkdir(parents=True, exist_ok=True)
    return creds_dir


@pytest.fixture
def credentials_manager(temp_credentials_dir: Path) -> CredentialsManager:
    """Create a CredentialsManager instance with a temporary directory."""
    return CredentialsManager(base_path=temp_credentials_dir)


class TestCredentialsManager:
    """Test suite for CredentialsManager."""

    def test_initialization_default_path(self) -> None:
        """Test that default path is created correctly."""
        manager = CredentialsManager()
        assert manager.base_path == CREDENTIALS_DIR
        assert manager.base_path.exists()

    def test_initialization_custom_path(self, temp_credentials_dir: Path) -> None:
        """Test initialization with custom path."""
        manager = CredentialsManager(base_path=temp_credentials_dir)
        assert manager.base_path == temp_credentials_dir
        assert manager.base_path.exists()

    def test_get_credentials_path(self, credentials_manager: CredentialsManager) -> None:
        """Test getting the path for a service's credentials."""
        google_path = credentials_manager.get_credentials_path("google")
        assert google_path == credentials_manager.base_path / "google_credentials.json"

        ha_path = credentials_manager.get_credentials_path("homeassistant")
        assert ha_path == credentials_manager.base_path / "homeassistant_credentials.json"

    @pytest.mark.parametrize(
        "service",
        ["", " ", "../etc", "bad/name", "bad name", "bad!name"],
    )
    def test_get_credentials_path_rejects_invalid_service_names(
        self,
        credentials_manager: CredentialsManager,
        service: str,
    ) -> None:
        """Test that invalid service names are rejected."""
        with pytest.raises(ValueError, match="Service name"):
            credentials_manager.get_credentials_path(service)

    def test_save_and_load_credentials(self, credentials_manager: CredentialsManager) -> None:
        """Test saving and loading credentials."""
        test_creds = {
            "token": "test_token_123",
            "refresh_token": "refresh_123",
            "client_id": "client_123",
            "client_secret": "secret_123",
            "scopes": ["scope1", "scope2"],
        }

        # Save credentials
        credentials_manager.save_credentials("test_service", test_creds)

        # Verify file was created
        creds_file = credentials_manager.get_credentials_path("test_service")
        assert creds_file.exists()

        # Load credentials
        loaded_creds = credentials_manager.load_credentials("test_service")
        assert loaded_creds == test_creds

    def test_load_nonexistent_credentials(self, credentials_manager: CredentialsManager) -> None:
        """Test loading credentials that don't exist."""
        result = credentials_manager.load_credentials("nonexistent")
        assert result is None

    def test_load_corrupted_credentials(self, credentials_manager: CredentialsManager) -> None:
        """Test loading corrupted credentials file."""
        # Create a corrupted credentials file
        creds_path = credentials_manager.get_credentials_path("corrupted")
        creds_path.write_text("not valid json{")

        # Should return None on error
        result = credentials_manager.load_credentials("corrupted")
        assert result is None

    def test_delete_credentials(self, credentials_manager: CredentialsManager) -> None:
        """Test deleting credentials."""
        test_creds = {"key": "value"}

        # Save credentials
        credentials_manager.save_credentials("to_delete", test_creds)
        creds_file = credentials_manager.get_credentials_path("to_delete")
        assert creds_file.exists()

        # Delete credentials
        credentials_manager.delete_credentials("to_delete")
        assert not creds_file.exists()

        # Deleting non-existent credentials should not raise error
        credentials_manager.delete_credentials("nonexistent")

    def test_list_services(self, credentials_manager: CredentialsManager) -> None:
        """Test listing all services with stored credentials."""
        # Initially empty
        assert credentials_manager.list_services() == []

        # Add some credentials
        credentials_manager.save_credentials("google", {"token": "google_token"})
        credentials_manager.save_credentials("homeassistant", {"token": "ha_token"})
        credentials_manager.save_credentials("spotify", {"token": "spotify_token"})

        # List should be sorted
        services = credentials_manager.list_services()
        assert services == ["google", "homeassistant", "spotify"]

    def test_update_credentials(self, credentials_manager: CredentialsManager) -> None:
        """Test updating existing credentials."""
        original = {"token": "old_token", "refresh_token": "old_refresh"}
        updated = {"token": "new_token", "refresh_token": "new_refresh", "extra": "data"}

        # Save original
        credentials_manager.save_credentials("update_test", original)
        assert credentials_manager.load_credentials("update_test") == original

        # Update
        credentials_manager.save_credentials("update_test", updated)
        assert credentials_manager.load_credentials("update_test") == updated

    def test_credentials_isolation(self, credentials_manager: CredentialsManager) -> None:
        """Test that credentials for different services are isolated."""
        google_creds = {"service": "google", "token": "google_123"}
        ha_creds = {"service": "homeassistant", "token": "ha_456"}

        credentials_manager.save_credentials("google", google_creds)
        credentials_manager.save_credentials("homeassistant", ha_creds)

        # Each service should have its own credentials
        assert credentials_manager.load_credentials("google") == google_creds
        assert credentials_manager.load_credentials("homeassistant") == ha_creds

        # Deleting one shouldn't affect the other
        credentials_manager.delete_credentials("google")
        assert credentials_manager.load_credentials("google") is None
        assert credentials_manager.load_credentials("homeassistant") == ha_creds

    def test_worker_credentials_are_isolated_from_shared(self, temp_credentials_dir: Path) -> None:
        """Worker-scoped credentials should not overwrite or read from the shared credential directory."""
        manager = CredentialsManager(temp_credentials_dir)
        manager.save_credentials("openai", {"api_key": "shared-key", "_source": "ui"})

        worker_manager = manager.for_worker("worker-a")
        worker_manager.save_credentials("openai", {"api_key": "worker-key", "_source": "ui"})

        assert manager.load_credentials("openai") == {"api_key": "shared-key", "_source": "ui"}
        assert worker_manager.load_credentials("openai") == {"api_key": "worker-key", "_source": "ui"}
        assert worker_manager.get_credentials_path("openai").parent != manager.get_credentials_path("openai").parent

    def test_save_scoped_credentials_writes_to_worker_manager(self, temp_credentials_dir: Path) -> None:
        """Scoped saves should target the worker-owned credentials store."""
        manager = CredentialsManager(temp_credentials_dir)
        execution_identity = ToolExecutionIdentity(
            channel="matrix",
            agent_name="general",
            requester_id="@alice:example.org",
            room_id="!room:example.org",
            thread_id=None,
            resolved_thread_id=None,
            session_id=None,
            tenant_id="tenant-123",
            account_id="account-456",
        )

        with tool_execution_identity(execution_identity):
            save_scoped_credentials(
                "google",
                {"token": "worker-token", "_source": "ui"},
                worker_scope="user",
                routing_agent_name="general",
                credentials_manager=manager,
            )

        shared_credentials = manager.load_credentials("google")
        worker_credentials = manager.for_worker(
            "v1:tenant-123:user:@alice:example.org",
        ).load_credentials("google")

        assert shared_credentials is None
        assert worker_credentials == {"token": "worker-token", "_source": "ui"}

    def test_load_scoped_credentials_shared_scope_does_not_fall_back_to_global_ui(
        self,
        temp_credentials_dir: Path,
    ) -> None:
        """Shared worker scope should not inherit UI-saved global credentials."""
        manager = CredentialsManager(temp_credentials_dir)
        execution_identity = ToolExecutionIdentity(
            channel="matrix",
            agent_name="general",
            requester_id="@alice:example.org",
            room_id="!room:example.org",
            thread_id=None,
            resolved_thread_id=None,
            session_id=None,
            tenant_id="tenant-123",
            account_id="account-456",
        )
        manager.save_credentials("google", {"api_key": "global-ui-key", "_source": "ui"})

        with tool_execution_identity(execution_identity):
            loaded_credentials = load_scoped_credentials(
                "google",
                worker_scope="shared",
                routing_agent_name="general",
                credentials_manager=manager,
            )

        assert loaded_credentials is None

    def test_load_scoped_credentials_shared_scope_keeps_env_fallback(
        self,
        temp_credentials_dir: Path,
    ) -> None:
        """Shared worker scope should still inherit env-backed credentials."""
        manager = CredentialsManager(temp_credentials_dir)
        execution_identity = ToolExecutionIdentity(
            channel="matrix",
            agent_name="general",
            requester_id="@alice:example.org",
            room_id="!room:example.org",
            thread_id=None,
            resolved_thread_id=None,
            session_id=None,
            tenant_id="tenant-123",
            account_id="account-456",
        )
        manager.save_credentials("google", {"api_key": "env-key", "_source": "env"})

        with tool_execution_identity(execution_identity):
            loaded_credentials = load_scoped_credentials(
                "google",
                worker_scope="shared",
                routing_agent_name="general",
                credentials_manager=manager,
            )

        assert loaded_credentials == {"api_key": "env-key", "_source": "env"}

    def test_load_scoped_credentials_uses_worker_rooted_manager_without_nesting(
        self,
        temp_credentials_dir: Path,
    ) -> None:
        """Worker-rooted managers should resolve scoped credentials from the current worker root."""
        base_manager = CredentialsManager(temp_credentials_dir)
        execution_identity = ToolExecutionIdentity(
            channel="matrix",
            agent_name="general",
            requester_id="@alice:example.org",
            room_id="!room:example.org",
            thread_id="$thread",
            resolved_thread_id="$thread",
            session_id="session-1",
            tenant_id="tenant-123",
            account_id="account-456",
        )
        worker_key = "v1:tenant-123:user:@alice:example.org"
        worker_manager = base_manager.for_worker(worker_key)
        worker_manager.save_credentials("openweather", {"api_key": "worker-key", "_source": "ui"})

        loaded_credentials = load_scoped_credentials(
            "openweather",
            worker_scope="user",
            routing_agent_name="general",
            credentials_manager=worker_manager,
            execution_identity=execution_identity,
        )

        assert loaded_credentials == {"api_key": "worker-key", "_source": "ui"}

    def test_credentials_manager_defaults_to_shared_storage_path_when_configured(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Dedicated workers should resolve shared credentials from the shared storage mount."""
        shared_storage_root = tmp_path / "shared-storage"
        monkeypatch.setenv("MINDROOM_SHARED_STORAGE_PATH", str(shared_storage_root))

        manager = CredentialsManager()

        assert manager.base_path == (shared_storage_root / "credentials").resolve()

    def test_shared_storage_root_supports_worker_scoped_credentials_lookup(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Dedicated workers should load shared env-backed credentials plus worker overrides from the shared storage mount."""
        shared_storage_root = tmp_path / "shared-storage"
        monkeypatch.setenv("MINDROOM_SHARED_STORAGE_PATH", str(shared_storage_root))
        manager = CredentialsManager()
        manager.save_credentials("google", {"api_key": "env-key", "_source": "env"})
        execution_identity = ToolExecutionIdentity(
            channel="matrix",
            agent_name="general",
            requester_id="@alice:example.org",
            room_id="!room:example.org",
            thread_id=None,
            resolved_thread_id=None,
            session_id=None,
            tenant_id="tenant-123",
            account_id="account-456",
        )
        worker_manager = manager.for_worker("v1:tenant-123:user:@alice:example.org")
        worker_manager.save_credentials("google", {"api_key": "worker-key", "_source": "ui"})

        with tool_execution_identity(execution_identity):
            loaded = load_scoped_credentials(
                "google",
                worker_scope="user",
                routing_agent_name="general",
                credentials_manager=manager,
            )

        assert loaded == {"api_key": "worker-key", "_source": "ui"}

    def test_merge_scoped_credentials_overlays_worker_credentials(
        self,
        temp_credentials_dir: Path,
    ) -> None:
        """Worker-scoped credentials should overlay env-backed shared credentials."""
        manager = CredentialsManager(temp_credentials_dir)
        manager.save_credentials("google", {"api_key": "env-key", "_source": "env", "shared_only": "yes"})
        worker_manager = manager.for_worker("worker-a")
        worker_manager.save_credentials("google", {"api_key": "worker-key", "_source": "ui"})

        merged = merge_scoped_credentials(
            "google",
            base_manager=manager,
            worker_manager=worker_manager,
        )

        assert merged == {"api_key": "worker-key", "_source": "ui", "shared_only": "yes"}

    def test_complex_credentials_structure(self, credentials_manager: CredentialsManager) -> None:
        """Test saving and loading complex nested credentials."""
        complex_creds: dict[str, Any] = {
            "token": "token_123",
            "nested": {
                "level1": {
                    "level2": ["item1", "item2", "item3"],
                    "data": {"key": "value"},
                },
            },
            "numbers": [1, 2, 3, 4.5],
            "boolean": True,
            "null_value": None,
        }

        credentials_manager.save_credentials("complex", complex_creds)
        loaded = credentials_manager.load_credentials("complex")
        assert loaded == complex_creds

    def test_get_api_key(self, temp_credentials_dir: Path) -> None:
        """Test getting API keys from credentials."""
        manager = CredentialsManager(temp_credentials_dir)

        # Test getting API key from simple structure
        manager.set_api_key("openai", "sk-test123")
        assert manager.get_api_key("openai") == "sk-test123"

        # Test getting non-existent service
        assert manager.get_api_key("nonexistent") is None

        # Test getting custom key name
        manager.save_credentials("custom", {"token": "custom-token"})
        assert manager.get_api_key("custom", "token") == "custom-token"
        assert manager.get_api_key("custom", "api_key") is None

    def test_set_api_key(self, temp_credentials_dir: Path) -> None:
        """Test setting API keys in credentials."""
        manager = CredentialsManager(temp_credentials_dir)

        # Test setting new API key
        manager.set_api_key("anthropic", "claude-key")
        assert manager.get_api_key("anthropic") == "claude-key"

        # Test updating existing API key
        manager.set_api_key("anthropic", "new-claude-key")
        assert manager.get_api_key("anthropic") == "new-claude-key"

        # Test setting custom key name
        manager.set_api_key("service", "value123", "custom_key")
        creds = manager.load_credentials("service")
        assert creds is not None
        assert creds["custom_key"] == "value123"

        # Test that other fields are preserved
        manager.save_credentials("multi", {"field1": "value1", "api_key": "old"})
        manager.set_api_key("multi", "new")
        creds = manager.load_credentials("multi")
        assert creds is not None
        assert creds["api_key"] == "new"
        assert creds["field1"] == "value1"


class TestGlobalCredentialsManager:
    """Test the global credentials manager singleton."""

    @pytest.fixture(autouse=True)
    def reset_global_manager(self) -> None:
        """Reset the global credentials manager before each test."""
        mindroom.credentials._credentials_manager = None

    def test_get_credentials_manager_singleton(self) -> None:
        """Test that get_credentials_manager returns the same instance."""
        manager1 = get_credentials_manager()
        manager2 = get_credentials_manager()
        assert manager1 is manager2

    def test_global_manager_default_path(self) -> None:
        """Test that global manager uses the default path."""
        manager = get_credentials_manager()
        assert manager.base_path == CREDENTIALS_DIR
