"""Tests for named connection resolution and validation."""

from __future__ import annotations

from pathlib import Path  # noqa: TC003

import pytest
from pydantic import ValidationError

from mindroom.config.connections import ConnectionConfig
from mindroom.config.main import Config
from mindroom.connections import (
    connection_google_application_credentials_path,
    connection_oauth_client,
    resolve_connection,
)
from mindroom.constants import RuntimePaths, resolve_primary_runtime_paths
from mindroom.credentials import get_runtime_shared_credentials_manager


def _runtime_paths(tmp_path: Path) -> RuntimePaths:
    return resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
    )


@pytest.mark.parametrize(
    ("config_kwargs", "provider", "purpose", "expected_connection_id"),
    [
        ({}, "openai", "chat_model", "openai/default"),
        ({}, "openai", "voice_stt", "openai/stt"),
        (
            {
                "memory": {
                    "embedder": {
                        "provider": "sentence_transformers",
                        "config": {"model": "sentence-transformers/all-MiniLM-L6-v2"},
                    },
                },
            },
            "openai",
            "embedder",
            "openai/embeddings",
        ),
        ({}, "gemini", "chat_model", "google/default"),
    ],
)
def test_resolve_connection_requires_configured_connections_for_authenticated_providers(
    tmp_path: Path,
    config_kwargs: dict[str, object],
    provider: str,
    purpose: str,
    expected_connection_id: str,
) -> None:
    """Authenticated providers without an active implicit default should still require a configured connection."""
    runtime_paths = _runtime_paths(tmp_path)

    with pytest.raises(ValueError, match=rf"requires a configured connection.*{expected_connection_id}"):
        resolve_connection(
            Config(**config_kwargs),
            provider=provider,
            purpose=purpose,
            runtime_paths=runtime_paths,
        )


def test_resolve_connection_uses_configured_default_named_services(tmp_path: Path) -> None:
    """Configured default chat, STT, and embedder routes should resolve to stable connection ids."""
    runtime_paths = _runtime_paths(tmp_path)
    shared_credentials = get_runtime_shared_credentials_manager(runtime_paths)
    shared_credentials.save_credentials("openai", {"api_key": "sk-openai", "_source": "test"})
    shared_credentials.save_credentials("google_gemini", {"api_key": "sk-google", "_source": "test"})
    config = Config(
        connections={
            "openai/default": {
                "provider": "openai",
                "service": "openai",
                "auth_kind": "api_key",
            },
            "openai/stt": {
                "provider": "openai",
                "service": "openai",
                "auth_kind": "api_key",
            },
            "openai/embeddings": {
                "provider": "openai",
                "service": "openai",
                "auth_kind": "api_key",
            },
            "google/default": {
                "provider": "google",
                "service": "google_gemini",
                "auth_kind": "api_key",
            },
        },
    )

    chat_connection = resolve_connection(
        config,
        provider="openai",
        purpose="chat_model",
        runtime_paths=runtime_paths,
    )
    stt_connection = resolve_connection(
        config,
        provider="openai",
        purpose="voice_stt",
        runtime_paths=runtime_paths,
    )
    embedder_connection = resolve_connection(
        config,
        provider="openai",
        purpose="embedder",
        runtime_paths=runtime_paths,
    )
    gemini_connection = resolve_connection(
        config,
        provider="gemini",
        purpose="chat_model",
        runtime_paths=runtime_paths,
    )

    assert chat_connection.connection_id == "openai/default"
    assert stt_connection.connection_id == "openai/stt"
    assert embedder_connection.connection_id == "openai/embeddings"
    assert chat_connection.service == stt_connection.service == embedder_connection.service == "openai"
    assert gemini_connection.connection_id == "google/default"
    assert gemini_connection.provider == "google"
    assert gemini_connection.service == "google_gemini"


def test_resolve_connection_uses_provider_default_as_voice_stt_fallback(tmp_path: Path) -> None:
    """Voice STT should still fall back to the provider default connection when no STT-specific id exists."""
    runtime_paths = _runtime_paths(tmp_path)
    shared_credentials = get_runtime_shared_credentials_manager(runtime_paths)
    shared_credentials.save_credentials("groq", {"api_key": "sk-groq", "_source": "test"})
    config = Config(
        connections={
            "groq/default": {
                "provider": "groq",
                "service": "groq",
                "auth_kind": "api_key",
            },
        },
    )

    stt_connection = resolve_connection(
        config,
        provider="groq",
        purpose="voice_stt",
        runtime_paths=runtime_paths,
    )

    assert stt_connection.connection_id == "groq/default"
    assert stt_connection.provider == "groq"
    assert stt_connection.service == "groq"


def test_resolve_connection_supports_google_adc_and_oauth_client(tmp_path: Path) -> None:
    """ADC and OAuth client auth kinds should load their expected payload shapes."""
    runtime_paths = _runtime_paths(tmp_path)
    shared_credentials = get_runtime_shared_credentials_manager(runtime_paths)
    shared_credentials.save_credentials(
        "google_vertex_adc",
        {"application_credentials_path": "/var/lib/mindroom-tests/google-adc.json", "_source": "test"},
    )
    shared_credentials.save_credentials(
        "google_oauth_client",
        {"client_id": "client-id", "client_secret": "client-secret", "_source": "test"},
    )
    config = Config(
        connections={
            "vertexai_claude/default": {
                "provider": "vertexai_claude",
                "service": "google_vertex_adc",
                "auth_kind": "google_adc",
            },
            "google/oauth": {
                "provider": "google",
                "service": "google_oauth_client",
                "auth_kind": "oauth_client",
            },
        },
    )

    vertex_connection = resolve_connection(
        config,
        provider="vertexai_claude",
        purpose="chat_model",
        runtime_paths=runtime_paths,
    )
    oauth_connection = resolve_connection(
        config,
        provider="google",
        purpose="google_oauth_client",
        runtime_paths=runtime_paths,
    )

    assert vertex_connection.connection_id == "vertexai_claude/default"
    assert vertex_connection.auth_kind == "google_adc"
    assert (
        connection_google_application_credentials_path(vertex_connection) == "/var/lib/mindroom-tests/google-adc.json"
    )
    assert oauth_connection.connection_id == "google/oauth"
    assert oauth_connection.auth_kind == "oauth_client"
    assert connection_oauth_client(oauth_connection) == ("client-id", "client-secret")


def test_resolve_connection_allows_ambient_google_adc(tmp_path: Path) -> None:
    """Vertex ADC connections should still resolve when ambient ADC provides credentials."""
    runtime_paths = _runtime_paths(tmp_path)
    config = Config(
        connections={
            "vertexai_claude/default": {
                "provider": "vertexai_claude",
                "service": "google_vertex_adc",
                "auth_kind": "google_adc",
            },
        },
    )

    vertex_connection = resolve_connection(
        config,
        provider="vertexai_claude",
        purpose="chat_model",
        runtime_paths=runtime_paths,
    )

    assert vertex_connection.connection_id == "vertexai_claude/default"
    assert vertex_connection.auth_kind == "google_adc"
    assert vertex_connection.service == "google_vertex_adc"
    assert vertex_connection.credentials is None
    assert connection_google_application_credentials_path(vertex_connection) is None


def test_resolve_connection_supports_auth_kind_none_for_ollama(tmp_path: Path) -> None:
    """Providers with auth_kind=none should still resolve without configured connections."""
    resolved = resolve_connection(
        Config(),
        provider="ollama",
        purpose="chat_model",
        runtime_paths=_runtime_paths(tmp_path),
    )

    assert resolved.connection_id == "ollama"
    assert resolved.provider == "ollama"
    assert resolved.auth_kind == "none"
    assert resolved.service is None
    assert resolved.credentials is None


def test_resolve_connection_allows_auth_kind_none_for_openai_voice_stt_endpoint(tmp_path: Path) -> None:
    """Explicit OpenAI-compatible STT connections may intentionally disable auth."""
    resolved = resolve_connection(
        Config(
            connections={
                "openai/local": {
                    "provider": "openai",
                    "auth_kind": "none",
                },
            },
            voice={
                "enabled": True,
                "stt": {
                    "provider": "openai",
                    "model": "whisper-1",
                    "connection": "openai/local",
                },
            },
        ),
        provider="openai",
        purpose="voice_stt",
        connection_name="openai/local",
        runtime_paths=_runtime_paths(tmp_path),
    )

    assert resolved.connection_id == "openai/local"
    assert resolved.auth_kind == "none"
    assert resolved.service is None
    assert resolved.credentials is None


def test_resolve_connection_rejects_provider_mismatch(tmp_path: Path) -> None:
    """Explicit named connections must match the consuming provider."""
    runtime_paths = _runtime_paths(tmp_path)
    config = Config(
        connections={
            "anthropic/custom": {
                "provider": "anthropic",
                "service": "anthropic",
                "auth_kind": "api_key",
            },
        },
    )

    with pytest.raises(ValueError, match="configured for provider 'anthropic', not 'openai'"):
        resolve_connection(
            config,
            provider="openai",
            purpose="chat_model",
            connection_name="anthropic/custom",
            runtime_paths=runtime_paths,
        )


def test_resolve_connection_rejects_auth_kind_mismatch(tmp_path: Path) -> None:
    """Runtime resolution should reject malformed auth kinds even if config was bypassed."""
    runtime_paths = _runtime_paths(tmp_path)
    config = Config()
    config.connections["google/oauth"] = ConnectionConfig.model_construct(
        provider="google",
        service="google_oauth_client",
        auth_kind="none",
    )

    with pytest.raises(ValueError, match="requires 'oauth_client'"):
        resolve_connection(
            config,
            provider="google",
            purpose="google_oauth_client",
            runtime_paths=runtime_paths,
        )


def test_resolve_connection_rejects_missing_service_for_authenticated_connection(tmp_path: Path) -> None:
    """Authenticated connections should fail explicitly when service metadata is malformed."""
    runtime_paths = _runtime_paths(tmp_path)
    config = Config()
    config.connections["openai/broken"] = ConnectionConfig.model_construct(
        provider="openai",
        service=None,
        auth_kind="api_key",
    )

    with pytest.raises(ValueError, match="no credential service is configured"):
        resolve_connection(
            config,
            provider="openai",
            purpose="chat_model",
            connection_name="openai/broken",
            runtime_paths=runtime_paths,
        )


def test_resolve_connection_rejects_missing_credentials_payload(tmp_path: Path) -> None:
    """Authenticated connections should fail explicitly when shared credentials are missing."""
    runtime_paths = _runtime_paths(tmp_path)
    config = Config(
        connections={
            "openai/default": {
                "provider": "openai",
                "service": "openai",
                "auth_kind": "api_key",
            },
        },
    )

    with pytest.raises(ValueError, match="Connection 'openai/default' is missing credentials"):
        resolve_connection(
            config,
            provider="openai",
            purpose="chat_model",
            runtime_paths=runtime_paths,
        )


def test_config_rejects_unknown_explicit_connection_reference() -> None:
    """Config validation should fail fast on unknown explicit connection ids."""
    with pytest.raises(ValidationError, match="Unknown connection 'missing/connection'"):
        Config(
            models={
                "default": {
                    "provider": "openai",
                    "id": "gpt-5.4",
                    "connection": "missing/connection",
                },
            },
        )


def test_validate_with_runtime_synthesizes_missing_default_chat_connection(tmp_path: Path) -> None:
    """Runtime-bound validation should materialize the conventional default chat connection when omitted."""
    config = Config.validate_with_runtime(
        {
            "models": {
                "default": {
                    "provider": "openai",
                    "id": "gpt-5.4",
                },
            },
        },
        _runtime_paths(tmp_path),
        strict_connection_validation=True,
    )

    assert config.models["default"].connection is None
    assert config.connections["openai/default"].provider == "openai"
    assert config.connections["openai/default"].service == "openai"
    assert config.connections["openai/default"].auth_kind == "api_key"


def test_validate_with_runtime_synthesizes_google_default_chat_connection(tmp_path: Path) -> None:
    """Google/Gemini defaults should use the dedicated Gemini credential bucket."""
    config = Config.validate_with_runtime(
        {
            "models": {
                "default": {
                    "provider": "gemini",
                    "id": "gemini-2.5-flash",
                },
            },
        },
        _runtime_paths(tmp_path),
        strict_connection_validation=True,
    )

    assert config.connections["google/default"].provider == "google"
    assert config.connections["google/default"].service == "google_gemini"
    assert config.connections["google/default"].auth_kind == "api_key"


def test_validate_with_runtime_synthesizes_missing_default_memory_embedder_connection(tmp_path: Path) -> None:
    """Runtime-bound validation should materialize the active default memory embedder connection."""
    config = Config.validate_with_runtime(
        {
            "connections": {
                "openai/default": {
                    "provider": "openai",
                    "service": "openai",
                    "auth_kind": "api_key",
                },
            },
        },
        _runtime_paths(tmp_path),
        strict_connection_validation=True,
    )

    assert config.memory.embedder.provider == "openai"
    assert config.memory.embedder.config.connection is None
    assert config.connections["openai/embeddings"].provider == "openai"
    assert config.connections["openai/embeddings"].service == "openai"
    assert config.connections["openai/embeddings"].auth_kind == "api_key"


def test_validate_with_runtime_synthesized_defaults_inherit_authored_service(tmp_path: Path) -> None:
    """Synthesized provider defaults should reuse the authored conventional default service name."""
    config = Config.validate_with_runtime(
        {
            "connections": {
                "openai/default": {
                    "provider": "openai",
                    "service": "tenant-openai",
                    "auth_kind": "api_key",
                },
            },
            "voice": {
                "stt": {
                    "provider": "openai",
                },
            },
        },
        _runtime_paths(tmp_path),
        strict_connection_validation=True,
    )

    assert config.connections["openai/embeddings"].service == "tenant-openai"
    assert config.connections["openai/stt"].service == "tenant-openai"


def test_validate_with_runtime_synthesized_voice_stt_inherits_auth_free_parent(tmp_path: Path) -> None:
    """Synthesized OpenAI STT defaults should inherit auth_kind=none from an authored auth-free parent."""
    config = Config.validate_with_runtime(
        {
            "connections": {
                "openai/default": {
                    "provider": "openai",
                    "auth_kind": "none",
                },
            },
            "memory": {
                "embedder": {
                    "provider": "sentence_transformers",
                    "config": {
                        "model": "sentence-transformers/all-MiniLM-L6-v2",
                    },
                },
            },
            "voice": {
                "stt": {
                    "provider": "openai",
                },
            },
        },
        _runtime_paths(tmp_path),
        strict_connection_validation=True,
    )

    assert config.connections["openai/stt"].service is None
    assert config.connections["openai/stt"].auth_kind == "none"


def test_validate_with_runtime_synthesizes_non_openai_voice_stt_connection(tmp_path: Path) -> None:
    """Runtime validation should synthesize non-OpenAI voice STT defaults from the provider default bucket."""
    config = Config.validate_with_runtime(
        {
            "connections": {
                "groq/default": {
                    "provider": "groq",
                    "service": "tenant-groq",
                    "auth_kind": "api_key",
                },
            },
            "voice": {
                "stt": {
                    "provider": "groq",
                },
            },
        },
        _runtime_paths(tmp_path),
        strict_connection_validation=True,
    )

    assert config.connections["groq/stt"].service == "tenant-groq"


def test_validate_with_runtime_synthesized_defaults_ignore_nondefault_siblings(tmp_path: Path) -> None:
    """Synthesized defaults should not inherit from arbitrary same-provider siblings."""
    config = Config.validate_with_runtime(
        {
            "connections": {
                "openai/research": {
                    "provider": "openai",
                    "service": "tenant-openai",
                    "auth_kind": "api_key",
                },
            },
            "models": {
                "default": {
                    "provider": "openai",
                    "id": "gpt-5.4",
                },
            },
            "voice": {
                "stt": {
                    "provider": "openai",
                },
            },
        },
        _runtime_paths(tmp_path),
        strict_connection_validation=True,
    )

    assert config.connections["openai/default"].service == "openai"
    assert config.connections["openai/embeddings"].service == "openai"
    assert config.connections["openai/stt"].service == "openai"


def test_config_rejects_explicit_connection_auth_kind_mismatch() -> None:
    """Explicit connection references must use the auth kind required by the consumer."""
    with pytest.raises(ValidationError, match="requires 'api_key'"):
        Config(
            connections={
                "openai/oauth": {
                    "provider": "openai",
                    "service": "openai_oauth",
                    "auth_kind": "oauth_client",
                },
            },
            models={
                "default": {
                    "provider": "openai",
                    "id": "gpt-5.4",
                    "connection": "openai/oauth",
                },
            },
        )


def test_config_allows_explicit_openai_voice_stt_connection_with_auth_kind_none() -> None:
    """OpenAI-compatible STT endpoints may opt out of auth with an explicit connection."""
    config = Config(
        connections={
            "openai/local": {
                "provider": "openai",
                "auth_kind": "none",
            },
        },
        voice={
            "enabled": True,
            "stt": {
                "provider": "openai",
                "model": "whisper-1",
                "connection": "openai/local",
            },
        },
    )

    assert config.connections["openai/local"].auth_kind == "none"
    assert config.voice.stt.connection == "openai/local"


def test_config_rejects_explicit_openai_model_connection_with_auth_kind_none() -> None:
    """SDK-backed OpenAI model connections must not allow auth_kind=none."""
    with pytest.raises(ValidationError, match="requires 'api_key'"):
        Config(
            connections={
                "openai/local": {
                    "provider": "openai",
                    "auth_kind": "none",
                },
            },
            models={
                "default": {
                    "provider": "openai",
                    "id": "gpt-5.4",
                    "connection": "openai/local",
                },
            },
        )


def test_validate_with_runtime_rejects_legacy_inline_api_keys(tmp_path: Path) -> None:
    """Runtime validation should reject removed inline API key fields."""
    runtime_paths = _runtime_paths(tmp_path)

    with pytest.raises(ValidationError, match="api_key"):
        Config.validate_with_runtime(
            {
                "models": {
                    "default": {
                        "provider": "openai",
                        "id": "gpt-5.4",
                        "api_key": "sk-chat",
                    },
                },
            },
            runtime_paths,
        )

    with pytest.raises(ValidationError, match="api_key"):
        Config.validate_with_runtime(
            {
                "memory": {
                    "embedder": {
                        "provider": "openai",
                        "config": {
                            "model": "text-embedding-3-small",
                            "api_key": "sk-embed",
                        },
                    },
                    "llm": {
                        "provider": "anthropic",
                        "config": {
                            "model": "claude-sonnet-4-6",
                            "api_key": "sk-memory-llm",
                        },
                    },
                },
            },
            runtime_paths,
        )

    with pytest.raises(ValidationError, match="api_key"):
        Config.validate_with_runtime(
            {
                "voice": {
                    "enabled": True,
                    "stt": {
                        "provider": "openai",
                        "model": "whisper-1",
                        "api_key": "sk-stt",
                    },
                },
            },
            runtime_paths,
        )


def test_config_rejects_default_vertex_adc_auth_kind_mismatch() -> None:
    """Vertex models using the default connection must require ADC auth."""
    with pytest.raises(ValidationError, match="requires 'google_adc'"):
        Config(
            connections={
                "vertexai_claude/default": {
                    "provider": "vertexai_claude",
                    "service": "google_vertex_adc",
                    "auth_kind": "api_key",
                },
            },
            models={
                "default": {
                    "provider": "vertexai_claude",
                    "id": "claude-sonnet-4-6",
                },
            },
        )


def test_config_rejects_configured_default_connection_provider_mismatch() -> None:
    """Configured default connections must still match the consuming provider."""
    with pytest.raises(
        ValidationError,
        match=r"models\.default\.connection: Connection 'openai/default' is for provider 'anthropic', not 'openai'",
    ):
        Config(
            connections={
                "openai/default": {
                    "provider": "anthropic",
                    "service": "anthropic",
                    "auth_kind": "api_key",
                },
            },
            models={
                "default": {
                    "provider": "openai",
                    "id": "gpt-5.4",
                },
            },
        )


def test_config_rejects_inline_model_api_key_bypasses() -> None:
    """Model config should reject inline API key escape hatches."""
    with pytest.raises(ValidationError, match="extra_kwargs.api_key"):
        Config(
            models={
                "default": {
                    "provider": "openai",
                    "id": "gpt-5.4",
                    "extra_kwargs": {"api_key": "sk-inline"},
                },
            },
        )


def test_config_rejects_inline_model_client_credentials_bypass() -> None:
    """Model config should reject inline Vertex client credential escape hatches."""
    with pytest.raises(ValidationError, match="extra_kwargs.client_params.credentials"):
        Config(
            models={
                "default": {
                    "provider": "vertexai_claude",
                    "id": "claude-sonnet-4-6",
                    "extra_kwargs": {
                        "client_params": {
                            "credentials": "inline",
                        },
                    },
                },
            },
        )


def test_config_rejects_inline_memory_api_key_bypasses() -> None:
    """Memory config should reject inline embedder and LLM API keys."""
    with pytest.raises(ValidationError):
        Config(
            memory={
                "embedder": {
                    "provider": "openai",
                    "config": {
                        "model": "text-embedding-3-small",
                        "api_key": "sk-inline",
                    },
                },
            },
        )

    with pytest.raises(ValidationError, match="memory.llm.config.api_key"):
        Config(
            memory={
                "llm": {
                    "provider": "openai",
                    "config": {
                        "model": "gpt-4o-mini",
                        "api_key": "sk-inline",
                    },
                },
            },
        )


def test_config_rejects_inline_voice_api_key_bypass() -> None:
    """Voice STT config should reject inline API keys."""
    with pytest.raises(ValidationError):
        Config(
            voice={
                "enabled": True,
                "stt": {
                    "provider": "openai",
                    "model": "whisper-1",
                    "api_key": "sk-inline",
                },
            },
        )


def test_connection_config_rejects_reserved_google_token_bucket_service() -> None:
    """User-authored connections must not reuse the reserved Google token bucket service name."""
    with pytest.raises(ValidationError, match="reserved for backend-managed Google token storage"):
        ConnectionConfig(
            provider="google",
            service="google",
            auth_kind="api_key",
        )
