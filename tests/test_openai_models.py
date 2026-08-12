"""Tests for MindRoom's OpenAI-wire model subclasses."""

from __future__ import annotations

from copy import deepcopy
from typing import LiteralString

import pytest
from agno.models.azure.openai_chat import AzureOpenAI
from agno.models.base import MessageData
from agno.models.deepseek import DeepSeek
from agno.models.llama_cpp import LlamaCpp
from agno.models.message import Message
from agno.models.openai import OpenAIChat
from agno.models.openai.like import OpenAILike
from agno.models.openrouter import OpenRouter
from agno.models.response import ModelResponse
from openai.types.chat.chat_completion_chunk import ChoiceDeltaToolCall, ChoiceDeltaToolCallFunction

from mindroom.azure_openai_model import MindRoomAzureOpenAI
from mindroom.openai_models import (
    _OPENROUTER_REASONING_DETAILS_BUFFER,
    MindRoomDeepSeek,
    MindRoomLlamaCpp,
    MindRoomOpenAIChat,
    MindRoomOpenAILike,
    MindRoomOpenAIResponses,
    MindRoomOpenRouter,
    _coalesced_openrouter_reasoning_details,
)

_CHAT_WIRE_PAIRS = [
    (MindRoomOpenAIChat, OpenAIChat),
    (MindRoomOpenAILike, OpenAILike),
    (MindRoomAzureOpenAI, AzureOpenAI),
    (MindRoomOpenRouter, OpenRouter),
    (MindRoomDeepSeek, DeepSeek),
    (MindRoomLlamaCpp, LlamaCpp),
]


@pytest.mark.parametrize(
    ("details", "expected"),
    [
        (
            [
                {"type": "reasoning.text", "text": "first", "index": 0},
                {"type": "reasoning.text", "text": " second", "index": 0},
            ],
            [{"type": "reasoning.text", "text": "first second", "index": 0}],
        ),
        (
            [
                {"type": "reasoning.text", "text": "first", "id": "reasoning-1"},
                {"type": "reasoning.text", "text": " second", "id": "reasoning-1"},
            ],
            [{"type": "reasoning.text", "text": "first second", "id": "reasoning-1"}],
        ),
        (
            [
                {"type": "reasoning.text", "text": "a", "index": 0},
                {"type": "reasoning.text", "text": "b", "index": 1},
                {"type": "reasoning.text", "text": "c", "index": 0},
            ],
            [
                {"type": "reasoning.text", "text": "a", "index": 0},
                {"type": "reasoning.text", "text": "b", "index": 1},
                {"type": "reasoning.text", "text": "c", "index": 0},
            ],
        ),
        (
            [
                {"type": "reasoning.text", "text": "a", "index": 0, "id": "one"},
                {"type": "reasoning.text", "text": "b", "index": 0, "id": "two"},
                {"type": "reasoning.text", "text": "c", "index": 0, "signature": "one"},
                {"type": "reasoning.text", "text": "d", "index": 0, "signature": "two"},
                {"type": "reasoning.text", "text": "e", "index": 0},
                {"type": "reasoning.text", "text": "f", "index": 0, "id": None},
            ],
            [
                {"type": "reasoning.text", "text": "a", "index": 0, "id": "one"},
                {"type": "reasoning.text", "text": "b", "index": 0, "id": "two"},
                {"type": "reasoning.text", "text": "c", "index": 0, "signature": "one"},
                {"type": "reasoning.text", "text": "d", "index": 0, "signature": "two"},
                {"type": "reasoning.text", "text": "e", "index": 0},
                {"type": "reasoning.text", "text": "f", "index": 0, "id": None},
            ],
        ),
        (
            [
                {"type": "reasoning.text", "text": "a"},
                {"type": "reasoning.text", "text": "b"},
                {"type": "reasoning.text", "text": "c", "index": True},
                {"type": "reasoning.text", "text": "d", "index": True},
                {"type": "reasoning.text", "text": "e", "id": ""},
                {"type": "reasoning.text", "text": "f", "id": ""},
            ],
            [
                {"type": "reasoning.text", "text": "a"},
                {"type": "reasoning.text", "text": "b"},
                {"type": "reasoning.text", "text": "c", "index": True},
                {"type": "reasoning.text", "text": "d", "index": True},
                {"type": "reasoning.text", "text": "e", "id": ""},
                {"type": "reasoning.text", "text": "f", "id": ""},
            ],
        ),
        (
            [
                {"type": "reasoning.text", "text": "a", "index": 0},
                {"type": "reasoning.text", "text": 1, "index": 0},
                {"type": "reasoning.summary", "text": "summary", "index": 0},
                "malformed",
                None,
            ],
            [
                {"type": "reasoning.text", "text": "a", "index": 0},
                {"type": "reasoning.text", "text": 1, "index": 0},
                {"type": "reasoning.summary", "text": "summary", "index": 0},
                "malformed",
                None,
            ],
        ),
    ],
)
def test_openrouter_reasoning_details_coalesce_only_adjacent_compatible_text(
    details: list[object],
    expected: list[object],
) -> None:
    """Only unambiguously compatible text fragments may be joined."""
    original = deepcopy(details)

    normalized = _coalesced_openrouter_reasoning_details(details)

    assert normalized == expected
    assert details == original


@pytest.mark.parametrize("details", [None, "details", {"type": "reasoning.text"}, 1])
def test_openrouter_reasoning_details_leave_non_lists_unchanged(details: object) -> None:
    """Malformed provider payloads must pass through without interpretation."""
    assert _coalesced_openrouter_reasoning_details(details) is details


def test_openrouter_reasoning_details_stream_coalesces_across_deltas_and_preserves_yields() -> None:
    """Streaming storage stays compact without changing superclass output behavior."""
    model = MindRoomOpenRouter(id="test/model", api_key="test-key")
    stream_data = MessageData()
    deltas = [
        ModelResponse(
            content="first",
            provider_data={
                "reasoning_details": [
                    {"type": "reasoning.text", "text": "a", "index": 0},
                    {"type": "reasoning.text", "text": "b", "index": 0},
                    {"type": "reasoning.text", "text": "c", "index": 1},
                ],
                "trace": ["one"],
            },
        ),
        ModelResponse(
            content=" second",
            provider_data={
                "reasoning_details": [
                    {"type": "reasoning.text", "text": "d", "index": 1},
                    {"type": "reasoning.text", "text": "e", "index": 0},
                ],
                "trace": ["two"],
            },
        ),
    ]

    yielded = [item for delta in deltas for item in model._populate_stream_data(stream_data, delta)]

    assert all(yielded_delta is original_delta for yielded_delta, original_delta in zip(yielded, deltas, strict=True))
    assert [item.content for item in yielded] == ["first", " second"]
    assert stream_data.response_content == "first second"
    assistant = Message(role="assistant")
    model._populate_assistant_message_from_stream_data(assistant, stream_data)
    assert stream_data.response_provider_data == {
        "reasoning_details": [
            {"type": "reasoning.text", "text": "ab", "index": 0},
            {"type": "reasoning.text", "text": "cd", "index": 1},
            {"type": "reasoning.text", "text": "e", "index": 0},
        ],
        "trace": ["one", "two"],
    }
    assert assistant.provider_data == stream_data.response_provider_data


def test_openrouter_reasoning_details_stream_joins_fragments_only_when_finalized() -> None:
    """Streaming must collect text linearly, then persist one ordinary string."""

    class NoConcatenationStr(str):
        __slots__ = ()

        def __add__(self, other: str, /) -> LiteralString:
            message = "streaming concatenated reasoning fragments"
            raise AssertionError(message)

    model = MindRoomOpenRouter(id="test/model", api_key="test-key")
    stream_data = MessageData()
    details = [{"type": "reasoning.text", "text": NoConcatenationStr("x"), "index": 0} for _ in range(100)]

    list(
        model._populate_stream_data(
            stream_data,
            ModelResponse(provider_data={"reasoning_details": details}),
        ),
    )
    buffered_blocks = getattr(stream_data, _OPENROUTER_REASONING_DETAILS_BUFFER)
    assert len(buffered_blocks) == 1
    assert not any(isinstance(block, dict) for block in buffered_blocks)
    assistant = Message(role="assistant")
    model._populate_assistant_message_from_stream_data(assistant, stream_data)

    persisted_details = stream_data.response_provider_data["reasoning_details"]
    assert persisted_details == [{"type": "reasoning.text", "text": "x" * 100, "index": 0}]
    assert type(persisted_details[0]["text"]) is str
    assert assistant.provider_data == stream_data.response_provider_data


def test_openrouter_reasoning_details_stream_preserves_later_non_list_value() -> None:
    """A malformed later provider value must retain Agno's replacement semantics."""
    model = MindRoomOpenRouter(id="test/model", api_key="test-key")
    stream_data = MessageData()

    list(
        model._populate_stream_data(
            stream_data,
            ModelResponse(
                provider_data={
                    "reasoning_details": [{"type": "reasoning.text", "text": "a", "index": 0}],
                },
            ),
        ),
    )
    list(
        model._populate_stream_data(
            stream_data,
            ModelResponse(provider_data={"reasoning_details": None}),
        ),
    )
    model._populate_assistant_message_from_stream_data(Message(role="assistant"), stream_data)

    assert stream_data.response_provider_data == {"reasoning_details": None}


def test_openrouter_reasoning_details_stream_list_after_non_list_starts_a_new_sequence() -> None:
    """A later valid list must replace the malformed value and discarded earlier list."""
    model = MindRoomOpenRouter(id="test/model", api_key="test-key")
    stream_data = MessageData()
    provider_values: list[object] = [
        [{"type": "reasoning.text", "text": "discarded", "index": 0}],
        None,
        [
            {"type": "reasoning.text", "text": "kept", "index": 0},
            {"type": "reasoning.text", "text": " together", "index": 0},
        ],
    ]

    for provider_value in provider_values:
        list(
            model._populate_stream_data(
                stream_data,
                ModelResponse(provider_data={"reasoning_details": provider_value}),
            ),
        )
    model._populate_assistant_message_from_stream_data(Message(role="assistant"), stream_data)

    assert stream_data.response_provider_data == {
        "reasoning_details": [{"type": "reasoning.text", "text": "kept together", "index": 0}],
    }


def test_openrouter_reasoning_details_replay_coalesces_without_mutating_history() -> None:
    """Wire replay is normalized while persisted provider data remains untouched."""
    model = MindRoomOpenRouter(id="test/model", api_key="test-key")
    assistant = Message(
        role="assistant",
        content="answer",
        provider_data={
            "reasoning_details": [
                {"type": "reasoning.text", "text": "first", "index": 0},
                {"type": "reasoning.text", "text": " second", "index": 0},
            ],
            "trace": {"request_id": "request-1"},
        },
    )
    original_provider_data = deepcopy(assistant.provider_data)

    formatted = model._format_message(assistant)

    assert formatted["reasoning_details"] == [
        {"type": "reasoning.text", "text": "first second", "index": 0},
    ]
    assert assistant.provider_data == original_provider_data
    assert assistant.provider_data["trace"] == {"request_id": "request-1"}


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


def _legacy_combined_tool_results() -> Message:
    """Recreate the combined tool-result shape handled by Agno's normalizer."""
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
def test_chat_models_leave_combined_tool_results_for_agno_normalization(
    model_cls: type[OpenAIChat],
    _agno_cls: type[OpenAIChat],
) -> None:
    """Argument repair must not consume non-assistant combined tool results."""
    tool_results = _legacy_combined_tool_results()

    formatted = model_cls(id="gpt-5.6", api_key="test-key")._format_all_messages([tool_results])

    assert formatted == [
        {"role": "tool", "content": "first result", "tool_call_id": "toolu_1"},
        {"role": "tool", "content": "second result", "tool_call_id": "toolu_2"},
    ]


def test_openai_responses_leaves_combined_tool_results_for_agno_normalization() -> None:
    """Responses replay must preserve Agno's combined-result normalization path."""
    tool_results = _legacy_combined_tool_results()

    formatted = MindRoomOpenAIResponses(id="gpt-5.6", api_key="test-key")._format_messages([tool_results])

    assert formatted == [
        {"type": "function_call_output", "call_id": "toolu_1", "output": "first result"},
        {"type": "function_call_output", "call_id": "toolu_2", "output": "second result"},
    ]


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
