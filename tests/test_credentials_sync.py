"""Tests for syncing shared provider/bootstrap credentials from runtime env."""

import json
import os
from pathlib import Path

import pytest

from mindroom import constants as constants_mod
from mindroom import credentials_sync as credentials_sync_mod
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


def _credential_seed_json(service: str = "google_oauth_client") -> str:
    return json.dumps(
        [
            {
                "service": service,
                "credentials": {
                    "client_id": {"env": "OAUTH_CLIENT_ID"},
                    "client_secret": {"env": "OAUTH_CLIENT_SECRET"},
                },
            },
        ],
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

    def test_sync_env_does_not_seed_legacy_google_oauth_client(
        self,
        temp_credentials_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Legacy Google OAuth client env vars should not seed stored client config."""
        monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id")
        monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "client-secret")

        sync_env_to_credentials(
            runtime_paths=_runtime_paths(
                temp_credentials_dir.parent,
                shared_credentials_dir=temp_credentials_dir,
            ),
        )

        cm = CredentialsManager(base_path=temp_credentials_dir)
        assert cm.load_credentials("google_oauth_client") is None

    def test_sync_declared_credential_seed_from_env(
        self,
        temp_credentials_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Explicit credential seeds should populate named services from env values."""
        monkeypatch.setenv("OAUTH_CLIENT_ID", "client-id")
        monkeypatch.setenv("OAUTH_CLIENT_SECRET", "client-secret")
        monkeypatch.setenv("MINDROOM_CREDENTIAL_SEEDS_JSON", _credential_seed_json())

        sync_env_to_credentials(
            runtime_paths=_runtime_paths(
                temp_credentials_dir.parent,
                shared_credentials_dir=temp_credentials_dir,
            ),
        )

        cm = CredentialsManager(base_path=temp_credentials_dir)
        assert cm.load_credentials("google_oauth_client") == {
            "client_id": "client-id",
            "client_secret": "client-secret",
            "_source": "env",
        }

    def test_sync_declared_credential_seed_from_file(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Credential seed specs can live in a config-relative JSON file."""
        config_dir = tmp_path / "cfg"
        config_dir.mkdir(parents=True, exist_ok=True)
        credentials_dir = tmp_path / "credentials"
        credentials_dir.mkdir()
        seed_file = config_dir / "credential-seeds.json"
        seed_file.write_text(_credential_seed_json(service="example_oauth_client"), encoding="utf-8")
        config_path = config_dir / "config.yaml"
        config_path.write_text("agents: {}\nmodels: {}\nrouter:\n  model: default\n", encoding="utf-8")
        (config_dir / ".env").write_text(
            f"MINDROOM_CREDENTIAL_SEEDS_FILE=credential-seeds.json\n{SHARED_CREDENTIALS_PATH_ENV}={credentials_dir}\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("OAUTH_CLIENT_ID", "client-id")
        monkeypatch.setenv("OAUTH_CLIENT_SECRET", "client-secret")

        sync_env_to_credentials(
            runtime_paths=constants_mod.resolve_runtime_paths(
                config_path=config_path,
                storage_path=tmp_path,
                process_env={
                    "OAUTH_CLIENT_ID": "client-id",
                    "OAUTH_CLIENT_SECRET": "client-secret",
                },
            ),
        )

        cm = CredentialsManager(base_path=credentials_dir)
        assert cm.load_credentials("example_oauth_client") == {
            "client_id": "client-id",
            "client_secret": "client-secret",
            "_source": "env",
        }

    def test_sync_declared_credential_seed_reads_file_backed_values(
        self,
        temp_credentials_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Seed field env refs should honor the existing NAME_FILE secret convention."""
        secret_file = temp_credentials_dir.parent / "oauth-client-secret"
        secret_file.write_text("client-secret\n", encoding="utf-8")
        monkeypatch.setenv("OAUTH_CLIENT_ID", "client-id")
        monkeypatch.setenv("OAUTH_CLIENT_SECRET_FILE", str(secret_file))
        monkeypatch.setenv("MINDROOM_CREDENTIAL_SEEDS_JSON", _credential_seed_json())

        sync_env_to_credentials(
            runtime_paths=_runtime_paths(
                temp_credentials_dir.parent,
                shared_credentials_dir=temp_credentials_dir,
            ),
        )

        cm = CredentialsManager(base_path=temp_credentials_dir)
        assert cm.load_credentials("google_oauth_client") == {
            "client_id": "client-id",
            "client_secret": "client-secret",
            "_source": "env",
        }

    def test_sync_declared_credential_seed_reads_literal_and_file_values(
        self,
        temp_credentials_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Seed field declarations should support literal values and direct file refs."""
        secret_file = temp_credentials_dir.parent / "oauth-client-secret"
        secret_file.write_text("client-secret\n", encoding="utf-8")
        monkeypatch.setenv(
            "MINDROOM_CREDENTIAL_SEEDS_JSON",
            json.dumps(
                {
                    "service": "google_oauth_client",
                    "credentials": {
                        "client_id": {"value": "client-id"},
                        "client_secret": {"file": str(secret_file)},
                    },
                },
            ),
        )

        sync_env_to_credentials(
            runtime_paths=_runtime_paths(
                temp_credentials_dir.parent,
                shared_credentials_dir=temp_credentials_dir,
            ),
        )

        cm = CredentialsManager(base_path=temp_credentials_dir)
        assert cm.load_credentials("google_oauth_client") == {
            "client_id": "client-id",
            "client_secret": "client-secret",
            "_source": "env",
        }

    def test_sync_declared_credential_seed_updates_env_sourced_credentials(
        self,
        temp_credentials_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Declared seeds may update credentials they previously seeded from env."""
        cm = CredentialsManager(base_path=temp_credentials_dir)
        cm.save_credentials(
            "google_oauth_client",
            {"client_id": "old-client-id", "client_secret": "old-secret", "_source": "env"},
        )
        monkeypatch.setenv("OAUTH_CLIENT_ID", "new-client-id")
        monkeypatch.setenv("OAUTH_CLIENT_SECRET", "new-secret")
        monkeypatch.setenv("MINDROOM_CREDENTIAL_SEEDS_JSON", _credential_seed_json())

        sync_env_to_credentials(
            runtime_paths=_runtime_paths(
                temp_credentials_dir.parent,
                shared_credentials_dir=temp_credentials_dir,
            ),
        )

        assert cm.load_credentials("google_oauth_client") == {
            "client_id": "new-client-id",
            "client_secret": "new-secret",
            "_source": "env",
        }

    def test_sync_declared_credential_seed_does_not_overwrite_ui_credentials(
        self,
        temp_credentials_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Declared seeds must not overwrite dashboard-managed credentials."""
        cm = CredentialsManager(base_path=temp_credentials_dir)
        cm.save_credentials(
            "google_oauth_client",
            {"client_id": "ui-client-id", "client_secret": "ui-secret", "_source": "ui"},
        )
        monkeypatch.setenv("OAUTH_CLIENT_ID", "env-client-id")
        monkeypatch.setenv("OAUTH_CLIENT_SECRET", "env-secret")
        monkeypatch.setenv("MINDROOM_CREDENTIAL_SEEDS_JSON", _credential_seed_json())

        sync_env_to_credentials(
            runtime_paths=_runtime_paths(
                temp_credentials_dir.parent,
                shared_credentials_dir=temp_credentials_dir,
            ),
        )

        assert cm.load_credentials("google_oauth_client") == {
            "client_id": "ui-client-id",
            "client_secret": "ui-secret",
            "_source": "ui",
        }

    def test_sync_declared_credential_seed_skips_missing_values(
        self,
        temp_credentials_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Declared seeds should not create partial credentials when a value is missing."""
        monkeypatch.setenv("OAUTH_CLIENT_ID", "client-id")
        monkeypatch.delenv("OAUTH_CLIENT_SECRET", raising=False)
        monkeypatch.delenv("OAUTH_CLIENT_SECRET_FILE", raising=False)
        monkeypatch.setenv("MINDROOM_CREDENTIAL_SEEDS_JSON", _credential_seed_json())

        sync_env_to_credentials(
            runtime_paths=_runtime_paths(
                temp_credentials_dir.parent,
                shared_credentials_dir=temp_credentials_dir,
            ),
        )

        cm = CredentialsManager(base_path=temp_credentials_dir)
        assert cm.load_credentials("google_oauth_client") is None

    def test_malformed_declared_credential_seed_json_does_not_block_builtin_sync(
        self,
        temp_credentials_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Malformed seed JSON should not prevent normal provider env syncing."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-openai-key")
        monkeypatch.setenv("MINDROOM_CREDENTIAL_SEEDS_JSON", "{")

        sync_env_to_credentials(
            runtime_paths=_runtime_paths(
                temp_credentials_dir.parent,
                shared_credentials_dir=temp_credentials_dir,
            ),
        )

        cm = CredentialsManager(base_path=temp_credentials_dir)
        assert cm.get_api_key("openai") == "sk-test-openai-key"

    def test_malformed_declared_credential_seed_file_does_not_block_builtin_sync(
        self,
        tmp_path: Path,
    ) -> None:
        """Malformed file-backed seed JSON should not prevent normal provider env syncing."""
        config_dir = tmp_path / "cfg"
        config_dir.mkdir(parents=True, exist_ok=True)
        credentials_dir = tmp_path / "credentials"
        credentials_dir.mkdir()
        seed_file = config_dir / "credential-seeds.json"
        seed_file.write_text("{", encoding="utf-8")
        config_path = config_dir / "config.yaml"
        config_path.write_text("agents: {}\nmodels: {}\nrouter:\n  model: default\n", encoding="utf-8")
        (config_dir / ".env").write_text(
            f"MINDROOM_CREDENTIAL_SEEDS_FILE=credential-seeds.json\n{SHARED_CREDENTIALS_PATH_ENV}={credentials_dir}\n",
            encoding="utf-8",
        )

        sync_env_to_credentials(
            runtime_paths=constants_mod.resolve_runtime_paths(
                config_path=config_path,
                storage_path=tmp_path,
                process_env={"OPENAI_API_KEY": "sk-test-openai-key"},
            ),
        )

        cm = CredentialsManager(base_path=credentials_dir)
        assert cm.get_api_key("openai") == "sk-test-openai-key"

    def test_invalid_declared_credential_seed_shape_does_not_block_builtin_sync(
        self,
        temp_credentials_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Decoded-but-invalid seed declarations should not abort provider env syncing."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-openai-key")
        monkeypatch.setenv("MINDROOM_CREDENTIAL_SEEDS_JSON", json.dumps({"seeds": "not-a-list"}))

        sync_env_to_credentials(
            runtime_paths=_runtime_paths(
                temp_credentials_dir.parent,
                shared_credentials_dir=temp_credentials_dir,
            ),
        )

        cm = CredentialsManager(base_path=temp_credentials_dir)
        assert cm.get_api_key("openai") == "sk-test-openai-key"

    def test_invalid_declared_credential_seed_entry_does_not_block_later_valid_seed(
        self,
        temp_credentials_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """One invalid optional seed should not prevent later valid seeds from syncing."""
        monkeypatch.setenv("OAUTH_CLIENT_ID", "client-id")
        monkeypatch.setenv("OAUTH_CLIENT_SECRET", "client-secret")
        monkeypatch.setenv(
            "MINDROOM_CREDENTIAL_SEEDS_JSON",
            json.dumps(
                [
                    {
                        "service": 123,
                        "credentials": {"token": {"value": "bad"}},
                    },
                    {
                        "service": "google_oauth_client",
                        "credentials": {
                            "client_id": {"env": "OAUTH_CLIENT_ID"},
                            "client_secret": {"env": "OAUTH_CLIENT_SECRET"},
                        },
                    },
                ],
            ),
        )

        sync_env_to_credentials(
            runtime_paths=_runtime_paths(
                temp_credentials_dir.parent,
                shared_credentials_dir=temp_credentials_dir,
            ),
        )

        cm = CredentialsManager(base_path=temp_credentials_dir)
        assert cm.load_credentials("google_oauth_client") == {
            "client_id": "client-id",
            "client_secret": "client-secret",
            "_source": "env",
        }

    def test_declared_credential_seed_env_names_stay_out_of_runtime_env_views(
        self,
        tmp_path: Path,
    ) -> None:
        """Credential seed declaration env vars must not leak to worker/runtime views."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text("agents: {}\nmodels: {}\nrouter:\n  model: default\n", encoding="utf-8")
        seed_file = tmp_path / "credential-seeds.json"
        seed_file.write_text(_credential_seed_json(), encoding="utf-8")
        seed_json = json.dumps(
            {
                "service": "example_oauth_client",
                "credentials": {"client_secret": {"value": "literal-secret"}},
            },
        )
        runtime_paths = constants_mod.resolve_runtime_paths(
            config_path=config_path,
            storage_path=tmp_path,
            process_env={
                "MINDROOM_CREDENTIAL_SEEDS_JSON": seed_json,
                "MINDROOM_CREDENTIAL_SEEDS_FILE": str(seed_file),
            },
        )

        public_runtime = constants_mod.serialize_public_runtime_paths(runtime_paths)
        isolated_runtime = constants_mod.isolated_runtime_paths(runtime_paths)
        runtime_envs = [
            public_runtime["process_env"],
            public_runtime["env_file_values"],
            isolated_runtime.process_env,
            isolated_runtime.env_file_values,
            constants_mod.execution_runtime_env_values(runtime_paths),
            constants_mod.sandbox_execution_runtime_env_values(runtime_paths),
        ]

        for runtime_env in runtime_envs:
            assert "MINDROOM_CREDENTIAL_SEEDS_JSON" not in runtime_env
            assert "MINDROOM_CREDENTIAL_SEEDS_FILE" not in runtime_env

    def test_declared_credential_seed_logs_file_source(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """File-backed seed sync should identify the file env var as its source."""
        config_dir = tmp_path / "cfg"
        config_dir.mkdir(parents=True, exist_ok=True)
        credentials_dir = tmp_path / "credentials"
        credentials_dir.mkdir()
        seed_file = config_dir / "credential-seeds.json"
        seed_file.write_text(_credential_seed_json(), encoding="utf-8")
        config_path = config_dir / "config.yaml"
        config_path.write_text("agents: {}\nmodels: {}\nrouter:\n  model: default\n", encoding="utf-8")
        (config_dir / ".env").write_text(
            f"MINDROOM_CREDENTIAL_SEEDS_FILE=credential-seeds.json\n{SHARED_CREDENTIALS_PATH_ENV}={credentials_dir}\n",
            encoding="utf-8",
        )
        calls: list[dict[str, object]] = []

        def fake_sync_service_credentials(**kwargs: object) -> bool:
            calls.append(kwargs)
            return True

        monkeypatch.setattr(credentials_sync_mod, "_sync_service_credentials", fake_sync_service_credentials)

        sync_env_to_credentials(
            runtime_paths=constants_mod.resolve_runtime_paths(
                config_path=config_path,
                storage_path=tmp_path,
                process_env={
                    "OAUTH_CLIENT_ID": "client-id",
                    "OAUTH_CLIENT_SECRET": "client-secret",
                },
            ),
        )

        assert calls == [
            {
                "service": "google_oauth_client",
                "credentials": {"client_id": "client-id", "client_secret": "client-secret"},
                "runtime_paths": constants_mod.resolve_runtime_paths(
                    config_path=config_path,
                    storage_path=tmp_path,
                    process_env={
                        "OAUTH_CLIENT_ID": "client-id",
                        "OAUTH_CLIENT_SECRET": "client-secret",
                    },
                ),
                "env_var": "MINDROOM_CREDENTIAL_SEEDS_FILE",
            },
        ]

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
