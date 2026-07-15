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


def _legacy_gemini_combined_tool_results() -> Message:
    """Older Gemini histories bundle multiple tool results into one tool message."""
    return Message(
        role="tool",
        content=["first result", "second result"],
        tool_calls=[
            {
                "tool_call_id": "toolu_1",
                "tool_name": "first_tool",
                "content": "first result",
            },
            {
                "tool_call_id": "toolu_2",
                "tool_name": "second_tool",
                "content": "second result",
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


@pytest.mark.parametrize(("model_cls", "_agno_cls"), _CHAT_WIRE_PAIRS)
def test_chat_models_leave_legacy_combined_tool_results_for_agno_normalization(
    model_cls: type[OpenAIChat],
    _agno_cls: type[OpenAIChat],
) -> None:
    """Argument repair must not interpret old Gemini tool-result bundles as function calls."""
    tool_results = _legacy_gemini_combined_tool_results()

    formatted = model_cls(id="gpt-5.6", api_key="test-key")._format_all_messages([tool_results])

    assert formatted == [
        {"role": "tool", "content": "first result", "tool_call_id": "toolu_1"},
        {"role": "tool", "content": "second result", "tool_call_id": "toolu_2"},
    ]


def test_openai_responses_leaves_legacy_combined_tool_results_for_agno_normalization() -> None:
    """Responses replay must preserve Agno's existing old-Gemini normalization path."""
    tool_results = _legacy_gemini_combined_tool_results()

    formatted = MindRoomOpenAIResponses(id="gpt-5.6", api_key="test-key")._format_messages([tool_results])

    assert formatted == [
        {"type": "function_call_output", "call_id": "toolu_1", "output": "first result"},
        {"type": "function_call_output", "call_id": "toolu_2", "output": "second result"},
    ]
