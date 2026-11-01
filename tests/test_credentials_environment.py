"""Credentials the environment declares: a read-time layer under the primary's stored credentials."""

import base64
import json
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest

from mindroom import constants as constants_mod
from mindroom.api import sandbox_runner as sandbox_runner_module
from mindroom.credentials.environment import get_secret_from_env
from mindroom.credentials.manager import (
    CredentialsManager,
    _reset_credentials_manager_cache,
    get_runtime_credentials_manager,
    runtime_credentials_manager_key,
    sync_shared_credentials_to_worker,
)
from mindroom.credentials.sync import get_api_key_for_provider, get_embedder_api_key
from mindroom.providers.credentials import missing_model_api_key_provider
from mindroom.runtime_env_policy import (
    CREDENTIALS_ENCRYPTION_KEY_ENV,
    SANDBOX_RUNTIME_ENV_BY_KEY,
    SANDBOX_STARTUP_MANIFEST_PATH_ENV,
)
from tests.conftest import load_config_yaml

_RUNNER_MODE = SANDBOX_RUNTIME_ENV_BY_KEY["runner_mode"]
_WORKER_KEY = "v1:test-tenant:shared:alpha"


@pytest.fixture(autouse=True)
def _fresh_credentials_managers() -> Iterator[None]:
    _reset_credentials_manager_cache()
    yield
    _reset_credentials_manager_cache()


def _runtime_paths(tmp_path: Path, env: Mapping[str, str]) -> constants_mod.RuntimePaths:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}\nmodels: {}\nrouter:\n  model: default\n", encoding="utf-8")
    return constants_mod.resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "storage",
        process_env=dict(env),
    )


def _restart(tmp_path: Path, env: Mapping[str, str]) -> constants_mod.RuntimePaths:
    """Return the runtime of a process started anew with ``env``."""
    _reset_credentials_manager_cache()
    return _runtime_paths(tmp_path, env)


def test_an_environment_key_resolves_without_being_stored(tmp_path: Path) -> None:
    """A provider key in the environment reaches the runtime, and is never written to the credential store."""
    runtime_paths = _runtime_paths(tmp_path, {"OPENAI_API_KEY": "sk-env"})

    assert get_api_key_for_provider("openai", runtime_paths) == "sk-env"
    assert not (get_runtime_credentials_manager(runtime_paths).base_path / "openai_credentials.json").exists()


def test_a_key_removed_from_the_environment_stops_resolving_after_restart(tmp_path: Path) -> None:
    """A copy an earlier release imported from the environment stops working once its variable is gone."""
    # What an earlier release's startup import left behind for OPENAI_API_KEY and GITHUB_TOKEN.
    store = CredentialsManager(tmp_path / "storage" / "credentials")
    store.save_credentials("openai", {"api_key": "sk-old", "_source": "env"})
    store.save_credentials("github_private", {"username": "x-access-token", "token": "ghp-old", "_source": "env"})
    store.save_credentials("anthropic", {"api_key": "sk-dashboard", "_source": "ui"})

    runtime_paths = _restart(tmp_path, {})

    assert get_api_key_for_provider("openai", runtime_paths) is None
    assert get_runtime_credentials_manager(runtime_paths).load_credentials("github_private") is None
    assert get_api_key_for_provider("anthropic", runtime_paths) == "sk-dashboard"


def test_a_granted_worker_loses_a_key_removed_from_the_environment(tmp_path: Path) -> None:
    """The worker mirror follows the environment: a removed variable leaves the granted worker at its next sync."""
    granted = frozenset({"github_private"})
    runtime_paths = _runtime_paths(tmp_path, {"GITHUB_TOKEN": "ghp-env"})
    manager = get_runtime_credentials_manager(runtime_paths)
    sync_shared_credentials_to_worker(_WORKER_KEY, allowed_services=granted, credentials_manager=manager)
    mirror = manager.for_worker(_WORKER_KEY).shared_manager()
    assert (mirror.load_credentials("github_private") or {}).get("token") == "ghp-env"

    runtime_paths = _restart(tmp_path, {})
    manager = get_runtime_credentials_manager(runtime_paths)
    sync_shared_credentials_to_worker(_WORKER_KEY, allowed_services=granted, credentials_manager=manager)

    assert manager.for_worker(_WORKER_KEY).shared_manager().load_credentials("github_private") is None


def _encryption_key(seed: bytes) -> str:
    return base64.urlsafe_b64encode(seed * 32).decode("ascii")


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


_OAUTH_CLIENT_ENV = {"OAUTH_CLIENT_ID": "client-id", "OAUTH_CLIENT_SECRET": "client-secret"}
_DECLARED_OAUTH_CLIENT = {"client_id": "client-id", "client_secret": "client-secret"}


# Which variables declare which services.


@pytest.mark.parametrize(
    ("variable", "service"),
    [
        ("OPENAI_API_KEY", "openai"),
        ("ANTHROPIC_API_KEY", "anthropic"),
        ("AZURE_OPENAI_API_KEY", "azure"),
        ("GOOGLE_API_KEY", "google"),
        ("OPENROUTER_API_KEY", "openrouter"),
        ("DEEPSEEK_API_KEY", "deepseek"),
        ("CEREBRAS_API_KEY", "cerebras"),
        ("GROQ_API_KEY", "groq"),
        ("ZAI_API_KEY", "zai"),
        ("OLLAMA_HOST", "ollama"),
    ],
)
def test_each_provider_variable_declares_its_service(tmp_path: Path, variable: str, service: str) -> None:
    """A provider variable supplies its service's credential, read from the environment without a saved file."""
    manager = get_runtime_credentials_manager(_runtime_paths(tmp_path, {variable: "value-from-env"}))

    field = "host" if service == "ollama" else "api_key"
    assert manager.load_credentials(service) == {field: "value-from-env"}
    assert manager.declared_variable(service) == variable
    assert not manager.get_credentials_path(service).exists()


@pytest.mark.parametrize(
    ("env_file_text", "exported"),
    [
        ("OPENAI_API_KEY=your-openai-key-here\n", None),
        ("OPENAI_API_KEY=your-openai-key-here # replace with your key\n", None),
        ("", "your-openai-key-here"),
    ],
    ids=["env-file", "env-file-inline-comment", "exported"],
)
def test_a_template_placeholder_declares_no_key(tmp_path: Path, env_file_text: str, exported: str | None) -> None:
    """A starter-template placeholder never supplies a key, so the dashboard still reports the provider."""
    (tmp_path / ".env").write_text(env_file_text, encoding="utf-8")
    runtime_paths = _runtime_paths(tmp_path, {} if exported is None else {"OPENAI_API_KEY": exported})
    assert get_secret_from_env("OPENAI_API_KEY", runtime_paths) is None

    assert get_api_key_for_provider("openai", runtime_paths) is None
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "agents: {}\nmodels:\n  default:\n    provider: openai\n    id: gpt-6-astra\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    assert missing_model_api_key_provider(load_config_yaml(config_path), runtime_paths, "default") == "openai"


@pytest.mark.parametrize(
    ("file_text", "expected"),
    [("sk-from-file\n", "sk-from-file"), ("your-openai-key-here\n", None), ("  \n", None)],
)
def test_unset_name_falls_through_to_name_file_with_the_same_rule(
    tmp_path: Path,
    file_text: str,
    expected: str | None,
) -> None:
    """A placeholder in NAME does not block NAME_FILE, whose contents follow the same unset rule."""
    secret_file = tmp_path / "openai-key"
    secret_file.write_text(file_text, encoding="utf-8")
    runtime_paths = _runtime_paths(
        tmp_path,
        {"OPENAI_API_KEY": "your-openai-key-here", "OPENAI_API_KEY_FILE": str(secret_file)},
    )

    assert get_secret_from_env("OPENAI_API_KEY", runtime_paths) == expected


@pytest.mark.parametrize("exported", ["", "your-openai-key-here"], ids=["blank", "placeholder"])
def test_an_unset_exported_key_does_not_hide_the_env_file_key(tmp_path: Path, exported: str) -> None:
    """An unset exported key falls through to the `.env` key, which wins over a `_FILE` variant, as for tools."""
    secret_file = tmp_path / "openai-key"
    secret_file.write_text("sk-from-file\n", encoding="utf-8")
    (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-env-file\n", encoding="utf-8")
    runtime_paths = _runtime_paths(tmp_path, {"OPENAI_API_KEY": exported, "OPENAI_API_KEY_FILE": str(secret_file)})

    assert get_api_key_for_provider("openai", runtime_paths) == "sk-env-file"
    assert get_runtime_credentials_manager(runtime_paths).declared_variable("openai") == "OPENAI_API_KEY"


def test_get_secret_from_env_resolves_relative_file_paths_from_config_dir(tmp_path: Path) -> None:
    """Relative *_FILE secret paths in the runtime `.env` should anchor to the config directory."""
    secret_file = tmp_path / "secrets" / "openai.key"
    secret_file.parent.mkdir()
    secret_file.write_text("sk-relative", encoding="utf-8")
    (tmp_path / ".env").write_text("OPENAI_API_KEY_FILE=secrets/openai.key\n", encoding="utf-8")

    assert get_secret_from_env("OPENAI_API_KEY", _runtime_paths(tmp_path, {})) == "sk-relative"


def test_a_secret_file_saved_with_a_byte_order_mark_reads_as_its_value(tmp_path: Path) -> None:
    """A `<NAME>_FILE` saved as UTF-8 with a BOM supplies the key without the BOM, as the `.env` beside it reads."""
    secret_file = tmp_path / "openai.key"
    secret_file.write_text("sk-bom\n", encoding="utf-8-sig")

    runtime_paths = _runtime_paths(tmp_path, {"OPENAI_API_KEY_FILE": str(secret_file)})

    assert get_secret_from_env("OPENAI_API_KEY", runtime_paths) == "sk-bom"


def test_a_blank_exported_file_variable_does_not_hide_the_env_file_one(tmp_path: Path) -> None:
    """A blank exported `<NAME>_FILE` counts as unset, so the `.env` one still names the secret file."""
    (tmp_path / "openai.key").write_text("sk-from-file\n", encoding="utf-8")
    (tmp_path / ".env").write_text("OPENAI_API_KEY_FILE=openai.key\n", encoding="utf-8")

    assert (
        get_secret_from_env("OPENAI_API_KEY", _runtime_paths(tmp_path, {"OPENAI_API_KEY_FILE": ""})) == "sk-from-file"
    )


def test_a_valid_name_ignores_an_unresolvable_file_variable(tmp_path: Path) -> None:
    """A non-empty NAME supplies the key without resolving a broken NAME_FILE path."""
    runtime_paths = _runtime_paths(
        tmp_path,
        {"OPENAI_API_KEY": "sk-direct", "OPENAI_API_KEY_FILE": "~mindroom-nonexistent-user/key"},
    )

    assert get_runtime_credentials_manager(runtime_paths).get_api_key("openai") == "sk-direct"


@pytest.mark.parametrize("content", [None, b"\xff\xfe not text"], ids=["directory", "not-utf8"])
def test_an_unreadable_secret_file_supplies_no_key(tmp_path: Path, content: bytes | None) -> None:
    """A NAME_FILE that cannot be read as text supplies no key instead of failing the start."""
    secret_file = tmp_path / "openai.key"
    if content is None:
        secret_file.mkdir()
    else:
        secret_file.write_bytes(content)

    assert (
        get_secret_from_env("OPENAI_API_KEY", _runtime_paths(tmp_path, {"OPENAI_API_KEY_FILE": str(secret_file)}))
        is None
    )


def test_an_empty_variable_declares_nothing(tmp_path: Path) -> None:
    """Blank values declare no credential, and only the variables that are set do."""
    runtime_paths = _runtime_paths(
        tmp_path,
        {"OPENAI_API_KEY": "valid-key", "ANTHROPIC_API_KEY": "", "GITHUB_TOKEN": "", "EMBEDDER_API_KEY": "  "},
    )

    assert get_runtime_credentials_manager(runtime_paths).list_services() == ["openai"]


def test_a_blank_secret_file_declares_nothing(tmp_path: Path) -> None:
    """A NAME_FILE whose contents are blank supplies no key."""
    secret_file = tmp_path / "openai-key"
    secret_file.write_text("  \n\t\n", encoding="utf-8")
    runtime_paths = _runtime_paths(tmp_path, {"OPENAI_API_KEY_FILE": str(secret_file)})

    assert get_runtime_credentials_manager(runtime_paths).load_credentials("openai") is None


@pytest.mark.parametrize(
    ("name", "service"),
    [
        ("OPENAI_API_KEY", "openai"),
        ("GITHUB_TOKEN", "github_private"),
        ("EMBEDDER_API_KEY", "embedder"),
    ],
)
def test_an_empty_name_falls_back_to_its_file_variable(tmp_path: Path, name: str, service: str) -> None:
    """An empty NAME falls back to NAME_FILE, which then supplies the credential under its own name."""
    secret_file = tmp_path / "secret"
    secret_file.write_text("secret-from-file\n", encoding="utf-8")
    (tmp_path / ".env").write_text(f"{name}_FILE={secret_file}\n", encoding="utf-8")
    manager = get_runtime_credentials_manager(_runtime_paths(tmp_path, {name: ""}))

    credentials = manager.load_credentials(service)
    assert credentials is not None
    assert "secret-from-file" in credentials.values()
    assert manager.declared_variable(service) == f"{name}_FILE"


def test_github_token_declares_the_private_git_credential(tmp_path: Path) -> None:
    """GITHUB_TOKEN supplies the github_private credential that private Git knowledge sources use."""
    manager = get_runtime_credentials_manager(_runtime_paths(tmp_path, {"GITHUB_TOKEN": "ghp-test-token"}))

    assert manager.load_credentials("github_private") == {
        "username": "x-access-token",
        "token": "ghp-test-token",
    }


@pytest.mark.parametrize("from_file", [False, True], ids=["variable", "file-variable"])
def test_embedder_api_key_declares_the_embedder_credential(tmp_path: Path, from_file: bool) -> None:
    """EMBEDDER_API_KEY, or its NAME_FILE form, supplies the dedicated embedder credential."""
    key_file = tmp_path / "embedder.key"
    key_file.write_text("sk-embedder-key\n", encoding="utf-8")
    env = {"EMBEDDER_API_KEY_FILE": str(key_file)} if from_file else {"EMBEDDER_API_KEY": "sk-embedder-key"}
    manager = get_runtime_credentials_manager(_runtime_paths(tmp_path, env))

    assert manager.load_credentials("embedder") == {"api_key": "sk-embedder-key"}


@pytest.mark.parametrize("from_env_file", [False, True], ids=["exported", "env-file-under-blank-export"])
def test_google_application_credentials_declares_the_vertex_adc_path(tmp_path: Path, from_env_file: bool) -> None:
    """GOOGLE_APPLICATION_CREDENTIALS supplies the path of the Vertex AI application default credentials.

    A blank exported value, as a compose passthrough of an unset variable gives, does not hide the `.env` one.
    """
    adc_file = tmp_path / "adc.json"
    env = {"GOOGLE_APPLICATION_CREDENTIALS": "" if from_env_file else str(adc_file)}
    if from_env_file:
        (tmp_path / ".env").write_text(f"GOOGLE_APPLICATION_CREDENTIALS={adc_file}\n", encoding="utf-8")
    manager = get_runtime_credentials_manager(_runtime_paths(tmp_path, env))

    assert manager.load_credentials("google_vertex_adc") == {
        "application_credentials_path": str(adc_file.resolve()),
    }
    assert manager.declared_variable("google_vertex_adc") == "GOOGLE_APPLICATION_CREDENTIALS"


def test_legacy_google_client_variables_declare_no_oauth_client(tmp_path: Path) -> None:
    """GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET do not supply the Google OAuth client config."""
    runtime_paths = _runtime_paths(tmp_path, {"GOOGLE_CLIENT_ID": "client-id", "GOOGLE_CLIENT_SECRET": "client-secret"})

    assert get_runtime_credentials_manager(runtime_paths).load_credentials("google_oauth_client") is None


# Credential seeds.


def test_a_seed_declares_its_service_from_variables(tmp_path: Path) -> None:
    """A credential seed supplies a named service from the variables its fields name."""
    runtime_paths = _runtime_paths(
        tmp_path,
        {**_OAUTH_CLIENT_ENV, "MINDROOM_CREDENTIAL_SEEDS_JSON": _credential_seed_json()},
    )

    assert get_runtime_credentials_manager(runtime_paths).load_credentials("google_oauth_client") == (
        _DECLARED_OAUTH_CLIENT
    )


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig"], ids=["plain", "byte-order-mark"])
def test_a_seed_file_relative_to_the_config_directory_declares_its_service(tmp_path: Path, encoding: str) -> None:
    """Credential seeds can live in a JSON file named relative to the config directory, saved with or without a BOM."""
    (tmp_path / "credential-seeds.json").write_text(
        _credential_seed_json(service="example_oauth_client"),
        encoding=encoding,
    )
    (tmp_path / ".env").write_text("MINDROOM_CREDENTIAL_SEEDS_FILE=credential-seeds.json\n", encoding="utf-8")
    runtime_paths = _runtime_paths(tmp_path, _OAUTH_CLIENT_ENV)

    assert get_runtime_credentials_manager(runtime_paths).load_credentials("example_oauth_client") == (
        _DECLARED_OAUTH_CLIENT
    )


def test_a_seed_field_reads_a_file_variable(tmp_path: Path) -> None:
    """A seed field's variable follows the NAME_FILE secret convention."""
    secret_file = tmp_path / "oauth-client-secret"
    secret_file.write_text("client-secret\n", encoding="utf-8")
    runtime_paths = _runtime_paths(
        tmp_path,
        {
            "OAUTH_CLIENT_ID": "client-id",
            "OAUTH_CLIENT_SECRET_FILE": str(secret_file),
            "MINDROOM_CREDENTIAL_SEEDS_JSON": _credential_seed_json(),
        },
    )

    assert get_runtime_credentials_manager(runtime_paths).load_credentials("google_oauth_client") == (
        _DECLARED_OAUTH_CLIENT
    )


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig"], ids=["plain", "byte-order-mark"])
def test_a_seed_field_reads_literal_and_file_values(tmp_path: Path, encoding: str) -> None:
    """Seed fields can hold literal values and direct file references, read without a byte order mark."""
    secret_file = tmp_path / "oauth-client-secret"
    secret_file.write_text("client-secret\n", encoding=encoding)
    seed = {
        "service": "google_oauth_client",
        "credentials": {"client_id": {"value": "client-id"}, "client_secret": {"file": str(secret_file)}},
    }
    runtime_paths = _runtime_paths(tmp_path, {"MINDROOM_CREDENTIAL_SEEDS_JSON": json.dumps(seed)})

    assert get_runtime_credentials_manager(runtime_paths).load_credentials("google_oauth_client") == (
        _DECLARED_OAUTH_CLIENT
    )


def test_a_seed_cannot_declare_an_oauth_token_service(tmp_path: Path) -> None:
    """OAuth tokens come only from connecting the account, never from a seed."""
    seed = {"service": "google_oauth", "credentials": {"access_token": {"value": "seeded-access-token"}}}
    runtime_paths = _runtime_paths(tmp_path, {"MINDROOM_CREDENTIAL_SEEDS_JSON": json.dumps(seed)})

    assert get_runtime_credentials_manager(runtime_paths).load_credentials("google_oauth") is None


def test_a_seed_with_a_missing_value_declares_nothing(tmp_path: Path) -> None:
    """A seed whose field has no value supplies no partial credential."""
    runtime_paths = _runtime_paths(
        tmp_path,
        {"OAUTH_CLIENT_ID": "client-id", "MINDROOM_CREDENTIAL_SEEDS_JSON": _credential_seed_json()},
    )

    assert get_runtime_credentials_manager(runtime_paths).load_credentials("google_oauth_client") is None


@pytest.mark.parametrize(
    ("env_file", "seeds_json"),
    [
        ("", "{"),
        ("MINDROOM_CREDENTIAL_SEEDS_FILE=credential-seeds.json\n", None),
        ("", json.dumps({"seeds": "not-a-list"})),
    ],
    ids=["malformed-json", "malformed-file", "invalid-shape"],
)
def test_a_malformed_seed_declaration_does_not_block_provider_keys(
    tmp_path: Path,
    env_file: str,
    seeds_json: str | None,
) -> None:
    """A seed declaration that cannot be read leaves the provider variables supplying their keys."""
    (tmp_path / "credential-seeds.json").write_text("{", encoding="utf-8")
    (tmp_path / ".env").write_text(env_file, encoding="utf-8")
    env = {"OPENAI_API_KEY": "sk-test-openai-key"}
    if seeds_json is not None:
        env["MINDROOM_CREDENTIAL_SEEDS_JSON"] = seeds_json

    assert get_runtime_credentials_manager(_runtime_paths(tmp_path, env)).get_api_key("openai") == "sk-test-openai-key"


def test_an_invalid_seed_entry_does_not_block_a_later_valid_one(tmp_path: Path) -> None:
    """One invalid seed does not keep the seeds after it from supplying their services."""
    seeds = [
        {"service": 123, "credentials": {"token": {"value": "bad"}}},
        json.loads(_credential_seed_json())[0],
    ]
    runtime_paths = _runtime_paths(tmp_path, {**_OAUTH_CLIENT_ENV, "MINDROOM_CREDENTIAL_SEEDS_JSON": json.dumps(seeds)})

    assert get_runtime_credentials_manager(runtime_paths).load_credentials("google_oauth_client") == (
        _DECLARED_OAUTH_CLIENT
    )


def test_internal_credential_env_names_stay_out_of_public_and_execution_env_views(tmp_path: Path) -> None:
    """Internal credential env vars must not leak to public manifests or tool execution envs."""
    seed_file = tmp_path / "credential-seeds.json"
    seed_file.write_text(_credential_seed_json(), encoding="utf-8")
    seed_json = json.dumps(
        {
            "service": "example_oauth_client",
            "credentials": {"client_secret": {"value": "literal-secret"}},
        },
    )
    runtime_paths = _runtime_paths(
        tmp_path,
        {
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

    assert isolated_runtime.env_value(CREDENTIALS_ENCRYPTION_KEY_ENV) is None
    for runtime_env in public_and_execution_envs:
        assert CREDENTIALS_ENCRYPTION_KEY_ENV not in runtime_env
        assert "MINDROOM_CREDENTIAL_SEEDS_JSON" not in runtime_env
        assert "MINDROOM_CREDENTIAL_SEEDS_FILE" not in runtime_env
    assert "MINDROOM_CREDENTIAL_SEEDS_JSON" not in isolated_runtime.process_env
    assert "MINDROOM_CREDENTIAL_SEEDS_FILE" not in isolated_runtime.process_env
    assert "MINDROOM_CREDENTIAL_SEEDS_JSON" not in isolated_runtime.env_file_values
    assert "MINDROOM_CREDENTIAL_SEEDS_FILE" not in isolated_runtime.env_file_values


# Who reads the layer.


@pytest.mark.parametrize(
    ("runner_mode", "reads_layer"),
    [(None, True), ("false", True), ("true", False)],
    ids=["primary", "primary-runner-mode-false", "runner"],
)
def test_only_a_primary_reads_the_credentials_its_environment_declares(
    tmp_path: Path,
    runner_mode: str | None,
    reads_layer: bool,
) -> None:
    """A runner never reads the environment as credentials."""
    env = {"OPENAI_API_KEY": "sk-env"}
    if runner_mode is not None:
        env[_RUNNER_MODE] = runner_mode
    runtime_paths = _runtime_paths(tmp_path, env)
    manager = get_runtime_credentials_manager(runtime_paths)

    assert get_api_key_for_provider("openai", runtime_paths) == ("sk-env" if reads_layer else None)
    assert manager.list_services() == (["openai"] if reads_layer else [])


@pytest.mark.parametrize("dedicated", [True, False], ids=["dedicated", "static"])
def test_the_sandbox_runner_commits_runner_mode_so_it_reads_no_declared_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dedicated: bool,
) -> None:
    """Both runner startup paths commit runner mode, so the runner's own variables never become credentials."""
    for name in (
        _RUNNER_MODE,
        SANDBOX_RUNTIME_ENV_BY_KEY["dedicated_worker_key"],
        CREDENTIALS_ENCRYPTION_KEY_ENV,
        "OPENAI_API_KEY",
        "OPENAI_API_KEY_FILE",
    ):
        monkeypatch.delenv(name, raising=False)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-6-astra\nagents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    env = {"OPENAI_API_KEY": "sk-env"}
    if dedicated:
        env[SANDBOX_RUNTIME_ENV_BY_KEY["dedicated_worker_key"]] = "worker-1"
    startup_runtime = constants_mod.resolve_primary_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "storage",
        process_env=env,
    )
    manifest_path = constants_mod.write_startup_manifest(startup_runtime.storage_root, startup_runtime)
    monkeypatch.setenv(SANDBOX_STARTUP_MANIFEST_PATH_ENV, str(manifest_path))

    runner_runtime, _config = sandbox_runner_module.load_config_from_startup_runtime()

    assert get_secret_from_env("OPENAI_API_KEY", runner_runtime) == "sk-env"
    assert runner_runtime.env_flag(_RUNNER_MODE)
    assert runtime_credentials_manager_key(runner_runtime).declaring_env_digest is None
    manager = get_runtime_credentials_manager(runner_runtime)
    assert manager.load_credentials("openai") is None
    assert manager.list_services() == []


def test_only_the_primary_shared_view_reads_declared_credentials(tmp_path: Path) -> None:
    """The primary's shared view is its own store, while worker and scoped stores never inherit the environment."""
    manager = get_runtime_credentials_manager(_runtime_paths(tmp_path, {"OPENAI_API_KEY": "sk-env"}))
    worker_manager = manager.for_worker(_WORKER_KEY)

    assert manager.shared_manager().get_api_key("openai") == "sk-env"
    assert manager.shared_manager().declared_variable("openai") == "OPENAI_API_KEY"
    for other in (
        worker_manager,
        worker_manager.shared_manager(),
        manager.for_primary_runtime_scope("@alice:localhost", "coder"),
        manager.for_primary_runtime_scope("@alice:localhost", None),
        manager.for_primary_runtime_agent_scope("coder"),
    ):
        assert other.load_credentials("openai") is None
        assert other.list_services() == []
        assert other.declared_variable("openai") is None


# How saved documents and the layer combine.


@pytest.mark.parametrize(
    ("env", "service", "saved"),
    [
        ({"OPENAI_API_KEY": "sk-env"}, "openai", {"api_key": "sk-ui"}),
        ({"GITHUB_TOKEN": "ghp-env"}, "github_private", {"username": "my-user", "token": "ghp-ui"}),
        ({"EMBEDDER_API_KEY": "sk-env"}, "embedder", {"api_key": "sk-ui"}),
        (
            {**_OAUTH_CLIENT_ENV, "MINDROOM_CREDENTIAL_SEEDS_JSON": _credential_seed_json()},
            "google_oauth_client",
            {"client_id": "ui-client-id", "client_secret": "ui-secret"},
        ),
    ],
    ids=["provider", "github", "embedder", "seed"],
)
def test_a_dashboard_save_wins_over_the_environment_until_it_is_deleted(
    tmp_path: Path,
    env: dict[str, str],
    service: str,
    saved: dict[str, str],
) -> None:
    """A saved document hides the whole declared one, and deleting it reveals the variable again at once."""
    manager = get_runtime_credentials_manager(_runtime_paths(tmp_path, env))
    declared = manager.load_credentials(service)
    assert declared is not None
    assert "_source" not in declared

    manager.save_credentials(service, {**saved, "_source": "ui"})
    assert manager.load_credentials(service) == {**saved, "_source": "ui"}

    manager.delete_credentials(service)
    assert manager.load_credentials(service) == declared


def test_deleting_a_credential_only_the_environment_declares_changes_nothing(tmp_path: Path) -> None:
    """Only removing the variable stops it supplying its credential."""
    runtime_paths = _runtime_paths(tmp_path, {"OPENAI_API_KEY": "sk-env"})
    manager = get_runtime_credentials_manager(runtime_paths)

    manager.delete_credentials("openai")

    assert get_api_key_for_provider("openai", runtime_paths) == "sk-env"
    assert manager.list_services() == ["openai"]


def test_a_changed_variable_supplies_its_new_value(tmp_path: Path) -> None:
    """A runtime whose environment differs reads its own declarations, so a changed variable applies at the next start."""
    assert get_api_key_for_provider("openai", _runtime_paths(tmp_path, {"OPENAI_API_KEY": "sk-old"})) == "sk-old"

    assert get_api_key_for_provider("openai", _runtime_paths(tmp_path, {"OPENAI_API_KEY": "sk-new"})) == "sk-new"


_OPENAI_SEED = json.dumps({"service": "openai", "credentials": {"api_key": "sk-seed"}})


@pytest.mark.parametrize(
    ("env", "env_file", "variable"),
    [
        ({"OPENAI_API_KEY": "sk-env"}, "", "OPENAI_API_KEY"),
        ({}, "OPENAI_API_KEY_FILE=openai.key\n", "OPENAI_API_KEY_FILE"),
        ({"MINDROOM_CREDENTIAL_SEEDS_JSON": _OPENAI_SEED}, "", "MINDROOM_CREDENTIAL_SEEDS_JSON"),
        ({}, "MINDROOM_CREDENTIAL_SEEDS_FILE=seeds.json\n", "MINDROOM_CREDENTIAL_SEEDS_FILE"),
        (
            {"OPENAI_API_KEY": "sk-env", "MINDROOM_CREDENTIAL_SEEDS_JSON": _OPENAI_SEED},
            "",
            "MINDROOM_CREDENTIAL_SEEDS_JSON",
        ),
        (
            {"MINDROOM_CREDENTIAL_SEEDS_JSON": _OPENAI_SEED},
            "MINDROOM_CREDENTIAL_SEEDS_FILE=seeds.json\n",
            "MINDROOM_CREDENTIAL_SEEDS_JSON",
        ),
    ],
    ids=["variable", "file-variable", "inline-seed", "seed-file", "seed-over-variable", "inline-seed-over-seed-file"],
)
def test_declared_variable_names_the_variable_that_supplies_a_credential(
    tmp_path: Path,
    env: dict[str, str],
    env_file: str,
    variable: str,
) -> None:
    """A seed replaces the provider's variable, and an inline seed replaces a seed file's entry for the service."""
    (tmp_path / "openai.key").write_text("sk-file\n", encoding="utf-8")
    (tmp_path / "seeds.json").write_text(_OPENAI_SEED, encoding="utf-8")
    (tmp_path / ".env").write_text(env_file, encoding="utf-8")

    assert get_runtime_credentials_manager(_runtime_paths(tmp_path, env)).declared_variable("openai") == variable


def test_a_saved_key_is_never_attributed_to_the_variable_holding_the_same_key(tmp_path: Path) -> None:
    """A key saved in the dashboard names no variable, whatever value the provider's variable has."""
    manager = get_runtime_credentials_manager(_runtime_paths(tmp_path, {"OPENAI_API_KEY": "sk-same"}))
    manager.save_credentials("openai", {"api_key": "sk-same", "_source": "ui"})

    assert manager.declared_variable("openai") is None
    assert manager.declared_variable("anthropic") is None


def test_the_environment_layer_is_never_written_to_disk(tmp_path: Path) -> None:
    """Reading, listing, and granting declared credentials writes no file but the granted worker's own mirror."""
    runtime_paths = _runtime_paths(
        tmp_path,
        {"OPENAI_API_KEY": "sk-env", "GITHUB_TOKEN": "ghp-env", "EMBEDDER_API_KEY": "sk-embedder"},
    )
    manager = get_runtime_credentials_manager(runtime_paths)

    assert get_api_key_for_provider("openai", runtime_paths) == "sk-env"
    assert get_embedder_api_key(runtime_paths) == "sk-embedder"
    assert manager.list_services() == ["embedder", "github_private", "openai"]
    assert manager.declared_variable("github_private") == "GITHUB_TOKEN"
    sync_shared_credentials_to_worker(
        _WORKER_KEY,
        allowed_services=frozenset({"github_private"}),
        credentials_manager=manager,
    )

    mirror = manager.for_worker(_WORKER_KEY).shared_manager()
    storage_root = runtime_paths.storage_root
    assert sorted(storage_root.rglob("*_credentials.json")) == [mirror.get_credentials_path("github_private")]


# The one-time removal of the copies earlier releases imported.


def test_an_unreadable_copy_is_kept_until_an_open_can_read_it(tmp_path: Path) -> None:
    """A copy that cannot be read stays, hides the variable, and is removed by the first open that can read it."""
    written_key = _encryption_key(b"a")
    store = CredentialsManager(tmp_path / "storage" / "credentials", encryption_key=written_key)
    store.save_credentials("openai", {"api_key": "sk-old", "_source": "env"})
    copy_path = store.get_credentials_path("openai")
    copy_bytes = copy_path.read_bytes()
    env = {"OPENAI_API_KEY": "sk-env"}

    runtime_paths = _restart(tmp_path, {**env, CREDENTIALS_ENCRYPTION_KEY_ENV: _encryption_key(b"b")})
    manager = get_runtime_credentials_manager(runtime_paths)

    assert manager.load_credentials("openai") is None
    assert get_api_key_for_provider("openai", runtime_paths) is None
    assert copy_path.read_bytes() == copy_bytes

    runtime_paths = _restart(tmp_path, {**env, CREDENTIALS_ENCRYPTION_KEY_ENV: written_key})
    manager = get_runtime_credentials_manager(runtime_paths)

    assert not copy_path.exists()
    assert get_api_key_for_provider("openai", runtime_paths) == "sk-env"


def test_a_dashboard_save_survives_the_cleanup(tmp_path: Path) -> None:
    """The cleanup removes only imported copies: dashboard saves, with or without a recorded source, keep winning."""
    store = CredentialsManager(tmp_path / "storage" / "credentials")
    store.save_credentials("openai", {"api_key": "sk-dashboard", "_source": "ui"})
    # Saved before documents recorded their source.
    store.save_credentials("anthropic", {"api_key": "sk-unrecorded"})
    store.save_credentials("google", {"api_key": "old-env-value", "_source": "env"})

    runtime_paths = _restart(
        tmp_path,
        {"OPENAI_API_KEY": "sk-env", "ANTHROPIC_API_KEY": "sk-env", "GOOGLE_API_KEY": "new-env-value"},
    )
    manager = get_runtime_credentials_manager(runtime_paths)

    assert get_api_key_for_provider("openai", runtime_paths) == "sk-dashboard"
    assert get_api_key_for_provider("anthropic", runtime_paths) == "sk-unrecorded"
    assert get_api_key_for_provider("google", runtime_paths) == "new-env-value"
    assert manager.get_credentials_path("openai").exists()
    assert manager.get_credentials_path("anthropic").exists()
    assert not manager.get_credentials_path("google").exists()


def test_a_write_back_of_a_declared_credential_is_kept_as_a_saved_one(tmp_path: Path) -> None:
    """A tool that saves back what it loaded from the environment keeps that save at the next start."""
    runtime_paths = _runtime_paths(tmp_path, {"OPENAI_API_KEY": "sk-env"})
    manager = get_runtime_credentials_manager(runtime_paths)
    manager.save_credentials("openai", {**(manager.load_credentials("openai") or {}), "api_key": "sk-renewed"})

    runtime_paths = _restart(tmp_path, {"OPENAI_API_KEY": "sk-env"})

    assert get_api_key_for_provider("openai", runtime_paths) == "sk-renewed"
