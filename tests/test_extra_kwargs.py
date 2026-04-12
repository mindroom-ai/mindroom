"""Test extra_kwargs functionality in model configuration."""

import base64
import tempfile
from pathlib import Path

import pytest
import yaml
from agno.media import File, Image
from agno.models.message import Message
from agno.models.vertexai.claude import Claude as VertexAIClaude
from pydantic import ValidationError

from mindroom.ai import get_model_instance
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.credentials import get_runtime_shared_credentials_manager
from mindroom.vertex_claude_prompt_cache import (
    copy_messages_with_vertex_prompt_cache_breakpoint,
    install_vertex_claude_prompt_cache_hook,
)


def _config_with_runtime_paths(config_data: dict[str, object]) -> tuple[Config, RuntimePaths]:
    runtime_root = Path(tempfile.mkdtemp())
    runtime_paths = resolve_runtime_paths(
        config_path=runtime_root / "config.yaml",
        storage_path=runtime_root / "mindroom_data",
        process_env={},
    )
    config = Config(**config_data)
    return config, runtime_paths


def _set_api_key(runtime_paths: RuntimePaths, service: str, api_key: str) -> None:
    get_runtime_shared_credentials_manager(runtime_paths).save_credentials(
        service,
        {"api_key": api_key, "_source": "test"},
    )


def _api_key_connection(provider: str, *, service: str | None = None) -> dict[str, str]:
    canonical_provider = "google" if provider == "gemini" else provider
    return {
        "provider": canonical_provider,
        "service": service or canonical_provider,
        "auth_kind": "api_key",
    }


def test_model_config_with_extra_kwargs() -> None:
    """Test that ModelConfig accepts and stores extra_kwargs."""
    extra_kwargs = {
        "request_params": {
            "provider": {
                "order": ["Cerebras"],
                "allow_fallbacks": False,
            },
        },
    }

    model_config = ModelConfig(
        provider="openrouter",
        id="openai/gpt-4",
        extra_kwargs=extra_kwargs,
    )

    assert model_config.extra_kwargs == extra_kwargs
    assert model_config.extra_kwargs["request_params"]["provider"]["order"] == ["Cerebras"]


def test_config_yaml_with_extra_kwargs() -> None:
    """Test loading config from YAML with extra_kwargs."""
    config_data = {
        "connections": {
            "openrouter/default": _api_key_connection("openrouter"),
        },
        "models": {
            "test_model": {
                "provider": "openrouter",
                "id": "openai/gpt-4",
                "extra_kwargs": {
                    "request_params": {
                        "provider": {
                            "order": ["Cerebras"],
                            "allow_fallbacks": False,
                        },
                    },
                    "temperature": 0.7,
                    "max_tokens": 4096,
                },
            },
        },
        "defaults": {
            "markdown": True,
        },
        "router": {
            "model": "test_model",
        },
        "memory": {
            "embedder": {
                "provider": "openai",
                "config": {
                    "model": "text-embedding-3-small",
                },
            },
        },
        "agents": {},
    }

    # Create a temporary YAML file
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(config_data, f)
        temp_path = f.name

    try:
        # Load config from YAML
        with Path(temp_path).open() as f:
            loaded_data = yaml.safe_load(f)

        config = Config(**loaded_data)

        # Check the model configuration
        model = config.models["test_model"]
        assert model.extra_kwargs is not None
        assert model.extra_kwargs["request_params"]["provider"]["order"] == ["Cerebras"]
        assert model.extra_kwargs["temperature"] == 0.7
        assert model.extra_kwargs["max_tokens"] == 4096
    finally:
        # Clean up
        Path(temp_path).unlink()


def test_get_model_instance_with_extra_kwargs() -> None:
    """Test that get_model_instance passes extra_kwargs to the model."""
    config_data = {
        "connections": {
            "openrouter/default": _api_key_connection("openrouter"),
        },
        "models": {
            "test_model": {
                "provider": "openrouter",
                "id": "openai/gpt-4",
                "extra_kwargs": {
                    "request_params": {
                        "provider": {
                            "order": ["Cerebras"],
                            "allow_fallbacks": False,
                        },
                    },
                    "temperature": 0.8,
                },
            },
        },
        "defaults": {
            "markdown": True,
        },
        "router": {
            "model": "test_model",
        },
        "memory": {
            "embedder": {
                "provider": "openai",
                "config": {
                    "model": "text-embedding-3-small",
                },
            },
        },
        "agents": {},
    }

    config, runtime_paths = _config_with_runtime_paths(config_data)
    _set_api_key(runtime_paths, "openrouter", "test-key")

    # Get the model instance
    model = get_model_instance(config, runtime_paths, "test_model")

    # Check that the model has the correct parameters
    assert model.id == "openai/gpt-4"
    assert model.request_params is not None
    assert model.request_params["provider"]["order"] == ["Cerebras"]
    assert model.request_params["provider"]["allow_fallbacks"] is False

    # Check that temperature was also passed
    assert model.temperature == 0.8


def test_different_providers_with_extra_kwargs() -> None:
    """Test that extra_kwargs works with different providers."""
    config_data = {
        "connections": {
            "openai/default": _api_key_connection("openai"),
            "anthropic/default": _api_key_connection("anthropic"),
        },
        "models": {
            "openai_model": {
                "provider": "openai",
                "id": "gpt-4",
                "extra_kwargs": {
                    "temperature": 0.5,
                    "top_p": 0.9,
                    "frequency_penalty": 0.3,
                },
            },
            "anthropic_model": {
                "provider": "anthropic",
                "id": "claude-3-opus",
                "extra_kwargs": {
                    "temperature": 0.2,
                    "max_tokens": 2048,
                },
            },
        },
        "defaults": {
            "markdown": True,
        },
        "router": {
            "model": "openai_model",
        },
        "memory": {
            "embedder": {
                "provider": "openai",
                "config": {
                    "model": "text-embedding-3-small",
                },
            },
        },
        "agents": {},
    }

    config, runtime_paths = _config_with_runtime_paths(config_data)
    _set_api_key(runtime_paths, "openai", "test-key")
    _set_api_key(runtime_paths, "anthropic", "test-key")

    # Test OpenAI model
    openai_model = get_model_instance(config, runtime_paths, "openai_model")
    assert openai_model.temperature == 0.5
    assert openai_model.top_p == 0.9
    assert openai_model.frequency_penalty == 0.3

    # Test Anthropic model
    anthropic_model = get_model_instance(config, runtime_paths, "anthropic_model")
    assert anthropic_model.temperature == 0.2
    assert anthropic_model.max_tokens == 2048
    assert anthropic_model.cache_system_prompt is True
    assert anthropic_model.extended_cache_time is True


def test_model_without_extra_kwargs() -> None:
    """Test that models work fine without extra_kwargs."""
    config_data = {
        "connections": {
            "openai/default": _api_key_connection("openai"),
        },
        "models": {
            "simple_model": {
                "provider": "openai",
                "id": "gpt-3.5-turbo",
                # No extra_kwargs
            },
        },
        "defaults": {
            "markdown": True,
        },
        "router": {
            "model": "simple_model",
        },
        "memory": {
            "embedder": {
                "provider": "openai",
                "config": {
                    "model": "text-embedding-3-small",
                },
            },
        },
        "agents": {},
    }

    config, runtime_paths = _config_with_runtime_paths(config_data)
    _set_api_key(runtime_paths, "openai", "test-key")

    # Should work without any issues
    model = get_model_instance(config, runtime_paths, "simple_model")
    assert model.id == "gpt-3.5-turbo"
    assert model.provider == "OpenAI"


def test_vertexai_claude_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test native Vertex Claude provider mapping."""
    config_data = {
        "connections": {
            "vertexai_claude/default": {
                "provider": "vertexai_claude",
                "service": "google_vertex_adc",
                "auth_kind": "google_adc",
            },
        },
        "models": {
            "vertex_claude_model": {
                "provider": "vertexai_claude",
                "id": "claude-sonnet-4@20250514",
                "extra_kwargs": {
                    "project_id": "demo-project",
                    "region": "us-central1",
                },
            },
        },
        "defaults": {
            "markdown": True,
        },
        "router": {
            "model": "vertex_claude_model",
        },
        "memory": {
            "embedder": {
                "provider": "openai",
                "config": {
                    "model": "text-embedding-3-small",
                },
            },
        },
        "agents": {},
    }

    config, runtime_paths = _config_with_runtime_paths(config_data)
    credentials_path = runtime_paths.storage_root / "vertex-adc.json"
    get_runtime_shared_credentials_manager(runtime_paths).save_credentials(
        "google_vertex_adc",
        {
            "application_credentials_path": str(credentials_path),
            "_source": "test",
        },
    )

    def fake_load_credentials_from_file(_path: str, *, scopes: list[str]) -> tuple[object, str]:
        assert scopes == ["https://www.googleapis.com/auth/cloud-platform"]
        return object(), "ignored-project"

    monkeypatch.setattr(
        "google.auth.load_credentials_from_file",
        fake_load_credentials_from_file,
    )
    model = get_model_instance(config, runtime_paths, "vertex_claude_model")

    assert isinstance(model, VertexAIClaude)
    assert model.id == "claude-sonnet-4@20250514"
    assert model.provider == "VertexAI"
    assert model.cache_system_prompt is True
    assert model.extended_cache_time is True


def test_vertexai_prompt_cache_breakpoint_marks_last_user_block() -> None:
    """Vertex Claude requests should cache through the latest user text block."""
    model = VertexAIClaude(
        id="claude-sonnet-4-6",
        project_id="demo-project",
        region="us-central1",
        cache_system_prompt=True,
        extended_cache_time=True,
    )
    messages = [
        Message(role="system", content="System prompt"),
        Message(role="assistant", content="Earlier reply"),
        Message(role="user", content=[{"type": "text", "text": "Current turn"}, {"type": "image", "source": "x"}]),
    ]

    prepared = copy_messages_with_vertex_prompt_cache_breakpoint(messages, model)

    assert messages[-1].content == [{"type": "text", "text": "Current turn"}, {"type": "image", "source": "x"}]
    assert prepared[-1].content == [
        {"type": "text", "text": "Current turn"},
        {"type": "image", "source": "x", "cache_control": {"type": "ephemeral", "ttl": "1h"}},
    ]


def test_vertexai_prompt_cache_breakpoint_marks_media_added_from_message_images() -> None:
    """Vertex Claude requests should cache through image blocks appended during Claude formatting."""
    model = VertexAIClaude(
        id="claude-sonnet-4-6",
        project_id="demo-project",
        region="us-central1",
        cache_system_prompt=True,
        extended_cache_time=True,
    )
    png_bytes = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO0pQe8AAAAASUVORK5CYII=",
    )
    messages = [
        Message(role="system", content="System prompt"),
        Message(role="user", content="Current turn", images=[Image(content=png_bytes)]),
    ]

    prepared = copy_messages_with_vertex_prompt_cache_breakpoint(messages, model)

    assert messages[-1].images is not None
    assert prepared[-1].images is None
    assert prepared[-1].content == [
        {"type": "text", "text": "Current turn"},
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": base64.b64encode(png_bytes).decode("utf-8"),
            },
            "cache_control": {"type": "ephemeral", "ttl": "1h"},
        },
    ]


def test_vertexai_prompt_cache_breakpoint_handles_file_only_turns() -> None:
    """Vertex Claude requests should still set a cache breakpoint when a user turn is file-only."""
    model = VertexAIClaude(
        id="claude-sonnet-4-6",
        project_id="demo-project",
        region="us-central1",
        cache_system_prompt=True,
    )
    messages = [
        Message(role="system", content="System prompt"),
        Message(
            role="user",
            files=[File(content=b"hello", mime_type="text/plain", filename="note.txt")],
        ),
    ]

    prepared = copy_messages_with_vertex_prompt_cache_breakpoint(messages, model)

    assert messages[-1].files is not None
    assert prepared[-1].files is None
    assert prepared[-1].content == [
        {
            "type": "document",
            "source": {
                "type": "text",
                "media_type": "text/plain",
                "data": "hello",
            },
            "citations": {"enabled": True},
            "cache_control": {"type": "ephemeral"},
        },
    ]


@pytest.mark.asyncio
async def test_vertexai_prompt_cache_hook_rewrites_messages_before_invoke() -> None:
    """The Vertex Claude hook should pass cache-marked messages to Agno."""
    model = VertexAIClaude(
        id="claude-sonnet-4-6",
        project_id="demo-project",
        region="us-central1",
        cache_system_prompt=True,
    )
    captured_messages: list[Message] = []

    async def fake_ainvoke(*args: object, **kwargs: object) -> object:
        del args
        captured_messages.extend(kwargs["messages"])
        return object()

    vars(model)["ainvoke"] = fake_ainvoke
    install_vertex_claude_prompt_cache_hook(model)

    await model.ainvoke(
        messages=[
            Message(role="system", content="System prompt"),
            Message(role="user", content="Current turn"),
        ],
        assistant_message=Message(role="assistant"),
    )

    assert captured_messages[-1].content == [
        {"type": "text", "text": "Current turn", "cache_control": {"type": "ephemeral"}},
    ]


def test_vertexai_claude_loads_runtime_google_application_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Vertex Claude should translate runtime ADC paths into explicit client credentials."""
    config_data = {
        "connections": {
            "vertexai_claude/default": {
                "provider": "vertexai_claude",
                "service": "google_vertex_adc",
                "auth_kind": "google_adc",
            },
        },
        "models": {
            "vertex_claude_model": {
                "provider": "vertexai_claude",
                "id": "claude-sonnet-4@20250514",
                "extra_kwargs": {
                    "project_id": "demo-project",
                    "region": "us-central1",
                },
            },
        },
        "defaults": {
            "markdown": True,
        },
        "router": {
            "model": "vertex_claude_model",
        },
        "memory": {
            "embedder": {
                "provider": "openai",
                "config": {
                    "model": "text-embedding-3-small",
                },
            },
        },
        "agents": {},
    }

    runtime_root = Path(tempfile.mkdtemp())
    credentials_path = runtime_root / "google-credentials.json"
    runtime_paths = resolve_runtime_paths(
        config_path=runtime_root / "config.yaml",
        storage_path=runtime_root / "mindroom_data",
        process_env={},
    )
    config = Config(**config_data)
    get_runtime_shared_credentials_manager(runtime_paths).save_credentials(
        "google_vertex_adc",
        {
            "application_credentials_path": str(credentials_path),
            "_source": "test",
        },
    )
    fake_google_credentials = object()

    def fake_load_credentials_from_file(path: str, *, scopes: list[str]) -> tuple[object, str]:
        assert Path(path).resolve() == credentials_path.resolve()
        assert scopes == ["https://www.googleapis.com/auth/cloud-platform"]
        return fake_google_credentials, "ignored-project"

    monkeypatch.setattr("google.auth.load_credentials_from_file", fake_load_credentials_from_file)

    model = get_model_instance(config, runtime_paths, "vertex_claude_model")

    assert isinstance(model, VertexAIClaude)
    assert model.client_params is not None
    assert model.client_params["credentials"] is fake_google_credentials


def test_get_model_instance_uses_explicit_named_connection_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """LLM consumers should honor an explicit named connection instead of the conventional default."""
    captured: dict[str, object] = {}

    class _FakeOpenAIChat:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)
            self.id = str(kwargs["id"])

    monkeypatch.setattr("mindroom.ai.OpenAIChat", _FakeOpenAIChat)

    config_data = {
        "connections": {
            "openai/default": _api_key_connection("openai"),
            "openai/research": _api_key_connection("openai", service="openai-research"),
        },
        "models": {
            "research": {
                "provider": "openai",
                "id": "gpt-4o-mini",
                "connection": "openai/research",
            },
        },
        "defaults": {"markdown": True},
        "router": {"model": "research"},
        "agents": {},
    }

    config, runtime_paths = _config_with_runtime_paths(config_data)
    _set_api_key(runtime_paths, "openai", "default-key")
    _set_api_key(runtime_paths, "openai-research", "research-key")

    model = get_model_instance(config, runtime_paths, "research")

    assert model.id == "gpt-4o-mini"
    assert captured["api_key"] == "research-key"


def test_model_config_rejects_inline_vertex_client_credentials() -> None:
    """Vertex model configs must not bypass connection routing with inline credentials."""
    with pytest.raises(ValidationError, match="extra_kwargs.client_params.credentials"):
        ModelConfig(
            provider="vertexai_claude",
            id="claude-sonnet-4-6",
            extra_kwargs={"client_params": {"credentials": object()}},
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
