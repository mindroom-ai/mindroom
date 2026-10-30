"""Tests for the shared provider, embedder, and Ollama readers in ``credentials/sync.py``."""

from pathlib import Path

import pytest

from mindroom import constants as constants_mod
from mindroom.credentials.environment import EMBEDDER_CREDENTIAL_SERVICE
from mindroom.credentials.manager import CredentialsManager, get_runtime_shared_credentials_manager
from mindroom.credentials.sync import (
    _EMBEDDER_KEYLESS_PLACEHOLDER_API_KEY,
    get_api_key_for_provider,
    get_api_key_for_service,
    get_embedder_api_key,
    get_ollama_host,
)


@pytest.fixture
def runtime_paths(tmp_path: Path) -> constants_mod.RuntimePaths:
    """Return a primary runtime whose environment declares no credentials."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}\nmodels: {}\nrouter:\n  model: default\n", encoding="utf-8")
    return constants_mod.resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "storage",
        process_env={},
    )


@pytest.fixture
def credentials_manager(runtime_paths: constants_mod.RuntimePaths) -> CredentialsManager:
    """Return the shared credential store the readers read."""
    return get_runtime_shared_credentials_manager(runtime_paths)


def test_get_api_key_for_provider(
    credentials_manager: CredentialsManager,
    runtime_paths: constants_mod.RuntimePaths,
) -> None:
    """Test getting API key for different providers."""
    credentials_manager.save_credentials("openai", {"api_key": "test-openai-key"})
    credentials_manager.save_credentials("google", {"api_key": "test-google-key"})

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
    credentials_manager: CredentialsManager,
    runtime_paths: constants_mod.RuntimePaths,
) -> None:
    """Dashboard keys saved under the env var name resolve, but the canonical service wins."""
    credentials_manager.save_credentials("OPENROUTER_API_KEY", {"api_key": "env-named-openrouter-key"})
    credentials_manager.save_credentials("GOOGLE_API_KEY", {"api_key": "env-named-google-key"})
    credentials_manager.save_credentials("OLLAMA_HOST", {"api_key": "not-a-provider-key"})

    assert get_api_key_for_provider("openrouter", runtime_paths=runtime_paths) == "env-named-openrouter-key"
    assert get_api_key_for_provider("gemini", runtime_paths=runtime_paths) == "env-named-google-key"
    assert get_api_key_for_provider("ollama", runtime_paths=runtime_paths) is None
    assert get_api_key_for_provider("openai", runtime_paths=runtime_paths) is None

    credentials_manager.save_credentials("openrouter", {"api_key": "canonical-openrouter-key"})
    assert get_api_key_for_provider("openrouter", runtime_paths=runtime_paths) == "canonical-openrouter-key"


def test_get_api_key_for_service_is_strict(
    credentials_manager: CredentialsManager,
    runtime_paths: constants_mod.RuntimePaths,
) -> None:
    """A named service resolves only that service's API key."""
    credentials_manager.save_credentials("openai", {"api_key": "shared-key"})
    credentials_manager.save_credentials("openai-realtime", {"api_key": "realtime-key"})

    assert get_api_key_for_service("openai-realtime", runtime_paths) == "realtime-key"
    assert get_api_key_for_service("missing", runtime_paths) is None
    assert get_api_key_for_service("openrouter", runtime_paths) is None

    credentials_manager.save_credentials("OPENROUTER_API_KEY", {"api_key": "env-named-openrouter-key"})
    assert get_api_key_for_service("openrouter", runtime_paths) == "env-named-openrouter-key"


@pytest.mark.parametrize(
    ("saved_host", "configured_host", "saved_first", "reached_host"),
    [
        (None, None, False, "http://localhost:11434"),
        (None, None, True, "http://localhost:11434"),
        (None, "http://configured.example:11434", False, "http://configured.example:11434"),
        (None, "http://configured.example:11434", True, "http://configured.example:11434"),
        ("http://saved.example:11434", None, False, "http://saved.example:11434"),
        ("http://saved.example:11434", "", False, "http://saved.example:11434"),
        ("http://saved.example:11434", None, True, "http://saved.example:11434"),
        ("http://saved.example:11434", "http://configured.example:11434", False, "http://configured.example:11434"),
        ("http://saved.example:11434", "http://configured.example:11434", True, "http://saved.example:11434"),
    ],
)
def test_get_ollama_host(
    credentials_manager: CredentialsManager,
    runtime_paths: constants_mod.RuntimePaths,
    saved_host: str | None,
    configured_host: str | None,
    saved_first: bool,
    reached_host: str,
) -> None:
    """The configured and saved Ollama hosts are tried in the caller's order, then the local default."""
    if saved_host is not None:
        credentials_manager.save_credentials("ollama", {"host": saved_host})

    reached = get_ollama_host(runtime_paths=runtime_paths, configured_host=configured_host, saved_first=saved_first)

    assert reached == reached_host


def test_get_embedder_api_key_explicit_wins(
    credentials_manager: CredentialsManager,
    runtime_paths: constants_mod.RuntimePaths,
) -> None:
    """An explicit config key must beat both credential services."""
    credentials_manager.save_credentials(EMBEDDER_CREDENTIAL_SERVICE, {"api_key": "embedder-key"})
    credentials_manager.save_credentials("openai", {"api_key": "openai-key"})

    assert get_embedder_api_key(runtime_paths, explicit_api_key="explicit-key") == "explicit-key"


def test_get_embedder_api_key_embedder_service_beats_openai(
    credentials_manager: CredentialsManager,
    runtime_paths: constants_mod.RuntimePaths,
) -> None:
    """The dedicated embedder credential must beat the shared openai key."""
    credentials_manager.save_credentials(EMBEDDER_CREDENTIAL_SERVICE, {"api_key": "embedder-key"})
    credentials_manager.save_credentials("openai", {"api_key": "openai-key"})

    assert get_embedder_api_key(runtime_paths) == "embedder-key"


def test_get_embedder_api_key_configured_service_is_a_strict_binding(
    credentials_manager: CredentialsManager,
    runtime_paths: constants_mod.RuntimePaths,
) -> None:
    """A named service wins over legacy services and never leaks into their fallback chain."""
    credentials_manager.save_credentials("embedding-production", {"api_key": "named-key"})
    credentials_manager.save_credentials(EMBEDDER_CREDENTIAL_SERVICE, {"api_key": "embedder-key"})
    credentials_manager.save_credentials("openai", {"api_key": "openai-key"})

    assert get_embedder_api_key(runtime_paths, credentials_service="embedding-production") == "named-key"

    credentials_manager.delete_credentials("embedding-production")
    assert (
        get_embedder_api_key(runtime_paths, credentials_service="embedding-production")
        == _EMBEDDER_KEYLESS_PLACEHOLDER_API_KEY
    )


def test_get_embedder_api_key_explicit_key_wins_over_configured_service(
    credentials_manager: CredentialsManager,
    runtime_paths: constants_mod.RuntimePaths,
) -> None:
    """Inline config remains the highest-priority backwards-compatible override."""
    credentials_manager.save_credentials("embedding-production", {"api_key": "named-key"})

    assert (
        get_embedder_api_key(
            runtime_paths,
            explicit_api_key="inline-key",
            credentials_service="embedding-production",
        )
        == "inline-key"
    )


def test_get_embedder_api_key_falls_back_to_openai(
    credentials_manager: CredentialsManager,
    runtime_paths: constants_mod.RuntimePaths,
) -> None:
    """Without a dedicated credential the shared openai key keeps working."""
    credentials_manager.save_credentials("openai", {"api_key": "openai-key"})

    assert get_embedder_api_key(runtime_paths) == "openai-key"


def test_get_embedder_api_key_accepts_env_var_named_provider_services(
    credentials_manager: CredentialsManager,
    runtime_paths: constants_mod.RuntimePaths,
) -> None:
    """The openai fallback and a provider credentials_service both accept env-var-named services."""
    credentials_manager.save_credentials("OPENAI_API_KEY", {"api_key": "env-named-openai-key"})
    credentials_manager.save_credentials("OPENROUTER_API_KEY", {"api_key": "env-named-openrouter-key"})

    assert get_embedder_api_key(runtime_paths) == "env-named-openai-key"
    assert get_embedder_api_key(runtime_paths, credentials_service="openrouter") == "env-named-openrouter-key"

    credentials_manager.save_credentials("openrouter", {"api_key": "canonical-openrouter-key"})
    assert get_embedder_api_key(runtime_paths, credentials_service="openrouter") == "canonical-openrouter-key"


def test_get_embedder_api_key_returns_placeholder_when_nothing_configured(
    runtime_paths: constants_mod.RuntimePaths,
) -> None:
    """Keyless mode resolves the placeholder so client construction never crashes."""
    assert get_embedder_api_key(runtime_paths) == _EMBEDDER_KEYLESS_PLACEHOLDER_API_KEY


def test_get_embedder_api_key_strips_resolved_values(
    credentials_manager: CredentialsManager,
    runtime_paths: constants_mod.RuntimePaths,
) -> None:
    """Keys with stray whitespace must be stripped before reaching the Authorization header."""
    assert get_embedder_api_key(runtime_paths, explicit_api_key="explicit-key\n") == "explicit-key"

    credentials_manager.save_credentials(EMBEDDER_CREDENTIAL_SERVICE, {"api_key": " embedder-key "})
    assert get_embedder_api_key(runtime_paths) == "embedder-key"


def test_get_embedder_api_key_ignores_non_string_stored_credential(
    credentials_manager: CredentialsManager,
    runtime_paths: constants_mod.RuntimePaths,
) -> None:
    """A malformed stored value resolves as absent instead of crashing resolution."""
    credentials_manager.save_credentials(EMBEDDER_CREDENTIAL_SERVICE, {"api_key": 42})
    credentials_manager.save_credentials("openai", {"api_key": "openai-key"})

    assert get_embedder_api_key(runtime_paths) == "openai-key"


def test_get_embedder_api_key_treats_blank_values_as_absent(
    credentials_manager: CredentialsManager,
    runtime_paths: constants_mod.RuntimePaths,
) -> None:
    """Blank explicit and blank embedder-service values must not shadow real keys."""
    credentials_manager.save_credentials(EMBEDDER_CREDENTIAL_SERVICE, {"api_key": "   "})
    credentials_manager.save_credentials("openai", {"api_key": "openai-key"})

    assert get_embedder_api_key(runtime_paths, explicit_api_key="  ") == "openai-key"
