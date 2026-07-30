"""Tests for model provider construction."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from agno.models.message import Message as AgnoMessage
from anthropic.lib.streaming import ParsedMessageStopEvent
from anthropic.types import Message as AnthropicMessage
from anthropic.types import ParsedMessage, Usage

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
                    id="claude-opus-5",
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
                    id="claude-opus-5",
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
    try:
        assert str(client.base_url) == "https://bedrock-mantle.us-east-1.api.aws/anthropic/"
    finally:
        client.close()


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
def test_current_claude_safeguard_refusal_is_terminal_in_all_response_modes(
    tmp_path: Path,
    provider: str,
    model_id: str,
    extra_kwargs: dict[str, str],
) -> None:
    """Successful-HTTP refusals must terminate streaming and non-streaming calls."""
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

    parsed_message = ParsedMessage[object].model_validate(_safeguard_refusal_message().model_dump())
    stop_event = ParsedMessageStopEvent(type="message_stop", message=parsed_message)
    with pytest.raises(ModelSafeguardRefusalError, match="stop_reason=refusal"):
        model._parse_provider_response_delta(stop_event)


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


def test_google_tool_loop_omits_invalid_ids_without_shifting_valid_ids(tmp_path: Path) -> None:
    """Malformed Gemini history must not put invalid or misaligned IDs on the wire."""
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
                        "id": 7,
                        "type": "function",
                        "function": {"name": "invalid", "arguments": "{}"},
                    },
                    {
                        "id": "call-123",
                        "type": "function",
                        "function": {"name": "valid", "arguments": "{}"},
                    },
                ],
            ),
            AgnoMessage(
                role="tool",
                tool_call_id="",
                tool_name="invalid",
                content="invalid result",
            ),
            AgnoMessage(
                role="tool",
                tool_call_id="call-123",
                tool_name="valid",
                content="valid result",
            ),
        ],
    )

    function_calls = [
        part.function_call for message in formatted_messages for part in message.parts if part.function_call is not None
    ]
    function_responses = [
        part.function_response
        for message in formatted_messages
        for part in message.parts
        if part.function_response is not None
    ]
    assert function_calls[0] is not None
    assert function_calls[0].id is None
    assert function_calls[1] is not None
    assert function_calls[1].id == "call-123"
    assert function_responses[0] is not None
    assert function_responses[0].id is None
    assert function_responses[1] is not None
    assert function_responses[1].id == "call-123"


@pytest.mark.parametrize("model_id", ["claude-fable-5", "claude-opus-5", "claude-sonnet-5"])
def test_current_direct_claude_omits_non_default_sampling_controls(tmp_path: Path, model_id: str) -> None:
    """Current Claude requests must omit sampling controls rejected by the provider."""
    config = bind_runtime_paths(
        Config(
            models={
                "claude": ModelConfig(
                    provider="anthropic",
                    id=model_id,
                    extra_kwargs={
                        "api_key": "dummy-key",
                        "temperature": 0.2,
                        "top_p": 0.8,
                        "top_k": 20,
                    },
                ),
            },
        ),
        test_runtime_paths(tmp_path),
    )
    model = get_model_instance(config, runtime_paths_for(config), "claude")

    request_params = model.get_request_params()

    assert "temperature" not in request_params
    assert "top_p" not in request_params
    assert "top_k" not in request_params


@pytest.mark.parametrize("model_id", ["gemini-3.6-flash", "gemini-3.5-flash-lite"])
def test_current_direct_gemini_omits_deprecated_sampling_controls(tmp_path: Path, model_id: str) -> None:
    """Current direct Gemini requests must omit deprecated sampling controls."""
    config = bind_runtime_paths(
        Config(
            models={
                "gemini": ModelConfig(
                    provider="google",
                    id=model_id,
                    extra_kwargs={
                        "api_key": "dummy-key",
                        "temperature": 0.2,
                        "top_p": 0.8,
                        "top_k": 20,
                    },
                ),
            },
        ),
        test_runtime_paths(tmp_path),
    )
    model = get_model_instance(config, runtime_paths_for(config), "gemini")

    request_config = model.get_request_params()["config"]

    assert request_config.temperature is None
    assert request_config.top_p is None
    assert request_config.top_k is None


def test_anthropic_timeout_override_is_preserved(tmp_path: Path) -> None:
    """Explicit Claude timeout config wins over the default."""
    config = bind_runtime_paths(
        Config(
            models={
                "claude": ModelConfig(
                    provider="anthropic",
                    id="claude-opus-5",
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
