"""Tests for the centralized credentials manager."""

from pathlib import Path
from typing import Any

import pytest

import mindroom.constants as constants_mod
import mindroom.credentials
from mindroom.api.credentials import RequestCredentialsTarget
from mindroom.api.google_integration import _build_google_token_data
from mindroom.api.integrations import _save_spotify_credentials
from mindroom.credentials import (
    _DEDICATED_WORKER_KEY_ENV,
    _DEDICATED_WORKER_ROOT_ENV,
    SHARED_CREDENTIALS_PATH_ENV,
    CredentialsManager,
    get_credentials_manager,
    get_runtime_credentials_manager,
    load_scoped_credentials,
    merge_scoped_credentials,
    save_scoped_credentials,
    sync_shared_credentials_to_worker,
)
from mindroom.tool_system.worker_routing import ResolvedWorkerTarget, ToolExecutionIdentity, resolve_worker_target


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


def _worker_target(
    worker_scope: str | None,
    routing_agent_name: str | None,
    execution_identity: ToolExecutionIdentity | None,
    *,
    tenant_id: str | None = None,
    account_id: str | None = None,
) -> ResolvedWorkerTarget:
    return resolve_worker_target(
        worker_scope,
        routing_agent_name,
        execution_identity,
        tenant_id=tenant_id,
        account_id=account_id,
    )


class TestCredentialsManager:
    """Test suite for CredentialsManager."""

    def test_initialization_explicit_runtime_path(self, tmp_path: Path) -> None:
        """Test that credentials managers use an explicitly resolved runtime root."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text("agents: {}\nmodels: {}\nrouter:\n  model: default\n", encoding="utf-8")
        runtime_paths = constants_mod.resolve_runtime_paths(config_path=config_path, storage_path=tmp_path)
        manager = CredentialsManager(constants_mod.credentials_dir(runtime_paths=runtime_paths))
        assert manager.base_path == tmp_path / "credentials"
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

        save_scoped_credentials(
            "google",
            {"token": "worker-token", "_source": "ui"},
            credentials_manager=manager,
            worker_target=_worker_target("user", "general", execution_identity),
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

        loaded_credentials = load_scoped_credentials(
            "google",
            credentials_manager=manager,
            worker_target=_worker_target("shared", "general", execution_identity),
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

        loaded_credentials = load_scoped_credentials(
            "google",
            credentials_manager=manager,
            worker_target=_worker_target("shared", "general", execution_identity),
        )

        assert loaded_credentials == {"api_key": "env-key", "_source": "env"}

    def test_load_scoped_credentials_uses_worker_rooted_manager_without_nesting(
        self,
        temp_credentials_dir: Path,
    ) -> None:
        """Worker-rooted managers should merge their shared mirror with worker-local overrides."""
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
        base_manager.save_credentials("openweather", {"api_key": "env-key", "_source": "env", "base": "yes"})
        sync_shared_credentials_to_worker(
            worker_key,
            include_ui_credentials=False,
            credentials_manager=base_manager,
        )
        worker_manager.save_credentials("openweather", {"api_key": "worker-key", "_source": "ui"})

        loaded_credentials = load_scoped_credentials(
            "openweather",
            credentials_manager=worker_manager,
            worker_target=_worker_target("user", "general", execution_identity),
        )

        assert loaded_credentials == {"api_key": "worker-key", "_source": "ui", "base": "yes"}

    def test_load_scoped_credentials_shared_scope_synthesizes_worker_key_from_tenant_context(
        self,
        temp_credentials_dir: Path,
    ) -> None:
        """Shared worker scope should resolve worker credentials from explicit tenant context."""
        manager = CredentialsManager(temp_credentials_dir)
        worker_key = "v1:tenant-123:shared:general"
        manager.for_worker(worker_key).save_credentials("google", {"api_key": "worker-key", "_source": "ui"})

        loaded_credentials = load_scoped_credentials(
            "google",
            credentials_manager=manager,
            worker_target=_worker_target(
                "shared",
                "general",
                None,
                tenant_id="tenant-123",
                account_id="account-456",
            ),
        )

        assert loaded_credentials == {"api_key": "worker-key", "_source": "ui"}

    def test_load_scoped_credentials_uses_shared_mirror_for_unscoped_worker_manager(
        self,
        temp_credentials_dir: Path,
    ) -> None:
        """Worker-rooted managers should load unscoped credentials from their mirrored shared layer."""
        base_manager = CredentialsManager(temp_credentials_dir)
        worker_key = "v1:tenant-123:unscoped:general"
        worker_manager = base_manager.for_worker(worker_key)
        base_manager.save_credentials("openai", {"api_key": "shared-ui-key", "_source": "ui"})
        sync_shared_credentials_to_worker(
            worker_key,
            include_ui_credentials=True,
            credentials_manager=base_manager,
        )

        loaded_credentials = load_scoped_credentials(
            "openai",
            credentials_manager=worker_manager,
            worker_target=None,
        )

        assert loaded_credentials == {"api_key": "shared-ui-key", "_source": "ui"}

    def test_save_scoped_credentials_unscoped_worker_manager_writes_local_override(
        self,
        temp_credentials_dir: Path,
    ) -> None:
        """Unscoped worker-rooted saves should create a worker-local override instead of mutating the shared mirror."""
        base_manager = CredentialsManager(temp_credentials_dir)
        worker_key = "v1:tenant-123:unscoped:general"
        worker_manager = base_manager.for_worker(worker_key)

        save_scoped_credentials(
            "google",
            {"refresh_token": "worker-refresh", "_source": "ui"},
            credentials_manager=worker_manager,
            worker_target=None,
        )

        assert worker_manager.load_credentials("google") == {
            "refresh_token": "worker-refresh",
            "_source": "ui",
        }
        assert worker_manager.shared_manager().load_credentials("google") is None

    def test_unscoped_worker_rooted_manager_keeps_local_refresh_across_resync(
        self,
        temp_credentials_dir: Path,
    ) -> None:
        """Resyncing the shared mirror should not clobber a worker-local unscoped refresh."""
        base_manager = CredentialsManager(temp_credentials_dir)
        worker_key = "v1:tenant-123:unscoped:general"
        worker_manager = base_manager.for_worker(worker_key)
        base_manager.save_credentials("google", {"client_id": "shared-client", "_source": "ui"})

        sync_shared_credentials_to_worker(
            worker_key,
            include_ui_credentials=True,
            credentials_manager=base_manager,
        )
        save_scoped_credentials(
            "google",
            {"refresh_token": "worker-refresh", "_source": "ui"},
            credentials_manager=worker_manager,
            worker_target=None,
        )

        sync_shared_credentials_to_worker(
            worker_key,
            include_ui_credentials=True,
            credentials_manager=base_manager,
        )

        loaded_credentials = load_scoped_credentials(
            "google",
            credentials_manager=worker_manager,
            worker_target=None,
        )

        assert loaded_credentials == {
            "client_id": "shared-client",
            "refresh_token": "worker-refresh",
            "_source": "ui",
        }

    def test_sync_shared_credentials_to_worker_copies_env_backed_credentials(
        self,
        temp_credentials_dir: Path,
    ) -> None:
        """Dedicated workers should mirror shared env-backed credentials into their shared layer."""
        manager = CredentialsManager(temp_credentials_dir)
        manager.save_credentials("google", {"api_key": "env-key", "_source": "env"})

        sync_shared_credentials_to_worker(
            "worker-a",
            include_ui_credentials=False,
            credentials_manager=manager,
        )

        worker_credentials = manager.for_worker("worker-a").shared_manager().load_credentials("google")
        assert worker_credentials == {"api_key": "env-key", "_source": "env"}

    def test_sync_shared_credentials_to_worker_preserves_worker_local_credentials(
        self,
        temp_credentials_dir: Path,
    ) -> None:
        """Dedicated worker seeding should not overwrite worker-local non-env credentials."""
        manager = CredentialsManager(temp_credentials_dir)
        manager.save_credentials("google", {"api_key": "env-key", "_source": "env"})
        worker_manager = manager.for_worker("worker-a")
        worker_manager.save_credentials("google", {"api_key": "worker-key", "_source": "ui"})

        sync_shared_credentials_to_worker(
            "worker-a",
            include_ui_credentials=False,
            credentials_manager=manager,
        )

        assert worker_manager.load_credentials("google") == {"api_key": "worker-key", "_source": "ui"}
        assert worker_manager.shared_manager().load_credentials("google") == {
            "api_key": "env-key",
            "_source": "env",
        }

    def test_sync_shared_credentials_to_worker_can_copy_ui_credentials_for_unscoped_workers(
        self,
        temp_credentials_dir: Path,
    ) -> None:
        """Unscoped workers should mirror dashboard-saved shared UI credentials."""
        manager = CredentialsManager(temp_credentials_dir)
        manager.save_credentials("openai", {"api_key": "shared-ui-key", "_source": "ui"})

        sync_shared_credentials_to_worker(
            "v1:tenant-123:unscoped:general",
            include_ui_credentials=True,
            credentials_manager=manager,
        )

        shared_worker_credentials = (
            manager.for_worker(
                "v1:tenant-123:unscoped:general",
            )
            .shared_manager()
            .load_credentials("openai")
        )
        assert shared_worker_credentials == {"api_key": "shared-ui-key", "_source": "ui"}

    def test_sync_shared_credentials_to_worker_copies_legacy_shared_credentials_for_unscoped_workers(
        self,
        temp_credentials_dir: Path,
    ) -> None:
        """Unscoped workers should mirror legacy shared credentials that predate _source tagging."""
        manager = CredentialsManager(temp_credentials_dir)
        manager.save_credentials("spotify", {"access_token": "legacy-token"})

        sync_shared_credentials_to_worker(
            "v1:tenant-123:unscoped:general",
            include_ui_credentials=True,
            credentials_manager=manager,
        )

        shared_worker_credentials = (
            manager.for_worker(
                "v1:tenant-123:unscoped:general",
            )
            .shared_manager()
            .load_credentials("spotify")
        )
        assert shared_worker_credentials == {"access_token": "legacy-token"}

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
        mindroom.credentials._credentials_manager_signature = None

    def test_get_credentials_manager_singleton(self, tmp_path: Path) -> None:
        """Test that get_credentials_manager returns the same instance."""
        manager1 = get_credentials_manager(storage_root=tmp_path)
        manager2 = get_credentials_manager(storage_root=tmp_path)
        assert manager1 is manager2

    def test_global_manager_uses_explicit_storage_root(self, tmp_path: Path) -> None:
        """Test that global manager uses the provided storage root."""
        manager = get_credentials_manager(storage_root=tmp_path)
        assert manager.base_path == tmp_path / "credentials"

    def test_global_manager_uses_explicit_shared_credentials_path(
        self,
        tmp_path: Path,
    ) -> None:
        """Dedicated workers should be able to configure a distinct shared credential mirror path."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "models:\n  default:\n    provider: openai\n    id: gpt-5.4\nagents: {}\nrouter:\n  model: default\n",
            encoding="utf-8",
        )
        storage_path = (tmp_path / "worker-root").resolve()
        shared_path = storage_path / ".shared_credentials"
        runtime_paths = constants_mod.resolve_runtime_paths(
            config_path=config_path,
            storage_path=storage_path,
            process_env={SHARED_CREDENTIALS_PATH_ENV: str(shared_path)},
        )

        manager = get_runtime_credentials_manager(runtime_paths)

        assert manager.base_path == storage_path / "credentials"
        assert manager.shared_base_path == shared_path

    def test_global_manager_rebuilds_when_storage_root_changes(self, tmp_path: Path) -> None:
        """Changing the explicit storage root should invalidate the cached manager."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "models:\n  default:\n    provider: openai\n    id: gpt-5.4\nagents: {}\nrouter:\n  model: default\n",
            encoding="utf-8",
        )
        first_root = tmp_path / "one"
        second_root = tmp_path / "two"

        manager_one = get_credentials_manager(storage_root=first_root)

        manager_two = get_credentials_manager(storage_root=second_root)

        assert manager_one.base_path == first_root / "credentials"
        assert manager_two.base_path == second_root / "credentials"
        assert manager_one is not manager_two

    def test_dedicated_worker_manager_reads_mirrored_shared_credentials(
        self,
        tmp_path: Path,
    ) -> None:
        """Dedicated worker processes should load mirrored shared credentials through the global manager."""
        root = (tmp_path / "shared-storage").resolve()
        base_manager = CredentialsManager(root / "credentials")
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "models:\n  default:\n    provider: openai\n    id: gpt-5.4\nagents: {}\nrouter:\n  model: default\n",
            encoding="utf-8",
        )
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
        worker_key = "v1:tenant-123:shared:general"
        base_manager.save_credentials("google", {"api_key": "env-key", "_source": "env"})
        sync_shared_credentials_to_worker(worker_key, credentials_manager=base_manager)
        worker_root = base_manager.for_worker(worker_key).storage_root

        mindroom.credentials._credentials_manager = None
        mindroom.credentials._credentials_manager_signature = None

        runtime_paths = constants_mod.resolve_runtime_paths(
            config_path=config_path,
            storage_path=worker_root,
            process_env={
                SHARED_CREDENTIALS_PATH_ENV: str(worker_root / ".shared_credentials"),
                _DEDICATED_WORKER_KEY_ENV: worker_key,
                _DEDICATED_WORKER_ROOT_ENV: str(worker_root),
            },
        )
        manager = get_runtime_credentials_manager(runtime_paths)
        loaded_credentials = load_scoped_credentials(
            "google",
            credentials_manager=manager,
            worker_target=_worker_target("shared", "general", execution_identity),
        )

        assert loaded_credentials == {"api_key": "env-key", "_source": "env"}


class TestSharedIntegrationCredentialTagging:
    """Regression tests for shared-only integration credential saves."""

    def test_google_token_data_is_tagged_as_ui_source(self) -> None:
        """Google OAuth tokens saved through the dashboard should be tagged as UI-managed."""

        class _FakeGoogleCredentials:
            def __init__(self) -> None:
                self.token = "access-token"  # noqa: S105
                self.refresh_token = "refresh-token"  # noqa: S105
                self.token_uri = "https://oauth2.googleapis.com/token"  # noqa: S105
                self.client_id = "client-id"
                self.client_secret = "client-secret"  # noqa: S105
                self.scopes = ("scope-a", "scope-b")
                self.id_token = None

        token_data = _build_google_token_data(_FakeGoogleCredentials())

        assert token_data["_source"] == "ui"

    def test_spotify_credentials_saved_from_dashboard_are_tagged_as_ui_source(
        self,
        temp_credentials_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Spotify OAuth saves should mark credentials as UI-managed so unscoped workers mirror them."""
        manager = CredentialsManager(temp_credentials_dir)
        target = RequestCredentialsTarget(
            base_manager=manager,
            target_manager=manager,
            worker_scope=None,
            agent_name=None,
            execution_identity=None,
        )

        def _resolve_target(*_args: object, **_kwargs: object) -> RequestCredentialsTarget:
            return target

        monkeypatch.setattr(
            "mindroom.api.integrations.resolve_request_credentials_target",
            _resolve_target,
        )

        _save_spotify_credentials({"access_token": "spotify-token"}, object())

        assert manager.load_credentials("spotify") == {
            "access_token": "spotify-token",
            "_source": "ui",
        }

    def test_dedicated_worker_manager_uses_current_worker_root_for_shared_scope(
        self,
        tmp_path: Path,
    ) -> None:
        """Dedicated workers mounted at arbitrary paths should read and write shared-scope credentials in the mounted root."""
        worker_root = (tmp_path / "app-worker").resolve()
        worker_manager = CredentialsManager(
            base_path=worker_root / "credentials",
            shared_base_path=worker_root / ".shared_credentials",
            current_worker_key="v1:tenant-123:shared:general",
            current_worker_root=worker_root,
        )
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
        worker_manager.save_credentials("google", {"token": "ui-token", "_source": "ui"})

        loaded_credentials = load_scoped_credentials(
            "google",
            credentials_manager=worker_manager,
            worker_target=_worker_target("shared", "general", execution_identity),
        )

        assert loaded_credentials == {"token": "ui-token", "_source": "ui"}

        save_scoped_credentials(
            "google",
            {"token": "refreshed-token", "_source": "ui"},
            credentials_manager=worker_manager,
            worker_target=_worker_target("shared", "general", execution_identity),
        )

        assert worker_manager.load_credentials("google") == {"token": "refreshed-token", "_source": "ui"}
        assert not (worker_root / "workers").exists()

    def test_dedicated_worker_manager_uses_current_worker_root_for_isolating_scope(
        self,
        tmp_path: Path,
    ) -> None:
        """Dedicated workers mounted at arbitrary paths should not nest isolating-scope credentials under another workers/ tree."""
        worker_root = (tmp_path / "app-worker").resolve()
        worker_manager = CredentialsManager(
            base_path=worker_root / "credentials",
            shared_base_path=worker_root / ".shared_credentials",
            current_worker_key="v1:tenant-123:user:@alice:example.org",
            current_worker_root=worker_root,
        )
        execution_identity = ToolExecutionIdentity(
            channel="matrix",
            agent_name="persistent_worker_lab",
            requester_id="@alice:example.org",
            room_id="!room:example.org",
            thread_id=None,
            resolved_thread_id=None,
            session_id=None,
            tenant_id="tenant-123",
            account_id="account-456",
        )
        worker_manager.shared_manager().save_credentials("google", {"api_key": "env-key", "_source": "env"})
        worker_manager.save_credentials("google", {"token": "ui-token", "_source": "ui"})

        loaded_credentials = load_scoped_credentials(
            "google",
            credentials_manager=worker_manager,
            worker_target=_worker_target("user", "persistent_worker_lab", execution_identity),
        )

        assert loaded_credentials == {"api_key": "env-key", "token": "ui-token", "_source": "ui"}

        save_scoped_credentials(
            "google",
            {"token": "refreshed-token", "_source": "ui"},
            credentials_manager=worker_manager,
            worker_target=_worker_target("user", "persistent_worker_lab", execution_identity),
        )

        assert worker_manager.load_credentials("google") == {"token": "refreshed-token", "_source": "ui"}
        assert not (worker_root / "workers").exists()
