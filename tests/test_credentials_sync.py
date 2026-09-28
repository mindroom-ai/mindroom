"""Tests for syncing shared provider/bootstrap credentials from runtime env."""

import base64
import json
import os
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from mindroom import constants as constants_mod
from mindroom import credentials_sync as credentials_sync_mod
from mindroom.credentials import CredentialsManager
from mindroom.credentials_sync import (
    _EMBEDDER_CREDENTIAL_SERVICE,
    _EMBEDDER_KEYLESS_PLACEHOLDER_API_KEY,
    _ENV_TO_SERVICE_MAP,
    get_api_key_for_provider,
    get_api_key_for_service,
    get_embedder_api_key,
    get_ollama_host,
    get_secret_from_env,
    sync_env_to_credentials,
)
from mindroom.runtime_env_policy import CREDENTIALS_ENCRYPTION_KEY_ENV, SHARED_CREDENTIALS_PATH_ENV


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

    def test_sync_declared_credential_seed_rejects_oauth_token_service(
        self,
        temp_credentials_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Bootstrap seeds must not bypass lifecycle publication for OAuth tokens."""
        monkeypatch.setenv(
            "MINDROOM_CREDENTIAL_SEEDS_JSON",
            json.dumps(
                {
                    "service": "google_oauth",
                    "credentials": {"access_token": {"value": "seeded-access-token"}},
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
        assert cm.load_credentials("google_oauth") is None

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

    def test_internal_credential_env_names_stay_out_of_public_and_execution_env_views(
        self,
        tmp_path: Path,
    ) -> None:
        """Internal credential env vars must not leak to public manifests or tool execution envs."""
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
                CREDENTIALS_ENCRYPTION_KEY_ENV: "encryption-key-material",
                "MINDROOM_CREDENTIAL_SEEDS_JSON": seed_json,
                "MINDROOM_CREDENTIAL_SEEDS_FILE": str(seed_file),
            },
        )

        public_runtime = constants_mod.serialize_public_runtime_paths(runtime_paths)
        isolated_runtime = constants_mod.isolated_runtime_paths(runtime_paths)
        public_and_execution_envs = [
            public_runtime["process_env"],
            public_runtime["env_file_values"],
            constants_mod.trusted_tool_runtime_env_values(runtime_paths),
            constants_mod.build_execution_tool_env("python", runtime_paths),
            constants_mod.trusted_tool_runtime_env_values(isolated_runtime),
            constants_mod.build_execution_tool_env("python", isolated_runtime),
        ]

        assert isolated_runtime.env_value(CREDENTIALS_ENCRYPTION_KEY_ENV) == "encryption-key-material"
        for runtime_env in public_and_execution_envs:
            assert CREDENTIALS_ENCRYPTION_KEY_ENV not in runtime_env
            assert "MINDROOM_CREDENTIAL_SEEDS_JSON" not in runtime_env
            assert "MINDROOM_CREDENTIAL_SEEDS_FILE" not in runtime_env
        assert isolated_runtime.process_env[CREDENTIALS_ENCRYPTION_KEY_ENV] == "encryption-key-material"
        assert "MINDROOM_CREDENTIAL_SEEDS_JSON" not in isolated_runtime.process_env
        assert "MINDROOM_CREDENTIAL_SEEDS_FILE" not in isolated_runtime.process_env
        assert "MINDROOM_CREDENTIAL_SEEDS_JSON" not in isolated_runtime.env_file_values
        assert "MINDROOM_CREDENTIAL_SEEDS_FILE" not in isolated_runtime.env_file_values

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

    def test_sync_env_does_not_overwrite_unreadable_existing_credentials(
        self,
        temp_credentials_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Encrypted-mode sync should fail closed when an existing credential file cannot be loaded."""
        encryption_key = base64.urlsafe_b64encode(b"0" * 32).decode("ascii")
        plaintext_credentials = {"api_key": "ui-set-key", "_source": "ui"}
        openai_path = temp_credentials_dir / "openai_credentials.json"
        openai_path.write_text(json.dumps(plaintext_credentials), encoding="utf-8")
        monkeypatch.setenv(CREDENTIALS_ENCRYPTION_KEY_ENV, encryption_key)
        monkeypatch.setenv("OPENAI_API_KEY", "env-key")

        sync_env_to_credentials(
            runtime_paths=_runtime_paths(
                temp_credentials_dir.parent,
                shared_credentials_dir=temp_credentials_dir,
            ),
        )

        assert json.loads(openai_path.read_text(encoding="utf-8")) == plaintext_credentials

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
        cm.save_credentials("openai", {"api_key": "legacy-key"})

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

    def test_github_private_sync_uses_env_owned_service_policy(
        self,
        temp_credentials_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """GitHub sync should reuse the generic env-owned credential save policy."""
        monkeypatch.setenv("GITHUB_TOKEN", "ghp-test-token")
        runtime_paths = _runtime_paths(
            temp_credentials_dir.parent,
            shared_credentials_dir=temp_credentials_dir,
        )
        calls: list[dict[str, object]] = []

        def fake_sync_service_credentials(**kwargs: object) -> bool:
            calls.append(kwargs)
            return True

        monkeypatch.setattr(credentials_sync_mod, "_sync_service_credentials", fake_sync_service_credentials)

        assert credentials_sync_mod._sync_github_private_credentials(runtime_paths=runtime_paths)
        assert calls == [
            {
                "service": "github_private",
                "credentials": {
                    "username": "x-access-token",
                    "token": "ghp-test-token",
                },
                "runtime_paths": runtime_paths,
                "env_var": "GITHUB_TOKEN",
            },
        ]

    def test_sync_env_updates_env_sourced_github_private_credentials(
        self,
        temp_credentials_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Env-sourced github_private credentials should follow GITHUB_TOKEN changes."""
        cm = CredentialsManager(base_path=temp_credentials_dir)
        cm.save_credentials(
            "github_private",
            {"username": "x-access-token", "token": "old-token", "_source": "env"},
        )
        monkeypatch.setenv("GITHUB_TOKEN", "ghp-new-token")

        sync_env_to_credentials(
            runtime_paths=_runtime_paths(
                temp_credentials_dir.parent,
                shared_credentials_dir=temp_credentials_dir,
            ),
        )

        assert cm.load_credentials("github_private") == {
            "username": "x-access-token",
            "token": "ghp-new-token",
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

    def test_sync_env_does_not_overwrite_legacy_github_private_credentials(
        self,
        temp_credentials_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Legacy github_private credentials without _source should not be overwritten."""
        cm = CredentialsManager(base_path=temp_credentials_dir)
        cm.save_credentials(
            "github_private",
            {"username": "my-user", "token": "legacy-token"},
        )
        monkeypatch.setenv("GITHUB_TOKEN", "ghp-env-token")

        sync_env_to_credentials(
            runtime_paths=_runtime_paths(
                temp_credentials_dir.parent,
                shared_credentials_dir=temp_credentials_dir,
            ),
        )

        assert cm.load_credentials("github_private") == {
            "username": "my-user",
            "token": "legacy-token",
        }

    def test_sync_env_skips_github_private_when_github_token_missing(
        self,
        temp_credentials_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Missing GITHUB_TOKEN should not create github_private credentials."""
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN_FILE", raising=False)

        sync_env_to_credentials(
            runtime_paths=_runtime_paths(
                temp_credentials_dir.parent,
                shared_credentials_dir=temp_credentials_dir,
            ),
        )

        cm = CredentialsManager(base_path=temp_credentials_dir)
        assert cm.load_credentials("github_private") is None

    def test_get_api_key_for_provider(self, credentials_manager: CredentialsManager) -> None:
        """Test getting API key for different providers."""
        # Set up test data
        credentials_manager.save_credentials("openai", {"api_key": "test-openai-key"})
        credentials_manager.save_credentials("google", {"api_key": "test-google-key"})
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

    def test_get_api_key_for_provider_accepts_env_var_named_service(
        self,
        credentials_manager: CredentialsManager,
    ) -> None:
        """Dashboard keys saved under the env var name resolve, but the canonical service wins."""
        credentials_manager.save_credentials("OPENROUTER_API_KEY", {"api_key": "env-named-openrouter-key"})
        credentials_manager.save_credentials("GOOGLE_API_KEY", {"api_key": "env-named-google-key"})
        credentials_manager.save_credentials("OLLAMA_HOST", {"api_key": "not-a-provider-key"})
        runtime_paths = _runtime_paths(
            credentials_manager.storage_root,
            shared_credentials_dir=credentials_manager.base_path,
        )

        assert get_api_key_for_provider("openrouter", runtime_paths=runtime_paths) == "env-named-openrouter-key"
        assert get_api_key_for_provider("gemini", runtime_paths=runtime_paths) == "env-named-google-key"
        assert get_api_key_for_provider("ollama", runtime_paths=runtime_paths) is None
        assert get_api_key_for_provider("openai", runtime_paths=runtime_paths) is None

        credentials_manager.save_credentials("openrouter", {"api_key": "canonical-openrouter-key"})
        assert get_api_key_for_provider("openrouter", runtime_paths=runtime_paths) == "canonical-openrouter-key"

    def test_get_api_key_for_service_is_strict(self, credentials_manager: CredentialsManager) -> None:
        """A named service resolves only that service's API key."""
        credentials_manager.save_credentials("openai", {"api_key": "shared-key"})
        credentials_manager.save_credentials("openai-realtime", {"api_key": "realtime-key"})
        runtime_paths = _runtime_paths(
            credentials_manager.storage_root,
            shared_credentials_dir=credentials_manager.base_path,
        )

        assert get_api_key_for_service("openai-realtime", runtime_paths) == "realtime-key"
        assert get_api_key_for_service("missing", runtime_paths) is None
        assert get_api_key_for_service("openrouter", runtime_paths) is None

        credentials_manager.save_credentials("OPENROUTER_API_KEY", {"api_key": "env-named-openrouter-key"})
        assert get_api_key_for_service("openrouter", runtime_paths) == "env-named-openrouter-key"

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
            "AZURE_OPENAI_API_KEY": "azure",
            "GOOGLE_API_KEY": "google",
            "OPENROUTER_API_KEY": "openrouter",
            "DEEPSEEK_API_KEY": "deepseek",
            "CEREBRAS_API_KEY": "cerebras",
            "GROQ_API_KEY": "groq",
            "ZAI_API_KEY": "zai",
            "OLLAMA_HOST": "ollama",
        }

        assert expected_services == _ENV_TO_SERVICE_MAP

    def test_sync_env_seeds_embedder_credential(
        self,
        temp_credentials_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """EMBEDDER_API_KEY should seed the dedicated embedder credential service."""
        monkeypatch.setenv("EMBEDDER_API_KEY", "sk-embedder-key")

        sync_env_to_credentials(
            runtime_paths=_runtime_paths(
                temp_credentials_dir.parent,
                shared_credentials_dir=temp_credentials_dir,
            ),
        )

        cm = CredentialsManager(base_path=temp_credentials_dir)
        assert cm.load_credentials(_EMBEDDER_CREDENTIAL_SERVICE) == {
            "api_key": "sk-embedder-key",
            "_source": "env",
        }

    def test_sync_env_seeds_embedder_credential_from_file(
        self,
        temp_credentials_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """EMBEDDER_API_KEY_FILE should follow the NAME_FILE secret convention."""
        key_file = temp_credentials_dir.parent / "embedder.key"
        key_file.write_text("sk-embedder-file-key\n", encoding="utf-8")
        monkeypatch.delenv("EMBEDDER_API_KEY", raising=False)
        monkeypatch.setenv("EMBEDDER_API_KEY_FILE", str(key_file))

        sync_env_to_credentials(
            runtime_paths=_runtime_paths(
                temp_credentials_dir.parent,
                shared_credentials_dir=temp_credentials_dir,
            ),
        )

        cm = CredentialsManager(base_path=temp_credentials_dir)
        assert cm.get_api_key(_EMBEDDER_CREDENTIAL_SERVICE) == "sk-embedder-file-key"

    def test_sync_env_does_not_overwrite_ui_embedder_credential(
        self,
        temp_credentials_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """UI-managed embedder credentials must not be overwritten by env sync."""
        cm = CredentialsManager(base_path=temp_credentials_dir)
        cm.save_credentials(_EMBEDDER_CREDENTIAL_SERVICE, {"api_key": "ui-embedder-key", "_source": "ui"})
        monkeypatch.setenv("EMBEDDER_API_KEY", "env-embedder-key")

        sync_env_to_credentials(
            runtime_paths=_runtime_paths(
                temp_credentials_dir.parent,
                shared_credentials_dir=temp_credentials_dir,
            ),
        )

        assert cm.get_api_key(_EMBEDDER_CREDENTIAL_SERVICE) == "ui-embedder-key"

    def test_sync_env_skips_embedder_when_env_missing(
        self,
        temp_credentials_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Missing EMBEDDER_API_KEY should not create the embedder credential."""
        monkeypatch.delenv("EMBEDDER_API_KEY", raising=False)
        monkeypatch.delenv("EMBEDDER_API_KEY_FILE", raising=False)

        sync_env_to_credentials(
            runtime_paths=_runtime_paths(
                temp_credentials_dir.parent,
                shared_credentials_dir=temp_credentials_dir,
            ),
        )

        cm = CredentialsManager(base_path=temp_credentials_dir)
        assert cm.load_credentials(_EMBEDDER_CREDENTIAL_SERVICE) is None

    def test_get_embedder_api_key_explicit_wins(self, credentials_manager: CredentialsManager) -> None:
        """An explicit config key must beat both credential services."""
        credentials_manager.save_credentials(_EMBEDDER_CREDENTIAL_SERVICE, {"api_key": "embedder-key"})
        credentials_manager.save_credentials("openai", {"api_key": "openai-key"})
        runtime_paths = _runtime_paths(
            credentials_manager.storage_root,
            shared_credentials_dir=credentials_manager.base_path,
        )

        assert get_embedder_api_key(runtime_paths, explicit_api_key="explicit-key") == "explicit-key"

    def test_get_embedder_api_key_embedder_service_beats_openai(
        self,
        credentials_manager: CredentialsManager,
    ) -> None:
        """The dedicated embedder credential must beat the shared openai key."""
        credentials_manager.save_credentials(_EMBEDDER_CREDENTIAL_SERVICE, {"api_key": "embedder-key"})
        credentials_manager.save_credentials("openai", {"api_key": "openai-key"})
        runtime_paths = _runtime_paths(
            credentials_manager.storage_root,
            shared_credentials_dir=credentials_manager.base_path,
        )

        assert get_embedder_api_key(runtime_paths) == "embedder-key"

    def test_get_embedder_api_key_configured_service_is_a_strict_binding(
        self,
        credentials_manager: CredentialsManager,
    ) -> None:
        """A named service wins over legacy services and never leaks into their fallback chain."""
        credentials_manager.save_credentials("embedding-production", {"api_key": "named-key"})
        credentials_manager.save_credentials(_EMBEDDER_CREDENTIAL_SERVICE, {"api_key": "embedder-key"})
        credentials_manager.save_credentials("openai", {"api_key": "openai-key"})
        runtime_paths = _runtime_paths(
            credentials_manager.storage_root,
            shared_credentials_dir=credentials_manager.base_path,
        )

        assert get_embedder_api_key(runtime_paths, credentials_service="embedding-production") == "named-key"

        credentials_manager.delete_credentials("embedding-production")
        assert (
            get_embedder_api_key(runtime_paths, credentials_service="embedding-production")
            == _EMBEDDER_KEYLESS_PLACEHOLDER_API_KEY
        )

    def test_get_embedder_api_key_explicit_key_wins_over_configured_service(
        self,
        credentials_manager: CredentialsManager,
    ) -> None:
        """Inline config remains the highest-priority backwards-compatible override."""
        credentials_manager.save_credentials("embedding-production", {"api_key": "named-key"})
        runtime_paths = _runtime_paths(
            credentials_manager.storage_root,
            shared_credentials_dir=credentials_manager.base_path,
        )

        assert (
            get_embedder_api_key(
                runtime_paths,
                explicit_api_key="inline-key",
                credentials_service="embedding-production",
            )
            == "inline-key"
        )

    def test_get_embedder_api_key_falls_back_to_openai(self, credentials_manager: CredentialsManager) -> None:
        """Without a dedicated credential the shared openai key keeps working."""
        credentials_manager.save_credentials("openai", {"api_key": "openai-key"})
        runtime_paths = _runtime_paths(
            credentials_manager.storage_root,
            shared_credentials_dir=credentials_manager.base_path,
        )

        assert get_embedder_api_key(runtime_paths) == "openai-key"

    def test_get_embedder_api_key_accepts_env_var_named_provider_services(
        self,
        credentials_manager: CredentialsManager,
    ) -> None:
        """The openai fallback and a provider credentials_service both accept env-var-named services."""
        credentials_manager.save_credentials("OPENAI_API_KEY", {"api_key": "env-named-openai-key"})
        credentials_manager.save_credentials("OPENROUTER_API_KEY", {"api_key": "env-named-openrouter-key"})
        runtime_paths = _runtime_paths(
            credentials_manager.storage_root,
            shared_credentials_dir=credentials_manager.base_path,
        )

        assert get_embedder_api_key(runtime_paths) == "env-named-openai-key"
        assert get_embedder_api_key(runtime_paths, credentials_service="openrouter") == "env-named-openrouter-key"

        credentials_manager.save_credentials("openrouter", {"api_key": "canonical-openrouter-key"})
        assert get_embedder_api_key(runtime_paths, credentials_service="openrouter") == "canonical-openrouter-key"

    def test_get_embedder_api_key_returns_placeholder_when_nothing_configured(
        self,
        credentials_manager: CredentialsManager,
    ) -> None:
        """Keyless mode resolves the placeholder so client construction never crashes."""
        runtime_paths = _runtime_paths(
            credentials_manager.storage_root,
            shared_credentials_dir=credentials_manager.base_path,
        )

        assert get_embedder_api_key(runtime_paths) == _EMBEDDER_KEYLESS_PLACEHOLDER_API_KEY

    def test_get_embedder_api_key_strips_resolved_values(
        self,
        credentials_manager: CredentialsManager,
    ) -> None:
        """Keys with stray whitespace must be stripped before reaching the Authorization header."""
        runtime_paths = _runtime_paths(
            credentials_manager.storage_root,
            shared_credentials_dir=credentials_manager.base_path,
        )

        assert get_embedder_api_key(runtime_paths, explicit_api_key="explicit-key\n") == "explicit-key"

        credentials_manager.save_credentials(_EMBEDDER_CREDENTIAL_SERVICE, {"api_key": " embedder-key "})
        assert get_embedder_api_key(runtime_paths) == "embedder-key"

    def test_get_embedder_api_key_ignores_non_string_stored_credential(
        self,
        credentials_manager: CredentialsManager,
    ) -> None:
        """A malformed stored value resolves as absent instead of crashing resolution."""
        credentials_manager.save_credentials(_EMBEDDER_CREDENTIAL_SERVICE, {"api_key": 42})
        credentials_manager.save_credentials("openai", {"api_key": "openai-key"})
        runtime_paths = _runtime_paths(
            credentials_manager.storage_root,
            shared_credentials_dir=credentials_manager.base_path,
        )

        assert get_embedder_api_key(runtime_paths) == "openai-key"

    def test_get_embedder_api_key_treats_blank_values_as_absent(
        self,
        credentials_manager: CredentialsManager,
    ) -> None:
        """Blank explicit and blank embedder-service values must not shadow real keys."""
        credentials_manager.save_credentials(_EMBEDDER_CREDENTIAL_SERVICE, {"api_key": "   "})
        credentials_manager.save_credentials("openai", {"api_key": "openai-key"})
        runtime_paths = _runtime_paths(
            credentials_manager.storage_root,
            shared_credentials_dir=credentials_manager.base_path,
        )

        assert get_embedder_api_key(runtime_paths, explicit_api_key="  ") == "openai-key"

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

    def test_credential_import_notice_on_first_import(
        self,
        temp_credentials_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """First credential import should emit a user-visible notice."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-new-key")

        runtime_paths = _runtime_paths(
            temp_credentials_dir.parent,
            shared_credentials_dir=temp_credentials_dir,
        )

        with capture_logs() as events:
            sync_env_to_credentials(runtime_paths=runtime_paths)

        # Check that notice was logged for OPENAI_API_KEY
        openai_notice_events = [
            e
            for e in events
            if e.get("log_level") == "info" and e.get("service") == "openai" and "Credential" in e.get("event", "")
        ]
        assert len(openai_notice_events) == 1

        notice_text = openai_notice_events[0]["event"]
        assert "OPENAI_API_KEY" in notice_text
        assert "imported" in notice_text
        assert "openai" in notice_text
        assert "DELETE /api/credentials/openai" in notice_text
        # Value should NEVER appear in the notice
        assert "sk-new-key" not in notice_text

    def test_credential_import_notice_on_value_change(
        self,
        temp_credentials_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Changing credential value should emit a notice."""
        # Seed initial env-sourced credential
        cm = CredentialsManager(base_path=temp_credentials_dir)
        cm.save_credentials("openai", {"api_key": "sk-old-key", "_source": "env"})

        monkeypatch.setenv("OPENAI_API_KEY", "sk-changed-key")

        runtime_paths = _runtime_paths(
            temp_credentials_dir.parent,
            shared_credentials_dir=temp_credentials_dir,
        )

        with capture_logs() as events:
            sync_env_to_credentials(runtime_paths=runtime_paths)

        # Check that notice was logged for OPENAI_API_KEY
        openai_notice_events = [
            e
            for e in events
            if e.get("log_level") == "info" and e.get("service") == "openai" and "Credential" in e.get("event", "")
        ]
        assert len(openai_notice_events) == 1

        notice_text = openai_notice_events[0]["event"]
        assert "OPENAI_API_KEY" in notice_text
        assert "updated" in notice_text
        assert "openai" in notice_text
        # Neither old nor new value should appear
        assert "sk-old-key" not in notice_text
        assert "sk-changed-key" not in notice_text

    def test_credential_import_no_notice_when_unchanged(
        self,
        temp_credentials_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Unchanged credential value should NOT emit a notice or save."""
        # Seed initial env-sourced credential
        cm = CredentialsManager(base_path=temp_credentials_dir)
        original_creds = {"api_key": "sk-same-key", "_source": "env"}
        cm.save_credentials("openai", original_creds)

        monkeypatch.setenv("OPENAI_API_KEY", "sk-same-key")

        runtime_paths = _runtime_paths(
            temp_credentials_dir.parent,
            shared_credentials_dir=temp_credentials_dir,
        )

        # Patch save_credentials to verify it's never called for openai
        original_save = cm.save_credentials
        save_calls = []

        def tracked_save(service: str, credentials: dict) -> None:
            save_calls.append(service)
            original_save(service, credentials)

        monkeypatch.setattr(cm, "save_credentials", tracked_save)

        with capture_logs() as events:
            sync_env_to_credentials(runtime_paths=runtime_paths)

        # Check that NO notice was logged for OPENAI_API_KEY
        openai_notice_events = [
            e
            for e in events
            if e.get("log_level") == "info" and e.get("service") == "openai" and "Credential" in e.get("event", "")
        ]
        assert len(openai_notice_events) == 0

        # Should log unchanged at debug level instead
        openai_unchanged_events = [
            e for e in events if e["event"] == "credential_env_sync_unchanged" and e["service"] == "openai"
        ]
        assert len(openai_unchanged_events) == 1

        # Verify save_credentials was never called for openai
        assert "openai" not in save_calls

        # Verify credential is still there and unchanged
        assert cm.get_api_key("openai") == "sk-same-key"

    def test_credential_import_notice_values_never_logged(
        self,
        temp_credentials_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Credential values should NEVER appear in any log output."""
        secret_value = "sk-super-secret-value-12345"  # noqa: S105
        monkeypatch.setenv("ANTHROPIC_API_KEY", secret_value)

        runtime_paths = _runtime_paths(
            temp_credentials_dir.parent,
            shared_credentials_dir=temp_credentials_dir,
        )

        with capture_logs() as events:
            sync_env_to_credentials(runtime_paths=runtime_paths)

        # Check ALL log events - the secret value should not appear anywhere
        all_log_text = json.dumps(events)
        assert secret_value not in all_log_text

    def test_credential_import_notice_distinguishes_env_source(
        self,
        tmp_path: Path,
    ) -> None:
        """Notice should distinguish process env vs .env file as source."""
        config_dir = tmp_path / "cfg"
        config_dir.mkdir(parents=True, exist_ok=True)
        credentials_dir = tmp_path / "credentials"
        credentials_dir.mkdir()

        config_path = config_dir / "config.yaml"
        config_path.write_text("agents: {}\nmodels: {}\nrouter:\n  model: default\n", encoding="utf-8")

        # Test 1: Process environment only
        runtime_paths1 = constants_mod.resolve_runtime_paths(
            config_path=config_path,
            storage_path=tmp_path,
            process_env={
                "OPENAI_API_KEY": "sk-process-env",
                f"{SHARED_CREDENTIALS_PATH_ENV}": str(credentials_dir),
            },
        )

        with capture_logs() as events1:
            sync_env_to_credentials(runtime_paths=runtime_paths1)

        openai_notice1 = next(
            e
            for e in events1
            if e.get("log_level") == "info" and e.get("service") == "openai" and "Credential" in e.get("event", "")
        )
        notice1 = openai_notice1["event"]
        assert "process environment" in notice1

        # Clean up for test 2
        (credentials_dir / "openai_credentials.json").unlink()

        # Test 2: .env file only
        (config_dir / ".env").write_text(
            f"OPENAI_API_KEY=sk-env-file\n{SHARED_CREDENTIALS_PATH_ENV}={credentials_dir}\n",
            encoding="utf-8",
        )
        runtime_paths2 = constants_mod.resolve_runtime_paths(
            config_path=config_path,
            storage_path=tmp_path,
            process_env={},
        )

        with capture_logs() as events2:
            sync_env_to_credentials(runtime_paths=runtime_paths2)

        openai_notice2 = next(
            e
            for e in events2
            if e.get("log_level") == "info" and e.get("service") == "openai" and "Credential" in e.get("event", "")
        )
        notice2 = openai_notice2["event"]
        assert ".env file" in notice2

        # Clean up for test 3
        (credentials_dir / "openai_credentials.json").unlink()

        # Test 3: Both (process env wins)
        runtime_paths3 = constants_mod.resolve_runtime_paths(
            config_path=config_path,
            storage_path=tmp_path,
            process_env={
                "OPENAI_API_KEY": "sk-process-env",
            },
        )

        with capture_logs() as events3:
            sync_env_to_credentials(runtime_paths=runtime_paths3)

        openai_notice3 = next(
            e
            for e in events3
            if e.get("log_level") == "info" and e.get("service") == "openai" and "Credential" in e.get("event", "")
        )
        notice3 = openai_notice3["event"]
        # Should mention process env and that .env is overridden
        assert "process environment" in notice3
        assert "overridden" in notice3
        assert "remove OPENAI_API_KEY from both" in notice3

    def test_credential_import_notice_names_file_var_correctly(
        self,
        temp_credentials_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Notice should name GITHUB_TOKEN_FILE when that var provides the value, not GITHUB_TOKEN."""
        token_file = temp_credentials_dir.parent / "github-token"
        token_file.write_text("ghp-file-token\n", encoding="utf-8")

        # Set only the _FILE var, not GITHUB_TOKEN
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.setenv("GITHUB_TOKEN_FILE", str(token_file))

        runtime_paths = _runtime_paths(
            temp_credentials_dir.parent,
            shared_credentials_dir=temp_credentials_dir,
        )

        with capture_logs() as events:
            sync_env_to_credentials(runtime_paths=runtime_paths)

        # Find the github_private notice
        github_notice = next(
            e
            for e in events
            if e.get("log_level") == "info"
            and e.get("service") == "github_private"
            and "Credential" in e.get("event", "")
        )

        # Should name GITHUB_TOKEN_FILE, not GITHUB_TOKEN
        assert github_notice["env_var"] == "GITHUB_TOKEN_FILE"
        notice_text = github_notice["event"]
        assert "GITHUB_TOKEN_FILE" in notice_text
        assert "remove GITHUB_TOKEN_FILE" in notice_text

    def test_credential_import_notice_for_adc_path_change(
        self,
        temp_credentials_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """ADC path change should emit a notice."""
        # Seed initial ADC credential
        cm = CredentialsManager(base_path=temp_credentials_dir)
        cm.save_credentials(
            "google_vertex_adc",
            {"application_credentials_path": "/old/path.json", "_source": "env"},
        )

        # Change the path
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/new/path.json")

        runtime_paths = _runtime_paths(
            temp_credentials_dir.parent,
            shared_credentials_dir=temp_credentials_dir,
        )

        with capture_logs() as events:
            sync_env_to_credentials(runtime_paths=runtime_paths)

        # Should emit notice for the changed value
        adc_notice = next(
            e
            for e in events
            if e.get("log_level") == "info"
            and e.get("service") == "google_vertex_adc"
            and "Credential" in e.get("event", "")
        )
        assert adc_notice["env_var"] == "GOOGLE_APPLICATION_CREDENTIALS"
        notice_text = adc_notice["event"]
        assert "updated" in notice_text
        assert "GOOGLE_APPLICATION_CREDENTIALS" in notice_text

    def test_credential_import_notice_for_declared_seeds(
        self,
        temp_credentials_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Declared credential seeds should emit notices naming the seed env var and service."""
        monkeypatch.setenv("OAUTH_CLIENT_ID", "client-id")
        monkeypatch.setenv("OAUTH_CLIENT_SECRET", "client-secret")
        monkeypatch.setenv("MINDROOM_CREDENTIAL_SEEDS_JSON", _credential_seed_json())

        runtime_paths = _runtime_paths(
            temp_credentials_dir.parent,
            shared_credentials_dir=temp_credentials_dir,
        )

        with capture_logs() as events:
            sync_env_to_credentials(runtime_paths=runtime_paths)

        # Should emit notice for the declared seed
        seed_notice = next(
            e
            for e in events
            if e.get("log_level") == "info"
            and e.get("service") == "google_oauth_client"
            and "Credential" in e.get("event", "")
        )
        # Should name the seed declaration env var
        assert seed_notice["env_var"] == "MINDROOM_CREDENTIAL_SEEDS_JSON"
        notice_text = seed_notice["event"]
        assert "google_oauth_client" in notice_text
        assert "MINDROOM_CREDENTIAL_SEEDS_JSON" in notice_text
