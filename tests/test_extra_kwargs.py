"""Test extra_kwargs functionality in model configuration."""

import os
import tempfile
from pathlib import Path

import pytest
import yaml
from agno.models.message import Message
from agno.models.response import ModelResponse
from agno.models.vertexai.claude import Claude as VertexAIClaude
from agno.utils.models.claude import format_messages

from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.model_loading import get_model_instance
from mindroom.vertex_claude_compat import MindroomVertexAIClaude
from mindroom.vertex_claude_compat import _strip_vertex_claude_tool_strict as strip_vertex_claude_tool_strict
from mindroom.vertex_claude_prompt_cache import (
    _copy_messages_with_vertex_prompt_cache_breakpoint as copy_messages_with_vertex_prompt_cache_breakpoint,
)
from mindroom.vertex_claude_prompt_cache import (
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

    sanitized = strip_vertex_claude_tool_strict([tool])

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


def test_vertex_prompt_cache_does_not_poison_plain_string_tool_content() -> None:
    """The hook must leave plain-string tool payloads untouched."""
    model = _vertex_claude_model()
    messages = _tool_turn_messages("ok")

    prepared = copy_messages_with_vertex_prompt_cache_breakpoint(messages, model)

    assert prepared[-1].content == "ok"
    assert messages[-1].content == "ok"
    assert "cache_control" not in str(prepared[-1].content)


def test_vertex_prompt_cache_does_not_mark_list_shaped_tool_content() -> None:
    """The hook must not add cache markers inside tool-result blocks."""
    model = _vertex_claude_model()
    messages = _tool_turn_messages([{"type": "tool_result", "content": "ok"}])

    prepared = copy_messages_with_vertex_prompt_cache_breakpoint(messages, model)

    assert isinstance(prepared[-1].content, list)
    for block in prepared[-1].content:
        assert "cache_control" not in block


def test_vertex_prompt_cache_wire_format_has_no_cache_control_in_tool_results() -> None:
    """Agno wire tool_result payloads must not contain cache-control text."""
    model = _vertex_claude_model()
    prepared = copy_messages_with_vertex_prompt_cache_breakpoint(_tool_turn_messages("ok"), model)

    chat_messages, _system_message = format_messages(prepared, compress_tool_results=True)

    for message in chat_messages:
        for block in message.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                assert "cache_control" not in str(block.get("content") or "")


def test_vertex_prompt_cache_marks_prior_user_when_tail_is_tool() -> None:
    """A trailing tool message should move the cache marker to the prior user turn."""
    model = _vertex_claude_model()
    prepared = copy_messages_with_vertex_prompt_cache_breakpoint(_tool_turn_messages("ok"), model)

    user_message = prepared[1]
    assert isinstance(user_message.content, list)
    assert user_message.content[-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    count = sum(
        1
        for message in prepared
        for block in (message.content if isinstance(message.content, list) else [])
        if isinstance(block, dict) and "cache_control" in block
    )
    assert count == 1


def test_vertexai_prompt_cache_hook_preserves_tool_payloads_with_disabled_compression() -> None:
    """The hooked Vertex Claude invoke path must cache the prior user block without rewriting tool results."""
    model = _vertex_claude_model()
    captured_requests: list[list[dict[str, object]]] = []

    class _FakeMessagesAPI:
        def create(self, *, messages: list[dict[str, object]], **_kwargs: object) -> object:
            captured_requests.append(messages)
            return object()

    class _FakeClient:
        def __init__(self) -> None:
            self.messages = _FakeMessagesAPI()

    vars(model)["get_client"] = lambda: _FakeClient()
    vars(model)["_prepare_request_kwargs"] = lambda *_args, **_kwargs: {}
    vars(model)["_has_beta_features"] = lambda **_kwargs: False
    vars(model)["_parse_provider_response"] = lambda *_args, **_kwargs: ModelResponse(content="ok")
    install_vertex_claude_prompt_cache_hook(model)

    messages = _tool_turn_messages("ok")
    tool_before = messages[-1].to_dict()

    response = model.response(messages=messages, compression_manager=None)

    assert response.content == "ok"
    assert messages[3].to_dict() == tool_before
    assert messages[-1].role == "assistant"
    assert len(captured_requests) == 1
    assert captured_requests[0][0]["content"] == [
        {"type": "text", "text": "Use the tool", "cache_control": {"type": "ephemeral", "ttl": "1h"}},
    ]
    assert captured_requests[0][-1]["content"] == [
        {"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"},
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
        process_env={"GOOGLE_APPLICATION_CREDENTIALS": str(credentials_path)},
    )
    config = Config(**config_data)
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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
