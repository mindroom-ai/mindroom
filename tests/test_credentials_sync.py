"""Tests for credentials sync functionality."""

from pathlib import Path
from unittest.mock import patch

import pytest

from mindroom.credentials import CredentialsManager
from mindroom.credentials_sync import (
    ENV_TO_SERVICE_MAP,
    get_api_key_for_provider,
    get_ollama_host,
    sync_env_to_credentials,
)


class TestCredentialsSync:
    """Test the credentials sync functionality."""

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
        """Test syncing new API keys from environment."""
        # Set environment variables
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-openai-key")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-anthropic-key")
        monkeypatch.setenv("GOOGLE_API_KEY", "test-google-key")
        monkeypatch.setenv("OLLAMA_HOST", "http://test:11434")

        # Mock get_credentials_manager to use our temp directory
        with patch("mindroom.credentials_sync.get_credentials_manager") as mock_get_cm:
            mock_get_cm.return_value = CredentialsManager(base_path=temp_credentials_dir)

            # Run sync
            sync_env_to_credentials()

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

        with patch("mindroom.credentials_sync.get_credentials_manager") as mock_get_cm:
            mock_get_cm.return_value = cm
            sync_env_to_credentials()

            assert cm.get_api_key("openai") == "ui-set-key"

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

        with patch("mindroom.credentials_sync.get_credentials_manager") as mock_get_cm:
            mock_get_cm.return_value = cm
            sync_env_to_credentials()

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

        with patch("mindroom.credentials_sync.get_credentials_manager") as mock_get_cm:
            mock_get_cm.return_value = cm
            sync_env_to_credentials()

            assert cm.get_api_key("openai") == "new-env-key"

    def test_sync_env_to_credentials_skip_empty(
        self,
        temp_credentials_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Test that empty environment variables are skipped."""
        # Set one valid and one empty environment variable
        monkeypatch.setenv("OPENAI_API_KEY", "valid-key")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "")

        # Mock get_credentials_manager
        with patch("mindroom.credentials_sync.get_credentials_manager") as mock_get_cm:
            cm = CredentialsManager(base_path=temp_credentials_dir)
            mock_get_cm.return_value = cm

            # Run sync
            sync_env_to_credentials()

            # Verify only valid key was synced
            assert cm.get_api_key("openai") == "valid-key"
            assert cm.get_api_key("anthropic") is None

    def test_sync_env_seeds_github_private_from_github_token(
        self,
        temp_credentials_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """GITHUB_TOKEN should seed github_private credentials for Git KB auth."""
        monkeypatch.setenv("GITHUB_TOKEN", "ghp-test-token")

        with patch("mindroom.credentials_sync.get_credentials_manager") as mock_get_cm:
            cm = CredentialsManager(base_path=temp_credentials_dir)
            mock_get_cm.return_value = cm
            sync_env_to_credentials()

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

        with patch("mindroom.credentials_sync.get_credentials_manager") as mock_get_cm:
            mock_get_cm.return_value = cm
            sync_env_to_credentials()

            github_private = cm.load_credentials("github_private")
            assert github_private is not None
            assert github_private["token"] == ui_value
            assert github_private["_source"] == "ui"

    def test_get_api_key_for_provider(self, credentials_manager: CredentialsManager) -> None:
        """Test getting API key for different providers."""
        # Set up test data
        credentials_manager.set_api_key("openai", "test-openai-key")
        credentials_manager.set_api_key("google", "test-google-key")

        with patch("mindroom.credentials_sync.get_credentials_manager") as mock_get_cm:
            mock_get_cm.return_value = credentials_manager

            # Test normal providers
            assert get_api_key_for_provider("openai") == "test-openai-key"
            assert get_api_key_for_provider("google") == "test-google-key"

            # Test gemini alias for google
            assert get_api_key_for_provider("gemini") == "test-google-key"

            # Test ollama returns None
            assert get_api_key_for_provider("ollama") is None

            # Test non-existent provider
            assert get_api_key_for_provider("anthropic") is None

    def test_get_ollama_host(self, credentials_manager: CredentialsManager) -> None:
        """Test getting Ollama host configuration."""
        # Test when no Ollama config exists
        with patch("mindroom.credentials_sync.get_credentials_manager") as mock_get_cm:
            mock_get_cm.return_value = credentials_manager
            assert get_ollama_host() is None

            # Set Ollama host
            credentials_manager.save_credentials("ollama", {"host": "http://localhost:11434"})
            assert get_ollama_host() == "http://localhost:11434"

    def test_all_env_vars_mapped(self) -> None:
        """Test that all expected environment variables are in the mapping."""
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

        assert expected_services == ENV_TO_SERVICE_MAP

    def test_sync_idempotent(self, temp_credentials_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that running sync multiple times doesn't cause issues."""
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")

        with patch("mindroom.credentials_sync.get_credentials_manager") as mock_get_cm:
            cm = CredentialsManager(base_path=temp_credentials_dir)
            mock_get_cm.return_value = cm

            # Run sync multiple times
            sync_env_to_credentials()
            sync_env_to_credentials()
            sync_env_to_credentials()

            # Should still have the same value
            assert cm.get_api_key("openai") == "test-key"

            # Should only have one file
            openai_files = list(temp_credentials_dir.glob("openai_*.json"))
            assert len(openai_files) == 1
