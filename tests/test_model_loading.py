"""Tests for model provider construction."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

from mindroom.azure_openai_model import MindRoomAzureOpenAI
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.model_loading import get_model_instance
from mindroom.openai_models import (
    MindRoomDeepSeek,
    MindRoomLlamaCpp,
    MindRoomOpenAIChat,
    MindRoomOpenAILike,
    MindRoomOpenAIResponses,
    MindRoomOpenRouter,
)
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path


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
