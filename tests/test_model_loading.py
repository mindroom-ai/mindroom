"""Tests for model provider construction."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal
from unittest.mock import patch

import pytest
from agno.models.message import Message as AgnoMessage
from anthropic.lib.streaming import ParsedMessageStopEvent
from anthropic.types import Message as AnthropicMessage
from anthropic.types import ParsedMessage, Usage
from pydantic import ValidationError

from mindroom.azure_openai_model import MindRoomAzureOpenAI
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.credentials import get_runtime_shared_credentials_manager
from mindroom.error_handling import ModelSafeguardRefusalError
from mindroom.model_loading import get_model_instance, missing_model_api_key_provider
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
        model="claude-fable-5-1",
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
                "current": ModelConfig(provider="openai", id="gpt-6-astra", extra_kwargs={"api_key": "dummy-key"}),
                "older": ModelConfig(provider="openai", id="gpt-4o", extra_kwargs={"api_key": "dummy-key"}),
                "compatible": ModelConfig(
                    provider="openai",
                    id="gpt-6-astra",
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


def test_custom_openai_endpoint_requires_explicit_responses_selection(tmp_path: Path) -> None:
    """A model name must not opt a compatible endpoint into a different API."""
    config = bind_runtime_paths(
        Config(
            models={
                "astra": ModelConfig(
                    provider="openai",
                    id="gpt-6-astra",
                    extra_kwargs={"api_key": "dummy-key", "base_url": "http://localhost:9292/v1"},
                ),
            },
        ),
        test_runtime_paths(tmp_path),
    )

    model = get_model_instance(config, runtime_paths_for(config), "astra")

    assert isinstance(model, MindRoomOpenAIChat)


@pytest.mark.parametrize(
    ("model_id", "api", "base_url", "expected_class"),
    [
        ("reasoning-alias", "responses", "http://localhost:9292/v1", MindRoomOpenAIResponses),
        ("gpt-6-astra", "responses", "http://localhost:9292/v1", MindRoomOpenAIResponses),
        ("gpt-6-astra", "chat_completions", None, MindRoomOpenAIChat),
        ("gpt-6-astra", "chat_completions", "http://localhost:9292/v1", MindRoomOpenAIChat),
    ],
)
def test_explicit_openai_api_overrides_model_and_endpoint_defaults(
    tmp_path: Path,
    model_id: str,
    api: Literal["responses", "chat_completions"],
    base_url: str | None,
    expected_class: type,
) -> None:
    """An endpoint's configured API must win over model-name inference."""
    config = Config(
        models={
            "default": ModelConfig(
                provider="openai",
                id=model_id,
                api=api,
                extra_kwargs={"api_key": "dummy-key", "base_url": base_url},
            ),
        },
    )
    model = get_model_instance(config, test_runtime_paths(tmp_path))
    assert isinstance(model, expected_class)


@pytest.mark.parametrize(("provider", "api"), [("openai", "invalid"), ("anthropic", "responses")])
def test_model_api_rejects_invalid_values_and_unsupported_providers(provider: str, api: str) -> None:
    """A transport selection must not be silently ignored or misspelled."""
    with pytest.raises(ValidationError):
        ModelConfig.model_validate({"provider": provider, "id": "test-model", "api": api})


@pytest.mark.parametrize(
    ("model_fields", "dashboard_key", "expected_key"),
    [
        ({}, None, "sk-shared"),
        ({"api_key": " sk-config "}, None, "sk-config"),
        ({"extra_kwargs": {"api_key": " sk-config "}}, None, "sk-config"),
        ({"api_key": "  ", "extra_kwargs": {"api_key": ""}}, None, "sk-shared"),
        ({"api_key": "sk-config"}, "sk-dashboard", "sk-dashboard"),
    ],
    ids=["shared", "api-key", "extra-kwargs-api-key", "blank-is-unset", "dashboard-key-wins"],
)
def test_model_api_key_precedence(
    tmp_path: Path,
    model_fields: dict[str, object],
    dashboard_key: str | None,
    expected_key: str,
) -> None:
    """The dashboard model key beats a configured key, which beats the provider's shared key."""
    runtime_paths = test_runtime_paths(tmp_path)
    credentials = get_runtime_shared_credentials_manager(runtime_paths)
    credentials.save_credentials("openai", {"api_key": "sk-shared"})
    if dashboard_key is not None:
        credentials.save_credentials("model:target", {"api_key": dashboard_key})
    config = Config(models={"target": ModelConfig(provider="openai", id="gpt-6-astra", **model_fields)})

    model = get_model_instance(bind_runtime_paths(config, runtime_paths), runtime_paths, "target")

    assert model.api_key == expected_key


@pytest.mark.parametrize(
    ("provider", "model_id"),
    [("openrouter", "z-ai/glm-5.3"), ("zai", "glm-5.3"), ("ollama", "qwen3.8:27b"), ("llama_cpp", "qwen")],
)
def test_configured_api_key_reaches_providers_with_own_key_handling(
    tmp_path: Path,
    provider: str,
    model_id: str,
) -> None:
    """Provider branches that resolve or skip the shared key still send the configured one."""
    runtime_paths = test_runtime_paths(tmp_path)
    get_runtime_shared_credentials_manager(runtime_paths).save_credentials(provider, {"api_key": "sk-shared"})
    config = Config(models={"target": ModelConfig(provider=provider, id=model_id, api_key="sk-config")})

    model = get_model_instance(bind_runtime_paths(config, runtime_paths), runtime_paths, "target")

    assert model.api_key == "sk-config"


def test_missing_model_api_key_provider_honors_model_credentials(tmp_path: Path) -> None:
    """First-run provider setup must not ask for a shared key a model does not use."""
    runtime_paths = test_runtime_paths(tmp_path)
    get_runtime_shared_credentials_manager(runtime_paths).save_credentials("model:dashboard", {"api_key": "sk-ui"})
    config = Config(
        models={
            "keyed": ModelConfig(provider="openai", id="gpt-6-astra", api_key="sk-config"),
            "dashboard": ModelConfig(provider="openai", id="gpt-6-astra"),
            "token": ModelConfig(provider="anthropic", id="claude-sonnet-5", extra_kwargs={"auth_token": "tok"}),
            "unkeyed": ModelConfig(provider="openai", id="gpt-6-astra"),
        },
    )

    assert [name for name in config.models if missing_model_api_key_provider(config, runtime_paths, name)] == [
        "unkeyed",
    ]


def test_alternative_auth_model_never_gets_the_shared_key(tmp_path: Path) -> None:
    """A model that authenticates with Anthropic's auth_token must not also send the shared API key."""
    runtime_paths = test_runtime_paths(tmp_path)
    get_runtime_shared_credentials_manager(runtime_paths).save_credentials("anthropic", {"api_key": "sk-shared"})
    model_config = ModelConfig(provider="anthropic", id="claude-sonnet-5", extra_kwargs={"auth_token": "tok"})
    config = bind_runtime_paths(Config(models={"target": model_config}), runtime_paths)

    model = get_model_instance(config, runtime_paths, "target")

    assert model.api_key is None


def test_model_config_rejects_api_key_in_both_fields_without_echoing_keys() -> None:
    """Two configured keys would leave one unused, and the error must not print either."""
    model = {"provider": "openai", "id": "model", "api_key": "sk-secret1", "extra_kwargs": {"api_key": "sk-secret2"}}
    with pytest.raises(ValidationError, match=r"either api_key or extra_kwargs\.api_key") as exc_info:
        Config.model_validate({"models": {"default": model}})

    assert "sk-secret" not in str(exc_info.value)


@pytest.mark.parametrize("api_key", [123, True, ["sk-a"]])
def test_model_config_rejects_non_string_extra_kwargs_api_key(api_key: object) -> None:
    """A non-string key (for example YAML ``yes``) must fail validation instead of reaching the provider."""
    with pytest.raises(ValidationError, match=r"extra_kwargs\.api_key must be a string"):
        ModelConfig(provider="openai", id="gpt-6-astra", extra_kwargs={"api_key": api_key})


@pytest.mark.parametrize(
    ("provider", "model_id", "extra_kwargs"),
    [
        ("codex", "gpt-6-astra", {}),
        ("kimi", "k3", {}),
        ("synthetic", "lorem-ipsum", {}),
        ("vertexai_claude", "claude-opus-5", {"project_id": "p", "region": "us-east1"}),
        (
            "bedrock_claude",
            "anthropic.claude-opus-5",
            {"aws_region": "us-east-1", "aws_access_key": "a", "aws_secret_key": "b"},
        ),
    ],
)
def test_keyless_providers_ignore_configured_api_key(
    tmp_path: Path,
    provider: str,
    model_id: str,
    extra_kwargs: dict[str, object],
) -> None:
    """Providers that authenticate without an API key never carry a configured one."""
    runtime_paths = test_runtime_paths(tmp_path)
    model_config = ModelConfig(provider=provider, id=model_id, api_key="sk-config", extra_kwargs=extra_kwargs)
    config = bind_runtime_paths(Config(models={"target": model_config}), runtime_paths)

    model = get_model_instance(config, runtime_paths, "target")

    assert getattr(model, "api_key", None) is None


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
        ("anthropic", "claude-fable-5-1", {"api_key": "dummy-key"}),
        (
            "bedrock_claude",
            "anthropic.claude-fable-5-1",
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
                    id="gemini-3.8-flash",
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
                    id="gemini-3.8-flash",
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


@pytest.mark.parametrize(
    "model_id",
    ["claude-fable-5-1", "claude-fable-5", "claude-opus-5", "claude-sonnet-5"],
)
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


@pytest.mark.parametrize("model_id", ["gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite"])
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


def test_models_from_one_entry_do_not_share_authored_generation_config(tmp_path: Path) -> None:
    """Agno writes each Gemini request into an authored generation_config dict, so each model needs its own."""
    config = bind_runtime_paths(
        Config(
            models={
                "gemini": ModelConfig(
                    provider="google",
                    id="gemini-3.8-flash",
                    extra_kwargs={"api_key": "dummy-key", "generation_config": {"max_output_tokens": 256}},
                ),
            },
        ),
        test_runtime_paths(tmp_path),
    )
    agent_a = get_model_instance(config, runtime_paths_for(config), "gemini")
    agent_b = get_model_instance(config, runtime_paths_for(config), "gemini")
    tool = {"type": "function", "function": {"name": "agent_a_tool", "parameters": {"type": "object"}}}

    agent_a.get_request_params(system_message="Agent A.", tools=[tool], tool_choice="auto")
    request_config = agent_b.get_request_params(system_message="Agent B.")["config"]

    assert request_config.tools is None
    assert request_config.tool_config is None
    assert config.models["gemini"].extra_kwargs["generation_config"] == {"max_output_tokens": 256}


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
                    id="gpt-6-astra",
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
