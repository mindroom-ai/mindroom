"""Tests for syncing shared provider/bootstrap credentials from runtime env."""

import os
from pathlib import Path

import pytest

from mindroom import constants as constants_mod
from mindroom.credentials import SHARED_CREDENTIALS_PATH_ENV, CredentialsManager
from mindroom.credentials_sync import (
    _ENV_TO_SERVICE_MAP,
    get_api_key_for_provider,
    get_ollama_host,
    get_secret_from_env,
    sync_env_to_credentials,
)


def _runtime_paths(
    storage_root: Path,
    *,
    shared_credentials_dir: Path | None = None,
) -> constants_mod.RuntimePaths:
    storage_root.mkdir(parents=True, exist_ok=True)
    config_path = storage_root / "config.yaml"
    config_path.write_text("agents: {}\nmodels: {}\nrouter:\n  model: default\n", encoding="utf-8")
    process_env = dict(os.environ)
    if shared_credentials_dir is not None:
        process_env[SHARED_CREDENTIALS_PATH_ENV] = str(shared_credentials_dir)
    return constants_mod.resolve_runtime_paths(
        config_path=config_path,
        storage_path=storage_root,
        process_env=process_env,
    )


class TestCredentialsSync:
    """Test the shared provider/bootstrap credential sync behavior."""

    @pytest.fixture
    def temp_credentials_dir(self, tmp_path: Path) -> Path:
        """Create a temporary credentials directory."""
        creds_dir = tmp_path / "credentials"
        creds_dir.mkdir()
        return creds_dir

    @pytest.fixture
    def credentials_manager(self, temp_credentials_dir: Path) -> CredentialsManager:
        """Create a CredentialsManager with a temporary directory."""
        return CredentialsManager(base_path=temp_credentials_dir)

    def test_sync_env_to_credentials_new_keys(
        self,
        temp_credentials_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Supported shared provider/bootstrap env values should seed credentials."""
        # Set shared provider/bootstrap env values.
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-openai-key")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-anthropic-key")
        monkeypatch.setenv("GOOGLE_API_KEY", "test-google-key")
        monkeypatch.setenv("OLLAMA_HOST", "http://test:11434")

        runtime_paths = _runtime_paths(
            temp_credentials_dir.parent,
            shared_credentials_dir=temp_credentials_dir,
        )

        # Run sync
        sync_env_to_credentials(runtime_paths=runtime_paths)

        # Verify files were created
        openai_file = temp_credentials_dir / "openai_credentials.json"
        anthropic_file = temp_credentials_dir / "anthropic_credentials.json"
        google_file = temp_credentials_dir / "google_credentials.json"
        ollama_file = temp_credentials_dir / "ollama_credentials.json"

        assert openai_file.exists()
        assert anthropic_file.exists()
        assert google_file.exists()
        assert ollama_file.exists()

        # Verify content
        cm = CredentialsManager(base_path=temp_credentials_dir)
        assert cm.get_api_key("openai") == "sk-test-openai-key"
        assert cm.get_api_key("anthropic") == "sk-test-anthropic-key"
        assert cm.get_api_key("google") == "test-google-key"

        # Verify source metadata is tracked
        openai_creds = cm.load_credentials("openai")
        assert openai_creds["_source"] == "env"

        ollama_creds = cm.load_credentials("ollama")
        assert ollama_creds["host"] == "http://test:11434"
        assert ollama_creds["_source"] == "env"

    def test_sync_env_does_not_overwrite_ui_credentials(
        self,
        temp_credentials_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Test that env sync does NOT overwrite UI-set credentials."""
        cm = CredentialsManager(base_path=temp_credentials_dir)
        cm.save_credentials("openai", {"api_key": "ui-set-key", "_source": "ui"})

        monkeypatch.setenv("OPENAI_API_KEY", "env-key")

        sync_env_to_credentials(
            runtime_paths=_runtime_paths(
                temp_credentials_dir.parent,
                shared_credentials_dir=temp_credentials_dir,
            ),
        )

        assert cm.get_api_key("openai") == "ui-set-key"

    def test_get_secret_from_env_resolves_relative_file_paths_from_config_dir(self, tmp_path: Path) -> None:
        """Relative *_FILE secret paths in the runtime `.env` should anchor to the config directory."""
        config_dir = tmp_path / "cfg"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_path = config_dir / "config.yaml"
        config_path.write_text("agents: {}\nmodels: {}\nrouter:\n  model: default\n", encoding="utf-8")
        secret_file = config_dir / "secrets" / "openai.key"
        secret_file.parent.mkdir(parents=True, exist_ok=True)
        secret_file.write_text("sk-relative", encoding="utf-8")
        (config_dir / ".env").write_text("OPENAI_API_KEY_FILE=secrets/openai.key\n", encoding="utf-8")

        runtime_paths = constants_mod.resolve_runtime_paths(config_path=config_path, process_env={})

        assert get_secret_from_env("OPENAI_API_KEY", runtime_paths) == "sk-relative"

    def test_sync_env_does_not_overwrite_legacy_credentials(
        self,
        temp_credentials_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Test that env sync does NOT overwrite legacy credentials (no _source)."""
        cm = CredentialsManager(base_path=temp_credentials_dir)
        # Legacy credential without _source field
        cm.set_api_key("openai", "legacy-key")

        monkeypatch.setenv("OPENAI_API_KEY", "env-key")

        sync_env_to_credentials(
            runtime_paths=_runtime_paths(
                temp_credentials_dir.parent,
                shared_credentials_dir=temp_credentials_dir,
            ),
        )

        assert cm.get_api_key("openai") == "legacy-key"

    def test_sync_env_updates_env_sourced_credentials(
        self,
        temp_credentials_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Test that env sync DOES update env-sourced credentials."""
        cm = CredentialsManager(base_path=temp_credentials_dir)
        cm.save_credentials("openai", {"api_key": "old-env-key", "_source": "env"})

        monkeypatch.setenv("OPENAI_API_KEY", "new-env-key")

        sync_env_to_credentials(
            runtime_paths=_runtime_paths(
                temp_credentials_dir.parent,
                shared_credentials_dir=temp_credentials_dir,
            ),
        )

        assert cm.get_api_key("openai") == "new-env-key"

    def test_sync_env_to_credentials_skip_empty(
        self,
        temp_credentials_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Empty shared env values should be ignored."""
        # Set one valid and one empty shared env value.
        monkeypatch.setenv("OPENAI_API_KEY", "valid-key")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "")

        cm = CredentialsManager(base_path=temp_credentials_dir)

        # Run sync
        sync_env_to_credentials(
            runtime_paths=_runtime_paths(
                temp_credentials_dir.parent,
                shared_credentials_dir=temp_credentials_dir,
            ),
        )

        # Verify only valid key was synced
        assert cm.get_api_key("openai") == "valid-key"
        assert cm.get_api_key("anthropic") is None

    def test_sync_env_to_credentials_uses_runtime_storage_override(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Env sync should write into the explicit runtime storage root."""
        runtime_storage = (tmp_path / "runtime-storage").resolve()
        monkeypatch.setenv("OPENAI_API_KEY", "runtime-key")
        runtime_paths = _runtime_paths(runtime_storage)

        sync_env_to_credentials(runtime_paths=runtime_paths)

        manager = CredentialsManager(base_path=runtime_storage / "credentials")
        assert manager.base_path == runtime_storage / "credentials"
        assert manager.get_api_key("openai") == "runtime-key"

    def test_sync_env_seeds_github_private_from_github_token(
        self,
        temp_credentials_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """GITHUB_TOKEN should seed github_private credentials for Git KB auth."""
        monkeypatch.setenv("GITHUB_TOKEN", "ghp-test-token")

        cm = CredentialsManager(base_path=temp_credentials_dir)
        sync_env_to_credentials(
            runtime_paths=_runtime_paths(
                temp_credentials_dir.parent,
                shared_credentials_dir=temp_credentials_dir,
            ),
        )

        github_private = cm.load_credentials("github_private")
        assert github_private == {
            "username": "x-access-token",
            "token": "ghp-test-token",
            "_source": "env",
        }

    def test_sync_env_does_not_overwrite_ui_github_private_credentials(
        self,
        temp_credentials_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """UI-managed github_private credentials must not be overwritten by env sync."""
        cm = CredentialsManager(base_path=temp_credentials_dir)
        ui_value = "ui-value"
        cm.save_credentials(
            "github_private",
            {"username": "my-user", "token": ui_value, "_source": "ui"},
        )
        monkeypatch.setenv("GITHUB_TOKEN", "ghp-env-token")

        sync_env_to_credentials(
            runtime_paths=_runtime_paths(
                temp_credentials_dir.parent,
                shared_credentials_dir=temp_credentials_dir,
            ),
        )

        github_private = cm.load_credentials("github_private")
        assert github_private is not None
        assert github_private["token"] == ui_value
        assert github_private["_source"] == "ui"

    def test_get_api_key_for_provider(self, credentials_manager: CredentialsManager) -> None:
        """Test getting API key for different providers."""
        # Set up test data
        credentials_manager.set_api_key("openai", "test-openai-key")
        credentials_manager.set_api_key("google", "test-google-key")
        runtime_paths = _runtime_paths(
            credentials_manager.storage_root,
            shared_credentials_dir=credentials_manager.base_path,
        )

        # Test normal providers
        assert get_api_key_for_provider("openai", runtime_paths=runtime_paths) == "test-openai-key"
        assert get_api_key_for_provider("google", runtime_paths=runtime_paths) == "test-google-key"

        # Test gemini alias for google
        assert get_api_key_for_provider("gemini", runtime_paths=runtime_paths) == "test-google-key"

        # Test ollama returns None
        assert get_api_key_for_provider("ollama", runtime_paths=runtime_paths) is None

        # Test non-existent provider
        assert get_api_key_for_provider("anthropic", runtime_paths=runtime_paths) is None

    def test_get_ollama_host(self, credentials_manager: CredentialsManager) -> None:
        """Test getting Ollama host configuration."""
        # Test when no Ollama config exists
        runtime_paths = _runtime_paths(
            credentials_manager.storage_root,
            shared_credentials_dir=credentials_manager.base_path,
        )
        assert get_ollama_host(runtime_paths=runtime_paths) is None

        # Set Ollama host
        credentials_manager.save_credentials("ollama", {"host": "http://localhost:11434"})
        assert get_ollama_host(runtime_paths=runtime_paths) == "http://localhost:11434"

    def test_all_env_vars_mapped(self) -> None:
        """All supported shared provider/bootstrap env vars should be mapped."""
        expected_services = {
            "OPENAI_API_KEY": "openai",
            "ANTHROPIC_API_KEY": "anthropic",
            "GOOGLE_API_KEY": "google",
            "OPENROUTER_API_KEY": "openrouter",
            "DEEPSEEK_API_KEY": "deepseek",
            "CEREBRAS_API_KEY": "cerebras",
            "GROQ_API_KEY": "groq",
            "OLLAMA_HOST": "ollama",
        }

        assert expected_services == _ENV_TO_SERVICE_MAP

    def test_sync_idempotent(self, temp_credentials_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that running sync multiple times doesn't cause issues."""
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")

        cm = CredentialsManager(base_path=temp_credentials_dir)

        # Run sync multiple times
        runtime_paths = _runtime_paths(
            temp_credentials_dir.parent,
            shared_credentials_dir=temp_credentials_dir,
        )
        sync_env_to_credentials(runtime_paths=runtime_paths)
        sync_env_to_credentials(runtime_paths=runtime_paths)
        sync_env_to_credentials(runtime_paths=runtime_paths)

        # Should still have the same value
        assert cm.get_api_key("openai") == "test-key"

        # Should only have one file
        openai_files = list(temp_credentials_dir.glob("openai_*.json"))
        assert len(openai_files) == 1
