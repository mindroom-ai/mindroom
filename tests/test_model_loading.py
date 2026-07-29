"""Tests for model provider construction."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from agno.models.message import Message as AgnoMessage
from anthropic.types import Message as AnthropicMessage
from anthropic.types import Usage

from mindroom.azure_openai_model import MindRoomAzureOpenAI
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.error_handling import ModelSafeguardRefusalError
from mindroom.model_loading import get_model_instance
from mindroom.openai_models import (
    MindRoomDeepSeek,
    MindRoomLlamaCpp,
    MindRoomOpenAIChat,
    MindRoomOpenAILike,
    MindRoomOpenAIResponses,
    MindRoomOpenRouter,
)
from mindroom.synthetic_model import SyntheticModel
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path


def _safeguard_refusal_message() -> AnthropicMessage:
    return AnthropicMessage(
        id="msg-refusal",
        content=[],
        model="claude-fable-5",
        role="assistant",
        stop_reason="refusal",
        stop_sequence=None,
        type="message",
        usage=Usage(input_tokens=100, output_tokens=4),
    )


def test_first_party_openai_gpt_5_4_and_newer_use_responses(tmp_path: Path) -> None:
    """First-party current GPT uses Responses while old and compatible models keep Chat Completions."""
    config = bind_runtime_paths(
        Config(
            models={
                "current": ModelConfig(provider="openai", id="gpt-5.6", extra_kwargs={"api_key": "dummy-key"}),
                "older": ModelConfig(provider="openai", id="gpt-4o", extra_kwargs={"api_key": "dummy-key"}),
                "compatible": ModelConfig(
                    provider="openai",
                    id="gpt-5.6",
                    extra_kwargs={"api_key": "dummy-key", "base_url": "http://localhost:9292/v1"},
                ),
            },
        ),
        test_runtime_paths(tmp_path),
    )

    current = get_model_instance(config, runtime_paths_for(config), "current")
    older = get_model_instance(config, runtime_paths_for(config), "older")
    compatible = get_model_instance(config, runtime_paths_for(config), "compatible")

    assert isinstance(current, MindRoomOpenAIResponses)
    assert isinstance(older, MindRoomOpenAIChat)
    assert isinstance(compatible, MindRoomOpenAIChat)


def test_openai_wire_providers_use_replay_compatible_models(tmp_path: Path) -> None:
    """Every OpenAI-wire chat provider must use the tool-call replay-compatible subclass."""
    expected = {
        "azure": MindRoomAzureOpenAI,
        "openrouter": MindRoomOpenRouter,
        "zai": MindRoomOpenAILike,
        "deepseek": MindRoomDeepSeek,
        "llama_cpp": MindRoomLlamaCpp,
    }
    config = bind_runtime_paths(
        Config(
            models={
                provider: ModelConfig(provider=provider, id="some-model", extra_kwargs={"api_key": "dummy-key"})
                for provider in expected
            },
        ),
        test_runtime_paths(tmp_path),
    )

    for provider, model_cls in expected.items():
        model = get_model_instance(config, runtime_paths_for(config), provider)
        assert isinstance(model, model_cls), provider


def test_synthetic_provider_loads_without_credentials(tmp_path: Path) -> None:
    """Synthetic models load locally with their configured generation settings."""
    config = bind_runtime_paths(
        Config(
            models={
                "load": ModelConfig(
                    provider="synthetic",
                    id="lorem-ipsum",
                    extra_kwargs={
                        "min_response_chars": 128,
                        "max_response_chars": 128,
                        "chars_per_second": 0,
                    },
                ),
            },
        ),
        test_runtime_paths(tmp_path),
    )

    model = get_model_instance(config, runtime_paths_for(config), "load")

    assert isinstance(model, SyntheticModel)
    assert model.min_response_chars == 128
    assert model.max_response_chars == 128


def test_vertexai_claude_gets_explicit_timeout_so_large_outputs_can_run_non_streaming(tmp_path: Path) -> None:
    """Vertex Claude gets an explicit timeout so large max_tokens can run non-streaming."""
    config = bind_runtime_paths(
        Config(
            models={
                "opus": ModelConfig(
                    provider="vertexai_claude",
                    id="claude-opus-4-8",
                    extra_kwargs={
                        "project_id": "dummy-project",
                        "region": "us-east1",
                        "max_tokens": 32768,
                    },
                ),
            },
        ),
        test_runtime_paths(tmp_path),
    )

    model = get_model_instance(config, runtime_paths_for(config), "opus")

    assert model.timeout == 3600.0


def test_anthropic_gets_explicit_timeout(tmp_path: Path) -> None:
    """Plain Anthropic models get the same explicit timeout default."""
    config = bind_runtime_paths(
        Config(
            models={
                "claude": ModelConfig(
                    provider="anthropic",
                    id="claude-opus-4-8",
                    extra_kwargs={"api_key": "dummy-key"},
                ),
            },
        ),
        test_runtime_paths(tmp_path),
    )

    model = get_model_instance(config, runtime_paths_for(config), "claude")

    assert model.timeout == 3600.0


def test_bedrock_claude_gets_explicit_timeout(tmp_path: Path) -> None:
    """Bedrock Claude uses the same anthropic SDK guard and needs the same explicit timeout."""
    config = bind_runtime_paths(
        Config(
            models={
                "bedrock": ModelConfig(
                    provider="bedrock_claude",
                    id="anthropic.claude-opus-4-8",
                    extra_kwargs={
                        "aws_region": "us-east-1",
                        "aws_access_key": "dummy-access",
                        "aws_secret_key": "dummy-secret",
                    },
                ),
            },
        ),
        test_runtime_paths(tmp_path),
    )

    model = get_model_instance(config, runtime_paths_for(config), "bedrock")

    assert model.timeout == 3600.0


def test_bedrock_current_claude_uses_mantle_endpoint(tmp_path: Path) -> None:
    """Current Bedrock Claude models must use the Mantle Messages endpoint."""
    config = bind_runtime_paths(
        Config(
            models={
                "bedrock": ModelConfig(
                    provider="bedrock_claude",
                    id="anthropic.claude-opus-5",
                    extra_kwargs={
                        "aws_region": "us-east-1",
                        "aws_access_key": "dummy-access",
                        "aws_secret_key": "dummy-secret",
                    },
                ),
            },
        ),
        test_runtime_paths(tmp_path),
    )

    model = get_model_instance(config, runtime_paths_for(config), "bedrock")
    client = model.get_client()

    assert str(client.base_url) == "https://bedrock-mantle.us-east-1.api.aws/anthropic/"


@pytest.mark.parametrize(
    ("provider", "model_id", "extra_kwargs"),
    [
        ("anthropic", "claude-fable-5", {"api_key": "dummy-key"}),
        (
            "bedrock_claude",
            "anthropic.claude-fable-5",
            {
                "aws_region": "us-east-1",
                "aws_access_key": "dummy-access",
                "aws_secret_key": "dummy-secret",
            },
        ),
    ],
)
def test_current_claude_safeguard_refusal_is_not_treated_as_empty_response(
    tmp_path: Path,
    provider: str,
    model_id: str,
    extra_kwargs: dict[str, str],
) -> None:
    """A successful HTTP refusal must surface as a typed terminal error."""
    config = bind_runtime_paths(
        Config(
            models={
                "claude": ModelConfig(
                    provider=provider,
                    id=model_id,
                    extra_kwargs=extra_kwargs,
                ),
            },
        ),
        test_runtime_paths(tmp_path),
    )
    model = get_model_instance(config, runtime_paths_for(config), "claude")

    with pytest.raises(ModelSafeguardRefusalError, match="stop_reason=refusal"):
        model._parse_provider_response(_safeguard_refusal_message())


def test_google_tool_loop_preserves_provider_call_ids(tmp_path: Path) -> None:
    """Gemini 3.6 tool-result requests must retain the originating call ID."""
    config = bind_runtime_paths(
        Config(
            models={
                "gemini": ModelConfig(
                    provider="google",
                    id="gemini-3.6-flash",
                    extra_kwargs={"api_key": "dummy-key"},
                ),
            },
        ),
        test_runtime_paths(tmp_path),
    )
    model = get_model_instance(config, runtime_paths_for(config), "gemini")
    formatted_messages, _system_message = model._format_messages(
        [
            AgnoMessage(
                role="assistant",
                tool_calls=[
                    {
                        "id": "call-123",
                        "type": "function",
                        "function": {"name": "lookup", "arguments": "{}"},
                    },
                ],
            ),
            AgnoMessage(
                role="tool",
                tool_call_id="call-123",
                tool_name="lookup",
                content="result",
            ),
        ],
    )

    function_call = formatted_messages[0].parts[0].function_call
    function_response = formatted_messages[1].parts[0].function_response
    assert function_call is not None
    assert function_call.id == "call-123"
    assert function_response is not None
    assert function_response.id == "call-123"


def test_anthropic_timeout_override_is_preserved(tmp_path: Path) -> None:
    """Explicit Claude timeout config wins over the default."""
    config = bind_runtime_paths(
        Config(
            models={
                "claude": ModelConfig(
                    provider="anthropic",
                    id="claude-opus-4-8",
                    extra_kwargs={
                        "api_key": "dummy-key",
                        "timeout": 120.0,
                    },
                ),
            },
        ),
        test_runtime_paths(tmp_path),
    )

    model = get_model_instance(config, runtime_paths_for(config), "claude")

    assert model.timeout == 120.0


def test_usage_telemetry_is_installed_when_full_request_logging_is_disabled(tmp_path: Path) -> None:
    """Every configured model should get the shared usage telemetry wrapper."""
    config = bind_runtime_paths(
        Config(
            models={
                "default": ModelConfig(
                    provider="openai",
                    id="gpt-5.6",
                    extra_kwargs={"api_key": "dummy-key"},
                ),
            },
        ),
        test_runtime_paths(tmp_path),
    )

    with patch("mindroom.model_loading.install_llm_request_logging") as install_logging:
        model = get_model_instance(config, runtime_paths_for(config), "default")

    install_logging.assert_called_once()
    assert install_logging.call_args.args == (model,)
    assert install_logging.call_args.kwargs["configured_provider"] == "openai"
    assert install_logging.call_args.kwargs["debug_config"].log_llm_requests is False
