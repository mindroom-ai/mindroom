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
from openai.types.chat.chat_completion_chunk import ChoiceDeltaToolCall, ChoiceDeltaToolCallFunction

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


def _messages_with_sparse_stream_placeholder() -> list[Message]:
    """Recreate history left by a tool-call stream whose first index was one."""
    return [
        Message(
            role="assistant",
            tool_calls=[
                {"id": "phantom-call"},
                {
                    "id": "call_abcdefghijklmnopqrstuvwx",
                    "type": "function",
                    "function": {"name": "get_status", "arguments": "{}"},
                },
            ],
        ),
        Message(role="tool", content="tool unavailable", tool_call_id="phantom-call"),
        Message(role="tool", content="ready", tool_call_id="call_abcdefghijklmnopqrstuvwx"),
    ]


def _sparse_tool_call_delta() -> ChoiceDeltaToolCall:
    """Return one valid call at stream index one, leaving index zero empty in Agno."""
    return ChoiceDeltaToolCall(
        index=1,
        id="call_abcdefghijklmnopqrstuvwx",
        type="function",
        function=ChoiceDeltaToolCallFunction(name="get_status", arguments="{}"),
    )


@pytest.mark.parametrize(("model_cls", "_agno_cls"), _CHAT_WIRE_PAIRS)
def test_chat_models_drop_sparse_stream_placeholders(
    model_cls: type[OpenAIChat],
    _agno_cls: type[OpenAIChat],
) -> None:
    """A missing lower stream index must not become an id-only assistant tool call."""
    parsed = model_cls(id="gpt-5.6", api_key="test-key").parse_tool_calls([_sparse_tool_call_delta()])

    assert parsed == [
        {
            "id": "call_abcdefghijklmnopqrstuvwx",
            "type": "function",
            "function": {"name": "get_status", "arguments": "{}"},
        },
    ]


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
def test_chat_models_remove_persisted_sparse_placeholder_and_orphan_result(
    model_cls: type[OpenAIChat],
    _agno_cls: type[OpenAIChat],
) -> None:
    """Replay must retain real calls while removing a saved placeholder pair."""
    messages = _messages_with_sparse_stream_placeholder()

    formatted = model_cls(id="gpt-5.6", api_key="test-key")._format_all_messages(messages)

    assert formatted == [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_abcdefghijklmnopqrstuvwx",
                    "type": "function",
                    "function": {"name": "get_status", "arguments": "{}"},
                },
            ],
        },
        {"role": "tool", "content": "ready", "tool_call_id": "call_abcdefghijklmnopqrstuvwx"},
    ]


def test_openai_responses_removes_persisted_sparse_placeholder_and_orphan_result() -> None:
    """Responses replay must retain real calls while removing a saved placeholder pair."""
    messages = _messages_with_sparse_stream_placeholder()

    formatted = MindRoomOpenAIResponses(id="gpt-5.6", api_key="test-key")._format_messages(messages)

    assert len(formatted) == 2
    assert formatted[0]["type"] == "function_call"
    assert formatted[0]["name"] == "get_status"
    assert formatted[0]["arguments"] == "{}"
    assert formatted[1] == {
        "type": "function_call_output",
        "call_id": formatted[0]["call_id"],
        "output": "ready",
    }
