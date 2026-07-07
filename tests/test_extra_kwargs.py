"""Test extra_kwargs functionality in model configuration."""

import importlib
import os
import tempfile
from pathlib import Path

import pytest
import yaml
from agno.models.anthropic import Claude
from agno.models.aws.claude import Claude as AwsBedrockClaude
from agno.models.azure import AzureOpenAI
from agno.models.llama_cpp import LlamaCpp
from agno.models.message import Message
from agno.models.openai import OpenAIChat
from agno.models.openai.like import OpenAILike
from agno.models.response import ModelResponse
from agno.models.vertexai.claude import Claude as VertexAIClaude
from agno.utils.models.claude import format_messages
from anthropic.types import Message as AnthropicMessage

from mindroom.claude_prompt_cache import (
    _DEFERRED_TOOL_NAMES_ATTR,
    _MAX_CACHE_MARKERS,
    _count_cache_markers,
    _prompt_cache_control,
    _PromptCacheClientProxy,
    _request_kwargs_with_prompt_cache_ladder,
    install_claude_deferred_tool_search,
    install_claude_prompt_cache_hook,
    native_tool_search_supported,
)
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.model_loading import get_model_instance
from mindroom.startup_errors import PermanentStartupError
from mindroom.vertex_claude_compat import MindroomVertexAIClaude, _strip_vertex_claude_tool_strict


def _config_with_runtime_paths(config_data: dict[str, object]) -> tuple[Config, RuntimePaths]:
    runtime_root = Path(tempfile.mkdtemp())
    runtime_paths = resolve_runtime_paths(
        config_path=runtime_root / "config.yaml",
        storage_path=runtime_root / "mindroom_data",
        process_env={},
    )
    config = Config(**config_data)
    return config, runtime_paths


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
    os.environ["OPENROUTER_API_KEY"] = "test-key"

    config_data = {
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

    # Get the model instance
    model = get_model_instance(config, runtime_paths, "test_model")

    # Check that the model has the correct parameters
    assert model.id == "openai/gpt-4"
    assert model.request_params is not None
    assert model.request_params["provider"]["order"] == ["Cerebras"]
    assert model.request_params["provider"]["allow_fallbacks"] is False

    # Check that temperature was also passed
    assert model.temperature == 0.8


def test_get_model_instance_supports_llama_cpp_provider() -> None:
    """llama.cpp should use Agno's OpenAI-compatible local provider class."""
    config_data = {
        "models": {
            "local_model": {
                "provider": "llama_cpp",
                "id": "gemma-4:31b-q4-uncensored",
                "extra_kwargs": {
                    "api_key": "sk-no-key-required",
                    "base_url": "http://llama.local/v1",
                    "max_tokens": 32000,
                },
            },
        },
        "router": {
            "model": "local_model",
        },
        "agents": {},
    }

    config, runtime_paths = _config_with_runtime_paths(config_data)

    model = get_model_instance(config, runtime_paths, "local_model")

    assert isinstance(model, LlamaCpp)
    assert model.id == "gemma-4:31b-q4-uncensored"
    assert model.api_key == "sk-no-key-required"
    assert model.base_url == "http://llama.local/v1"
    assert model.max_tokens == 32000
    assert model.default_role_map["system"] == "system"


def test_llama_cpp_provider_does_not_auto_fetch_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Custom llama.cpp configs without api_key should not consult shared credentials."""
    config_data = {
        "models": {
            "local_model": {
                "provider": "llama_cpp",
                "id": "gemma-4:31b-q4-uncensored",
                "extra_kwargs": {
                    "base_url": "http://llama.local/v1",
                },
            },
        },
        "router": {
            "model": "local_model",
        },
        "agents": {},
    }
    config, runtime_paths = _config_with_runtime_paths(config_data)
    provider_lookups: list[str] = []

    def get_api_key(provider: str, *, runtime_paths: RuntimePaths) -> str:
        _ = runtime_paths
        provider_lookups.append(provider)
        return "unexpected-credential"

    monkeypatch.setattr("mindroom.model_loading.get_api_key_for_provider", get_api_key)

    model = get_model_instance(config, runtime_paths, "local_model")

    assert isinstance(model, LlamaCpp)
    assert provider_lookups == []
    assert model.api_key == "not-provided"


def test_different_providers_with_extra_kwargs() -> None:
    """Test that extra_kwargs works with different providers."""
    os.environ["OPENAI_API_KEY"] = "test-key"
    os.environ["ANTHROPIC_API_KEY"] = "test-key"

    config_data = {
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
                "id": "claude-opus-4-8",
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
    os.environ["OPENAI_API_KEY"] = "test-key"

    config_data = {
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

    # Should work without any issues
    model = get_model_instance(config, runtime_paths, "simple_model")
    assert model.id == "gpt-3.5-turbo"
    assert model.provider == "OpenAI"


def test_vertexai_claude_provider() -> None:
    """Test native Vertex Claude provider mapping."""
    config_data = {
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
    model = get_model_instance(config, runtime_paths, "vertex_claude_model")

    assert isinstance(model, VertexAIClaude)
    assert isinstance(model, MindroomVertexAIClaude)
    assert model.id == "claude-sonnet-4@20250514"
    assert model.provider == "VertexAI"
    assert model.cache_system_prompt is True
    assert model.extended_cache_time is True


def test_bedrock_claude_provider_uses_runtime_env() -> None:
    """Bedrock Claude provider should create Agno AWS Claude with runtime .env settings."""
    runtime_root = Path(tempfile.mkdtemp())
    env_path = runtime_root / ".env"
    env_path.write_text(
        "AWS_ACCESS_KEY_ID=aws-access\n"
        "AWS_SECRET_ACCESS_KEY=aws-secret\n"
        "AWS_SESSION_TOKEN=aws-session\n"
        "AWS_REGION=us-east-1\n",
        encoding="utf-8",
    )
    runtime_paths = resolve_runtime_paths(
        config_path=runtime_root / "config.yaml",
        storage_path=runtime_root / "mindroom_data",
        process_env={},
    )
    config = Config(
        models={
            "bedrock_model": ModelConfig(
                provider="bedrock_claude",
                id="anthropic.claude-opus-4-8",
                context_window=1_000_000,
            ),
        },
        defaults={"markdown": True},
        router={"model": "bedrock_model"},
        memory={
            "embedder": {
                "provider": "sentence_transformers",
                "config": {"model": "sentence-transformers/all-MiniLM-L6-v2"},
            },
        },
        agents={},
    )

    model = get_model_instance(config, runtime_paths, "bedrock_model")

    assert isinstance(model, AwsBedrockClaude)
    assert model.id == "anthropic.claude-opus-4-8"
    assert model.provider == "AwsBedrock"
    assert model.aws_access_key == "aws-access"
    assert model.aws_secret_key == "aws-secret"  # noqa: S105
    assert model.aws_session_token == "aws-session"  # noqa: S105
    assert model.aws_region == "us-east-1"
    assert model.cache_system_prompt is True
    assert model.extended_cache_time is True


def test_bedrock_claude_provider_respects_explicit_profile_over_env_static_keys(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Bedrock Claude should respect configured aws_profile over env static keys."""
    env_path = tmp_path / ".env"
    env_path.write_text(
        "AWS_ACCESS_KEY_ID=env-access\nAWS_SECRET_ACCESS_KEY=env-secret\nAWS_REGION=us-east-1\n",
        encoding="utf-8",
    )
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={},
    )
    config = Config(
        models={
            "bedrock_model": ModelConfig(
                provider="bedrock_claude",
                id="anthropic.claude-opus-4-8",
                context_window=1_000_000,
                extra_kwargs={"aws_profile": "my-explicit-profile"},
            ),
        },
        defaults={"markdown": True},
        router={"model": "bedrock_model"},
        memory={
            "embedder": {
                "provider": "sentence_transformers",
                "config": {"model": "sentence-transformers/all-MiniLM-L6-v2"},
            },
        },
        agents={},
    )

    class MockSession:
        def __init__(self, **kwargs: object) -> None:
            self.profile_name = kwargs.get("profile_name")
            self.region_name = kwargs.get("region_name")

    monkeypatch.setattr("boto3.session.Session", MockSession)

    model = get_model_instance(config, runtime_paths, "bedrock_model")

    assert isinstance(model, AwsBedrockClaude)
    assert isinstance(model.session, MockSession)
    assert model.session.profile_name == "my-explicit-profile"
    assert model.session.region_name == "us-east-1"
    assert model.aws_access_key is None
    assert model.aws_secret_key is None


def test_bedrock_claude_provider_auto_installs_boto3(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bedrock Claude should use the optional dependency installer instead of a base dependency."""
    config_data = {
        "models": {
            "bedrock_model": {
                "provider": "bedrock_claude",
                "id": "anthropic.claude-opus-4-8",
                "extra_kwargs": {
                    "aws_access_key": "aws-access",
                    "aws_secret_key": "aws-secret",
                    "aws_region": "us-east-1",
                },
            },
        },
        "defaults": {"markdown": True},
        "router": {"model": "bedrock_model"},
        "memory": {
            "embedder": {
                "provider": "sentence_transformers",
                "config": {"model": "sentence-transformers/all-MiniLM-L6-v2"},
            },
        },
        "agents": {},
    }
    config, runtime_paths = _config_with_runtime_paths(config_data)
    calls: list[tuple[list[str], str]] = []

    def ensure(dependencies: list[str], extra_name: str, *_args: object, **_kwargs: object) -> bool:
        calls.append((dependencies, extra_name))
        return False

    monkeypatch.setattr("mindroom.model_loading.ensure_optional_deps", ensure)

    get_model_instance(config, runtime_paths, "bedrock_model")

    assert calls == [(["boto3"], "aws_bedrock")]


def test_azure_openai_provider_uses_runtime_env() -> None:
    """Azure provider should create Agno AzureOpenAI with runtime .env settings."""
    runtime_root = Path(tempfile.mkdtemp())
    env_path = runtime_root / ".env"
    env_path.write_text(
        "AZURE_OPENAI_API_KEY=sk-azure\n"
        "AZURE_OPENAI_ENDPOINT=https://example-resource.openai.azure.com\n"
        "AZURE_OPENAI_API_VERSION=2024-10-21\n",
        encoding="utf-8",
    )
    runtime_paths = resolve_runtime_paths(
        config_path=runtime_root / "config.yaml",
        storage_path=runtime_root / "mindroom_data",
        process_env={},
    )
    config = Config(
        models={
            "azure_model": ModelConfig(
                provider="azure",
                id="team-chat-deployment",
                context_window=258_000,
            ),
        },
        defaults={"markdown": True},
        router={"model": "azure_model"},
        memory={
            "embedder": {
                "provider": "sentence_transformers",
                "config": {"model": "sentence-transformers/all-MiniLM-L6-v2"},
            },
        },
        agents={},
    )

    model = get_model_instance(config, runtime_paths, "azure_model")

    assert isinstance(model, AzureOpenAI)
    assert model.id == "team-chat-deployment"
    assert model.api_key == "sk-azure"
    assert model.azure_endpoint == "https://example-resource.openai.azure.com"
    assert model.api_version == "2024-10-21"


def test_azure_openai_provider_uses_endpoint_file_and_canonical_runtime_env() -> None:
    """Azure provider should support *_FILE endpoint secrets and Azure-specific overrides."""
    runtime_root = Path(tempfile.mkdtemp())
    endpoint_file = runtime_root / "azure-endpoint.txt"
    endpoint_file.write_text("https://file-resource.openai.azure.com\n", encoding="utf-8")
    env_path = runtime_root / ".env"
    env_path.write_text(
        "AZURE_OPENAI_API_KEY=sk-azure\n"
        f"AZURE_OPENAI_ENDPOINT_FILE={endpoint_file}\n"
        "OPENAI_API_VERSION=2023-01-01\n"
        "AZURE_OPENAI_DEPLOYMENT=env-chat-deployment\n",
        encoding="utf-8",
    )
    runtime_paths = resolve_runtime_paths(
        config_path=runtime_root / "config.yaml",
        storage_path=runtime_root / "mindroom_data",
        process_env={},
    )
    config = Config(
        models={
            "azure_model": ModelConfig(
                provider="azure",
                id="config-chat-deployment",
                context_window=258_000,
            ),
        },
        defaults={"markdown": True},
        router={"model": "azure_model"},
        memory={
            "embedder": {
                "provider": "sentence_transformers",
                "config": {"model": "sentence-transformers/all-MiniLM-L6-v2"},
            },
        },
        agents={},
    )

    model = get_model_instance(config, runtime_paths, "azure_model")

    assert isinstance(model, AzureOpenAI)
    assert model.azure_endpoint == "https://file-resource.openai.azure.com"
    assert model.azure_deployment == "env-chat-deployment"
    assert model.api_version != "2023-01-01"


def test_prompt_cache_control_ttl() -> None:
    """Extended cache time selects the 1h TTL; the default omits the ttl field."""
    assert _prompt_cache_control() == {"type": "ephemeral"}
    assert _prompt_cache_control(extended_cache_time=True) == {"type": "ephemeral", "ttl": "1h"}


def _strict_tool_definition() -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": "update_profile",
            "description": "Update profile",
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "strict": {"type": "boolean"},
                },
                "required": ["name"],
                "additionalProperties": False,
            },
        },
    }


def test_strip_vertex_claude_tool_strict_preserves_schema_and_input() -> None:
    """Vertex Claude rejects provider-level strict, but schema fields named strict are valid."""
    tool = _strict_tool_definition()

    sanitized = _strip_vertex_claude_tool_strict([tool])

    assert sanitized is not None
    assert "strict" not in sanitized[0]["function"]
    assert "strict" in sanitized[0]["function"]["parameters"]["properties"]
    assert tool["function"]["strict"] is True


def test_mindroom_vertexai_claude_request_kwargs_strip_tool_strict() -> None:
    """Mindroom's Vertex Claude model should not send strict in the provider tool payload."""
    model = MindroomVertexAIClaude(
        id="claude-sonnet-4-6",
        project_id="demo-project",
        region="us-central1",
        cache_system_prompt=False,
    )

    request_kwargs = model._prepare_request_kwargs("", tools=[_strict_tool_definition()])

    assert request_kwargs["tools"] == [
        {
            "name": "update_profile",
            "description": "Update profile",
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": ""},
                    "strict": {"type": "boolean", "description": ""},
                },
                "required": ["name"],
                "additionalProperties": False,
            },
        },
    ]
    assert model._has_beta_features(tools=[_strict_tool_definition()]) is False


def _vertex_claude_model(*, extended_cache_time: bool = True) -> VertexAIClaude:
    return VertexAIClaude(
        id="claude-sonnet-4-6",
        project_id="demo-project",
        region="us-central1",
        cache_system_prompt=True,
        extended_cache_time=extended_cache_time,
    )


def _tool_turn_messages(tool_content: str | list[dict[str, object]]) -> list[Message]:
    return [
        Message(role="system", content="System prompt"),
        Message(role="user", content="Use the tool"),
        Message(
            role="assistant",
            tool_calls=[
                {
                    "id": "toolu_1",
                    "function": {"name": "demo_tool", "arguments": "{}"},
                },
            ],
        ),
        Message(role="tool", tool_call_id="toolu_1", content=tool_content),
    ]


def test_prompt_cache_ladder_marks_newest_tool_result_prior_user_and_tools() -> None:
    """The ladder should mark the newest tool result, a prior boundary, and the last tool."""
    chat_messages, _system_message = format_messages(_tool_turn_messages("ok"), compress_tool_results=True)
    request_kwargs = {
        "system": [{"type": "text", "text": "System prompt", "cache_control": {"type": "ephemeral", "ttl": "1h"}}],
        "tools": [{"name": "demo_tool", "input_schema": {"type": "object"}}],
        "messages": chat_messages,
    }

    prepared = _request_kwargs_with_prompt_cache_ladder(
        request_kwargs,
        _prompt_cache_control(extended_cache_time=True),
    )

    expected_cache_control = {"type": "ephemeral", "ttl": "1h"}
    tool_result_block = prepared["messages"][-1]["content"][0]
    assert tool_result_block["type"] == "tool_result"
    assert tool_result_block["cache_control"] == expected_cache_control
    assert prepared["messages"][0]["content"][-1]["cache_control"] == expected_cache_control
    assert prepared["tools"][-1]["cache_control"] == expected_cache_control
    assert _count_cache_markers(prepared) == _MAX_CACHE_MARKERS


def test_prompt_cache_ladder_does_not_double_count_existing_message_markers() -> None:
    """A pre-existing message marker must consume budget once, not twice."""
    request_kwargs = {
        "system": [{"type": "text", "text": "S", "cache_control": {"type": "ephemeral"}}],
        "tools": [{"name": "demo_tool", "input_schema": {"type": "object"}}],
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "m1"}]},
            {"role": "user", "content": [{"type": "text", "text": "m2", "cache_control": {"type": "ephemeral"}}]},
            {"role": "user", "content": [{"type": "text", "text": "m3"}]},
        ],
    }

    prepared = _request_kwargs_with_prompt_cache_ladder(request_kwargs, _prompt_cache_control())

    # Budget: 4 total minus system marker minus the pre-existing message
    # marker leaves room for one new rung (on m3) and the tools marker.
    assert prepared["messages"][-1]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in prepared["messages"][0]["content"][0]
    assert prepared["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert _count_cache_markers(prepared) == _MAX_CACHE_MARKERS


def test_prompt_cache_client_proxy_delegates_context_manager() -> None:
    """Context-manager use of the proxied client must reach the real client."""
    events: list[str] = []

    class _FakeClient:
        def __enter__(self) -> object:
            events.append("enter")
            return self

        def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> bool:
            events.append("exit")
            return True

    proxy = _PromptCacheClientProxy(_FakeClient(), _vertex_claude_model())
    with proxy as entered:
        assert entered is proxy
    assert events == ["enter", "exit"]
    # The delegate's __exit__ return value (exception suppression) is preserved.
    assert proxy.__exit__(None, None, None) is True


def test_prompt_cache_ladder_respects_marker_budget() -> None:
    """A request already at the marker limit must pass through unchanged."""
    marked_block = {"type": "text", "text": "x", "cache_control": {"type": "ephemeral"}}
    request_kwargs = {
        "system": [dict(marked_block)],
        "tools": [dict(marked_block)],
        "messages": [
            {"role": "user", "content": [dict(marked_block)]},
            {"role": "user", "content": [dict(marked_block), {"type": "text", "text": "tail"}]},
        ],
    }

    prepared = _request_kwargs_with_prompt_cache_ladder(request_kwargs, _prompt_cache_control())

    assert prepared is request_kwargs
    assert _count_cache_markers(prepared) == _MAX_CACHE_MARKERS


def test_prompt_cache_ladder_skips_unmarkable_blocks() -> None:
    """Thinking blocks, SDK objects, and empty text must not carry cache markers."""
    request_kwargs = {
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "earlier turn"}]},
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "reasoning", "signature": "sig"},
                    {"type": "text", "text": ""},
                ],
            },
        ],
    }

    prepared = _request_kwargs_with_prompt_cache_ladder(request_kwargs, _prompt_cache_control())

    assert "cache_control" not in str(prepared["messages"][1])
    assert prepared["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}


def test_prompt_cache_ladder_does_not_mutate_input() -> None:
    """The ladder must copy structures rather than mutating the caller's request."""
    messages = [{"role": "user", "content": [{"type": "text", "text": "hello"}]}]
    tools = [{"name": "demo_tool", "input_schema": {"type": "object"}}]
    request_kwargs = {"messages": messages, "tools": tools}

    _request_kwargs_with_prompt_cache_ladder(request_kwargs, _prompt_cache_control())

    assert messages == [{"role": "user", "content": [{"type": "text", "text": "hello"}]}]
    assert tools == [{"name": "demo_tool", "input_schema": {"type": "object"}}]


def _install_fake_sync_client(model: VertexAIClaude | Claude) -> list[dict[str, object]]:
    """Install a capturing fake SDK client on a Claude model; return the capture list."""
    captured_kwargs: list[dict[str, object]] = []

    class _FakeMessagesAPI:
        def create(self, **kwargs: object) -> object:
            captured_kwargs.append(kwargs)
            return object()

    class _FakeClient:
        def __init__(self) -> None:
            self.messages = _FakeMessagesAPI()

    vars(model)["get_client"] = lambda: _FakeClient()
    vars(model)["_prepare_request_kwargs"] = lambda *_args, **_kwargs: {}
    vars(model)["_has_beta_features"] = lambda **_kwargs: False
    vars(model)["_parse_provider_response"] = lambda *_args, **_kwargs: ModelResponse(content="ok")
    return captured_kwargs


def test_prompt_cache_hook_applies_ladder_at_wire_level_without_rewriting_messages() -> None:
    """The hooked client must add ladder markers without touching Agno messages or tool payloads."""
    model = _vertex_claude_model()
    captured_kwargs = _install_fake_sync_client(model)
    install_claude_prompt_cache_hook(model)

    messages = _tool_turn_messages("ok")
    tool_before = messages[3].to_dict()

    response = model.response(messages=messages, compression_manager=None)

    assert response.content == "ok"
    assert messages[3].to_dict() == tool_before
    assert len(captured_kwargs) == 1
    wire_messages = captured_kwargs[0]["messages"]
    expected_cache_control = {"type": "ephemeral", "ttl": "1h"}
    assert wire_messages[-1]["content"][0]["type"] == "tool_result"
    assert wire_messages[-1]["content"][0]["cache_control"] == expected_cache_control
    assert wire_messages[-1]["content"][0]["content"] == "ok"
    assert wire_messages[0]["content"] == [
        {"type": "text", "text": "Use the tool", "cache_control": expected_cache_control},
    ]


def test_prompt_cache_hook_inert_when_cache_disabled() -> None:
    """Disabling cache_system_prompt must leave the wire request unmarked."""
    model = _vertex_claude_model()
    model.cache_system_prompt = False
    captured_kwargs = _install_fake_sync_client(model)
    install_claude_prompt_cache_hook(model)

    model.response(messages=_tool_turn_messages("ok"), compression_manager=None)

    wire_messages = captured_kwargs[0]["messages"]
    assert _count_cache_markers({"messages": list(wire_messages)}) == 0


def test_prompt_cache_hook_applies_to_direct_anthropic_claude() -> None:
    """The hook must ladder direct Anthropic Claude models, not only Vertex."""
    model = Claude(id="claude-sonnet-4-6", api_key="test-key", cache_system_prompt=True)
    captured_kwargs = _install_fake_sync_client(model)
    install_claude_prompt_cache_hook(model)

    model.response(
        messages=[Message(role="system", content="System prompt"), Message(role="user", content="Current turn")],
        compression_manager=None,
    )

    wire_messages = captured_kwargs[0]["messages"]
    assert wire_messages[0]["content"] == [
        {"type": "text", "text": "Current turn", "cache_control": {"type": "ephemeral"}},
    ]


@pytest.mark.asyncio
async def test_prompt_cache_hook_wraps_async_client() -> None:
    """The async client path must apply the ladder as well."""
    model = _vertex_claude_model()
    captured_kwargs: list[dict[str, object]] = []

    class _FakeAsyncMessagesAPI:
        async def create(self, **kwargs: object) -> object:
            captured_kwargs.append(kwargs)
            return object()

    class _FakeAsyncClient:
        def __init__(self) -> None:
            self.messages = _FakeAsyncMessagesAPI()

    vars(model)["get_async_client"] = lambda: _FakeAsyncClient()
    vars(model)["_prepare_request_kwargs"] = lambda *_args, **_kwargs: {}
    vars(model)["_has_beta_features"] = lambda **_kwargs: False
    vars(model)["_parse_provider_response"] = lambda *_args, **_kwargs: ModelResponse(content="ok")
    install_claude_prompt_cache_hook(model)

    await model.ainvoke(
        messages=[
            Message(role="system", content="System prompt"),
            Message(role="user", content="Current turn"),
        ],
        assistant_message=Message(role="assistant"),
    )

    assert captured_kwargs[0]["messages"][-1]["content"] == [
        {"type": "text", "text": "Current turn", "cache_control": {"type": "ephemeral", "ttl": "1h"}},
    ]


def _wire_tool(name: str) -> dict[str, object]:
    return {"name": name, "description": f"{name} description", "input_schema": {"type": "object"}}


@pytest.mark.parametrize(
    ("provider", "model_id", "expected"),
    [
        ("anthropic", "claude-opus-4-8", True),
        ("anthropic", "claude-sonnet-5", True),
        ("Anthropic", "claude-sonnet-4-5-20250929", True),
        ("vertexai_claude", "claude-haiku-4-5@20251001", True),
        # Unreleased Claude models default to the native path (denylist gating).
        ("anthropic", "claude-opus-4-9", True),
        ("anthropic", "claude-fable-6", True),
        ("anthropic", "claude-opus-4-1", False),
        ("anthropic", "claude-opus-4-20250514", False),
        ("anthropic", "claude-3-5-sonnet-20241022", False),
        ("vertexai_claude", "claude-sonnet-4@20250514", False),
        ("openai", "gpt-5.5", False),
        ("bedrock_claude", "anthropic.claude-opus-4-8", False),
    ],
)
def test_native_tool_search_supported_gating(provider: str, model_id: str, *, expected: bool) -> None:
    """Claude-family providers qualify unless the model predates tool search."""
    assert native_tool_search_supported(provider, model_id) is expected


def test_deferred_tool_search_tags_tools_and_injects_search_tool() -> None:
    """Deferred tools ship tagged and name-sorted after the search tool and non-deferred tools."""
    model = Claude(id="claude-opus-4-8", api_key="test-key", cache_system_prompt=True)
    captured_kwargs = _install_fake_sync_client(model)
    # zeta_tool arrives pre-marked (as Agno's cache_tools flag would): the
    # marker must be stripped because deferred tools may not carry one.
    tools = [
        _wire_tool("always_tool"),
        {**_wire_tool("zeta_tool"), "cache_control": {"type": "ephemeral"}},
        _wire_tool("alpha_tool"),
    ]
    vars(model)["_prepare_request_kwargs"] = lambda *_args, **_kwargs: {"tools": [dict(tool) for tool in tools]}
    install_claude_deferred_tool_search(model, deferred_tool_names=frozenset({"zeta_tool", "alpha_tool"}))

    model.response(messages=[Message(role="user", content="hi")], compression_manager=None)

    wire_tools = captured_kwargs[0]["tools"]
    assert [tool.get("name") for tool in wire_tools] == [
        "tool_search_tool_regex",
        "always_tool",
        "alpha_tool",
        "zeta_tool",
    ]
    assert wire_tools[0] == {"type": "tool_search_tool_regex_20251119", "name": "tool_search_tool_regex"}
    # The cache-ladder marker must land on the last non-deferred tool: deferred
    # tools may not carry cache_control (the API rejects the request).
    assert wire_tools[1]["cache_control"] == {"type": "ephemeral"}
    for deferred_tool in wire_tools[2:]:
        assert deferred_tool["defer_loading"] is True
        assert "cache_control" not in deferred_tool


def test_deferred_tool_search_skips_tools_marker_when_all_tools_deferred() -> None:
    """With every authored tool deferred, no tools marker is emitted at all.

    Whether the API accepts cache_control on the search-tool type is
    unverified, and deferred tools may never carry one, so the ladder leaves
    the tools array unmarked and relies on the system-prompt breakpoint.
    """
    model = Claude(id="claude-opus-4-8", api_key="test-key", cache_system_prompt=True)
    captured_kwargs = _install_fake_sync_client(model)
    vars(model)["_prepare_request_kwargs"] = lambda *_args, **_kwargs: {"tools": [_wire_tool("alpha_tool")]}
    install_claude_deferred_tool_search(model, deferred_tool_names=frozenset({"alpha_tool"}))

    model.response(messages=[Message(role="user", content="hi")], compression_manager=None)

    wire_tools = captured_kwargs[0]["tools"]
    assert wire_tools[0]["name"] == "tool_search_tool_regex"
    assert wire_tools[1]["defer_loading"] is True
    assert _count_cache_markers({"tools": list(wire_tools)}) == 0


def test_deferred_tool_search_applies_without_cache_ladder_when_cache_disabled() -> None:
    """Deferred tagging is independent of the cache ladder gate."""
    model = Claude(id="claude-opus-4-8", api_key="test-key", cache_system_prompt=False)
    captured_kwargs = _install_fake_sync_client(model)
    vars(model)["_prepare_request_kwargs"] = lambda *_args, **_kwargs: {"tools": [_wire_tool("alpha_tool")]}
    install_claude_deferred_tool_search(model, deferred_tool_names=frozenset({"alpha_tool"}))

    model.response(messages=[Message(role="user", content="hi")], compression_manager=None)

    request = captured_kwargs[0]
    wire_tools = request["tools"]
    assert wire_tools[0]["name"] == "tool_search_tool_regex"
    assert wire_tools[1]["defer_loading"] is True
    assert _count_cache_markers({"tools": list(wire_tools), "messages": list(request["messages"])}) == 0


def test_deferred_tool_search_leaves_requests_without_matching_tools_unchanged() -> None:
    """The search tool is injected only when a deferred tool is present in the request."""
    model = Claude(id="claude-opus-4-8", api_key="test-key", cache_system_prompt=True)
    captured_kwargs = _install_fake_sync_client(model)
    vars(model)["_prepare_request_kwargs"] = lambda *_args, **_kwargs: {"tools": [_wire_tool("always_tool")]}
    install_claude_deferred_tool_search(model, deferred_tool_names=frozenset({"other_tool"}))

    model.response(messages=[Message(role="user", content="hi")], compression_manager=None)

    assert [tool["name"] for tool in captured_kwargs[0]["tools"]] == ["always_tool"]


def test_install_claude_deferred_tool_search_ignores_non_claude_and_empty_sets() -> None:
    """The installer is a no-op for non-Claude models and empty name sets."""
    llama = LlamaCpp(id="gemma", api_key="test-key", base_url="http://llama.local/v1")
    install_claude_deferred_tool_search(llama, deferred_tool_names=frozenset({"alpha_tool"}))
    assert _DEFERRED_TOOL_NAMES_ATTR not in vars(llama)

    claude = Claude(id="claude-opus-4-8", api_key="test-key", cache_system_prompt=False)
    install_claude_deferred_tool_search(claude, deferred_tool_names=frozenset())
    assert _DEFERRED_TOOL_NAMES_ATTR not in vars(claude)


_SERVER_TOOL_USE_BLOCK = {
    "type": "server_tool_use",
    "id": "srvtoolu_01ABC",
    "name": "tool_search_tool_regex",
    "input": {"pattern": "weather"},
}
_TOOL_SEARCH_RESULT_BLOCK = {
    "type": "tool_search_tool_result",
    "tool_use_id": "srvtoolu_01ABC",
    "content": {
        "type": "tool_search_tool_search_result",
        "tool_references": [{"type": "tool_reference", "tool_name": "get_weather"}],
    },
}


def _anthropic_response(content: list[dict[str, object]]) -> AnthropicMessage:
    return AnthropicMessage.model_validate(
        {
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": "claude-opus-4-8",
            "content": content,
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    )


def test_server_tool_search_blocks_round_trip_in_assistant_history() -> None:
    """server_tool_use and tool_search_tool_result replay verbatim, in order, exactly once."""
    model = Claude(id="claude-opus-4-8", api_key="test-key", cache_system_prompt=False)
    first_response = _anthropic_response(
        [
            {"type": "text", "text": "I'll search for a weather tool."},
            _SERVER_TOOL_USE_BLOCK,
            _TOOL_SEARCH_RESULT_BLOCK,
        ],
    )
    responses = iter([first_response, _anthropic_response([{"type": "text", "text": "Done."}])])
    captured_kwargs: list[dict[str, object]] = []

    class _FakeMessagesAPI:
        def create(self, **kwargs: object) -> object:
            captured_kwargs.append(kwargs)
            return next(responses)

    class _FakeClient:
        def __init__(self) -> None:
            self.messages = _FakeMessagesAPI()

    vars(model)["get_client"] = lambda: _FakeClient()

    messages = [Message(role="user", content="What is the weather?")]
    model.response(messages=messages, compression_manager=None)
    model.response(messages=messages, compression_manager=None)

    assistant_wires = [message for message in captured_kwargs[1]["messages"] if message["role"] == "assistant"]
    assert len(assistant_wires) == 1
    replayed_dict_blocks = [block for block in assistant_wires[0]["content"] if isinstance(block, dict)]
    assert replayed_dict_blocks == [
        first_response.content[1].model_dump(),
        first_response.content[2].model_dump(),
    ]


def test_server_tool_blocks_replay_to_non_anthropic_provider_without_crashing() -> None:
    """History stored on the native path must stay replayable after a `!model` provider switch."""
    assistant = Message(
        role="assistant",
        content="I'll search for a weather tool.",
        provider_data={"server_tool_blocks": [dict(_SERVER_TOOL_USE_BLOCK), dict(_TOOL_SEARCH_RESULT_BLOCK)]},
    )

    formatted = OpenAIChat(id="gpt-5.5", api_key="test-key")._format_message(assistant)

    assert formatted["role"] == "assistant"
    assert formatted["content"] == "I'll search for a weather tool."


def test_vertexai_claude_loads_runtime_google_application_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Vertex Claude should translate runtime ADC paths into explicit client credentials."""
    config_data = {
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
    credentials_path.write_text('{"type":"authorized_user"}\n', encoding="utf-8")
    runtime_paths = resolve_runtime_paths(
        config_path=runtime_root / "config.yaml",
        storage_path=runtime_root / "mindroom_data",
        process_env={"GOOGLE_APPLICATION_CREDENTIALS": str(credentials_path)},
    )
    config = Config(**config_data)
    fake_google_credentials = object()
    import_order: list[str] = []

    class FakeAuthorizedUserCredentials:
        @classmethod
        def from_authorized_user_file(cls, path: str, *, scopes: list[str]) -> object:
            assert Path(path).resolve() == credentials_path.resolve()
            assert scopes == ["https://www.googleapis.com/auth/cloud-platform"]
            return fake_google_credentials

    original_import_module = importlib.import_module

    def fake_import_module(module_name: str) -> object:
        import_order.append(module_name)
        if module_name == "google.auth":
            msg = "google.auth should not be used for authorized-user ADC"
            raise AssertionError(msg)
        if module_name == "google.oauth2.credentials":
            return type("FakeOauthCredentialsModule", (), {"Credentials": FakeAuthorizedUserCredentials})
        return original_import_module(module_name)

    monkeypatch.setattr("mindroom.google_adc.importlib.import_module", fake_import_module)

    model = get_model_instance(config, runtime_paths, "vertex_claude_model")

    assert isinstance(model, VertexAIClaude)
    assert model.client_params is not None
    assert model.client_params["credentials"] is fake_google_credentials
    assert import_order == ["google.oauth2.credentials"]


def test_vertexai_claude_rejects_missing_runtime_google_application_credentials() -> None:
    """Missing GOOGLE_APPLICATION_CREDENTIALS should fail with an actionable error."""
    config_data = {
        "models": {
            "vertex_claude_model": {
                "provider": "vertexai_claude",
                "id": "claude-sonnet-4-6",
                "extra_kwargs": {
                    "project_id": "demo-project",
                    "region": "us-central1",
                },
            },
        },
        "router": {
            "model": "vertex_claude_model",
        },
        "agents": {},
    }

    runtime_root = Path(tempfile.mkdtemp())
    credentials_path = runtime_root / "missing-google-credentials.json"
    runtime_paths = resolve_runtime_paths(
        config_path=runtime_root / "config.yaml",
        storage_path=runtime_root / "mindroom_data",
        process_env={"GOOGLE_APPLICATION_CREDENTIALS": str(credentials_path)},
    )
    config = Config(**config_data)

    with pytest.raises(
        PermanentStartupError,
        match="GOOGLE_APPLICATION_CREDENTIALS points to a file that does not exist",
    ):
        get_model_instance(config, runtime_paths, "vertex_claude_model")


def test_vertexai_claude_rejects_invalid_runtime_google_application_credentials() -> None:
    """Invalid ADC files should fail as permanent startup errors."""
    config_data = {
        "models": {
            "vertex_claude_model": {
                "provider": "vertexai_claude",
                "id": "claude-sonnet-4-6",
                "extra_kwargs": {
                    "project_id": "demo-project",
                    "region": "us-central1",
                },
            },
        },
        "router": {
            "model": "vertex_claude_model",
        },
        "agents": {},
    }

    runtime_root = Path(tempfile.mkdtemp())
    credentials_path = runtime_root / "invalid-google-credentials.json"
    credentials_path.write_text('{"type":"authorized_user"}\n', encoding="utf-8")
    runtime_paths = resolve_runtime_paths(
        config_path=runtime_root / "config.yaml",
        storage_path=runtime_root / "mindroom_data",
        process_env={"GOOGLE_APPLICATION_CREDENTIALS": str(credentials_path)},
    )
    config = Config(**config_data)

    with pytest.raises(PermanentStartupError, match="Failed to load GOOGLE_APPLICATION_CREDENTIALS"):
        get_model_instance(config, runtime_paths, "vertex_claude_model")


def test_vertexai_claude_loads_service_account_credentials_directly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Service-account ADC should avoid google-auth's authorized-user imports."""
    config_data = {
        "models": {
            "vertex_claude_model": {
                "provider": "vertexai_claude",
                "id": "claude-sonnet-4-6",
                "extra_kwargs": {
                    "project_id": "demo-project",
                    "region": "us-central1",
                },
            },
        },
        "router": {
            "model": "vertex_claude_model",
        },
        "agents": {},
    }

    runtime_root = Path(tempfile.mkdtemp())
    credentials_path = runtime_root / "google-credentials.json"
    credentials_path.write_text('{"type":"service_account"}\n', encoding="utf-8")
    runtime_paths = resolve_runtime_paths(
        config_path=runtime_root / "config.yaml",
        storage_path=runtime_root / "mindroom_data",
        process_env={"GOOGLE_APPLICATION_CREDENTIALS": str(credentials_path)},
    )
    config = Config(**config_data)
    fake_google_credentials = object()
    import_order: list[str] = []

    class FakeServiceAccountCredentials:
        @classmethod
        def from_service_account_file(cls, path: str, *, scopes: list[str]) -> object:
            assert Path(path).resolve() == credentials_path.resolve()
            assert scopes == ["https://www.googleapis.com/auth/cloud-platform"]
            return fake_google_credentials

    original_import_module = importlib.import_module

    def fake_import_module(module_name: str) -> object:
        import_order.append(module_name)
        if module_name == "google.auth":
            msg = "google.auth should not be used for service-account ADC"
            raise AssertionError(msg)
        if module_name == "google.oauth2.service_account":
            return type("FakeServiceAccountModule", (), {"Credentials": FakeServiceAccountCredentials})
        return original_import_module(module_name)

    monkeypatch.setattr("mindroom.google_adc.importlib.import_module", fake_import_module)

    model = get_model_instance(config, runtime_paths, "vertex_claude_model")

    assert isinstance(model, VertexAIClaude)
    assert model.client_params is not None
    assert model.client_params["credentials"] is fake_google_credentials
    assert import_order == ["google.oauth2.service_account"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


def test_get_model_instance_supports_zai_provider() -> None:
    """Z.ai should use Agno's OpenAI-compatible provider class with the Z.ai base URL."""
    config_data = {
        "models": {
            "glm": {
                "provider": "zai",
                "id": "glm-5.2",
                "extra_kwargs": {"api_key": "test-zai-key"},
            },
            "glm_custom": {
                "provider": "zai",
                "id": "glm-5.2",
                "extra_kwargs": {
                    "api_key": "test-zai-key",
                    "base_url": "https://open.bigmodel.cn/api/paas/v4",
                },
            },
        },
        "router": {
            "model": "glm",
        },
        "agents": {},
    }

    config, runtime_paths = _config_with_runtime_paths(config_data)

    model = get_model_instance(config, runtime_paths, "glm")
    assert isinstance(model, OpenAILike)
    assert model.id == "glm-5.2"
    assert model.api_key == "test-zai-key"
    assert model.base_url == "https://api.z.ai/api/paas/v4"
    assert model.name == "ZAI"
    assert model.provider == "ZAI"

    custom_model = get_model_instance(config, runtime_paths, "glm_custom")
    assert custom_model.base_url == "https://open.bigmodel.cn/api/paas/v4"
