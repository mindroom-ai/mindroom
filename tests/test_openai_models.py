"""Tests for MindRoom's OpenAI-wire model subclasses."""

from __future__ import annotations

import pytest
from agno.models.azure.openai_chat import AzureOpenAI
from agno.models.deepseek import DeepSeek
from agno.models.llama_cpp import LlamaCpp
from agno.models.message import Message
from agno.models.openai import OpenAIChat
from agno.models.openai.like import OpenAILike
from agno.models.openrouter import OpenRouter

from mindroom.azure_openai_model import MindRoomAzureOpenAI
from mindroom.openai_models import (
    MindRoomDeepSeek,
    MindRoomLlamaCpp,
    MindRoomOpenAIChat,
    MindRoomOpenAILike,
    MindRoomOpenAIResponses,
    MindRoomOpenRouter,
)

_CHAT_WIRE_PAIRS = [
    (MindRoomOpenAIChat, OpenAIChat),
    (MindRoomOpenAILike, OpenAILike),
    (MindRoomAzureOpenAI, AzureOpenAI),
    (MindRoomOpenRouter, OpenRouter),
    (MindRoomDeepSeek, DeepSeek),
    (MindRoomLlamaCpp, LlamaCpp),
]


def _assistant_with_argumentless_tool_call() -> Message:
    """Anthropic saves zero-argument tool calls without a function.arguments field."""
    return Message(
        role="assistant",
        tool_calls=[
            {
                "id": "toolu_1",
                "type": "function",
                "function": {"name": "get_status"},
            },
        ],
    )


@pytest.mark.parametrize(("model_cls", "_agno_cls"), _CHAT_WIRE_PAIRS)
def test_chat_models_supply_missing_tool_arguments_without_mutating_history(
    model_cls: type[OpenAIChat],
    _agno_cls: type[OpenAIChat],
) -> None:
    """Chat Completions replay must repair zero-argument calls from another provider."""
    assistant = _assistant_with_argumentless_tool_call()

    formatted = model_cls(id="gpt-5.6", api_key="test-key")._format_all_messages([assistant])

    assert formatted[0]["tool_calls"][0]["function"]["arguments"] == "{}"
    assert "arguments" not in assistant.tool_calls[0]["function"]


@pytest.mark.parametrize(("model_cls", "agno_cls"), _CHAT_WIRE_PAIRS)
def test_chat_models_preserve_provider_dataclass_defaults(
    model_cls: type[OpenAIChat],
    agno_cls: type[OpenAIChat],
) -> None:
    """The compat mixin must not re-apply OpenAIChat defaults over provider-specific ones."""
    ours = model_cls(api_key="test-key")
    theirs = agno_cls(api_key="test-key")

    assert (ours.id, ours.name, ours.provider, ours.base_url, ours.max_tokens) == (
        theirs.id,
        theirs.name,
        theirs.provider,
        theirs.base_url,
        theirs.max_tokens,
    )


def test_openai_responses_supplies_missing_tool_arguments_without_mutating_history() -> None:
    """Responses replay must repair zero-argument calls from another provider."""
    assistant = _assistant_with_argumentless_tool_call()

    formatted = MindRoomOpenAIResponses(id="gpt-5.6", api_key="test-key")._format_messages([assistant])

    assert formatted[0]["arguments"] == "{}"
    assert "arguments" not in assistant.tool_calls[0]["function"]
