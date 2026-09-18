"""Tests for MindRoom's OpenAI-wire model subclasses."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

import httpx
import pytest
from agno.models.azure.openai_chat import AzureOpenAI
from agno.models.deepseek import DeepSeek
from agno.models.llama_cpp import LlamaCpp
from agno.models.message import Message
from agno.models.openai import OpenAIChat
from agno.models.openai.like import OpenAILike
from agno.models.openrouter import OpenRouter
from openai import OpenAI
from openai.types.chat.chat_completion_chunk import ChoiceDeltaToolCall, ChoiceDeltaToolCallFunction

from mindroom.azure_openai_model import MindRoomAzureOpenAI
from mindroom.legacy_openai_tool_replay import repair_legacy_openai_tool_replay
from mindroom.openai_models import (
    MindRoomDeepSeek,
    MindRoomLlamaCpp,
    MindRoomOpenAIChat,
    MindRoomOpenAILike,
    MindRoomOpenAIResponses,
    MindRoomOpenRouter,
)

if TYPE_CHECKING:
    from openai.types.completion_usage import CompletionUsage
    from openai.types.responses import ResponseUsage

_CHAT_WIRE_PAIRS = [
    (MindRoomOpenAIChat, OpenAIChat),
    (MindRoomOpenAILike, OpenAILike),
    (MindRoomAzureOpenAI, AzureOpenAI),
    (MindRoomOpenRouter, OpenRouter),
    (MindRoomDeepSeek, DeepSeek),
    (MindRoomLlamaCpp, LlamaCpp),
]

type _OpenAIRoute = Literal["chat", "responses"]


def _sdk_usage(route: _OpenAIRoute, *, include_cache_write: bool) -> CompletionUsage | ResponseUsage:
    """Parse provider usage through the real OpenAI client without network I/O."""

    def respond(_request: httpx.Request) -> httpx.Response:
        input_details = {"cached_tokens": 12_000}
        if include_cache_write:
            input_details["cache_write_tokens"] = 3_000
        if route == "chat":
            input_details["audio_tokens"] = 11
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl_test",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "gpt-6-astra",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": "Ready"},
                        },
                    ],
                    "usage": {
                        "prompt_tokens": 15_000,
                        "completion_tokens": 50,
                        "total_tokens": 15_050,
                        "prompt_tokens_details": input_details,
                        "completion_tokens_details": {"audio_tokens": 13, "reasoning_tokens": 7},
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "resp_test",
                "object": "response",
                "created_at": 1,
                "model": "gpt-6-astra",
                "status": "completed",
                "output": [],
                "parallel_tool_calls": True,
                "tool_choice": "auto",
                "tools": [],
                "error": None,
                "incomplete_details": None,
                "usage": {
                    "input_tokens": 15_000,
                    "output_tokens": 50,
                    "total_tokens": 15_050,
                    "input_tokens_details": input_details,
                    "output_tokens_details": {"reasoning_tokens": 7},
                },
            },
        )

    with OpenAI(
        api_key="test-key",
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    ) as client:
        if route == "chat":
            response = client.chat.completions.create(
                model="gpt-6-astra",
                messages=[{"role": "user", "content": "Hello"}],
            )
        else:
            response = client.responses.create(model="gpt-6-astra", input="Hello")
    assert response.usage is not None
    return response.usage


@pytest.mark.parametrize("include_cache_write", [True, False], ids=["current", "older"])
@pytest.mark.parametrize("route", ["responses", "chat"])
def test_openai_metrics_preserve_sdk_input_details(route: _OpenAIRoute, *, include_cache_write: bool) -> None:
    """Dropping an SDK input counter makes persisted usage and billing inaccurate."""
    usage = _sdk_usage(route, include_cache_write=include_cache_write)
    model = MindRoomOpenAIChat(id="gpt-6-astra") if route == "chat" else MindRoomOpenAIResponses(id="gpt-6-astra")

    metrics = model._get_metrics(usage)

    assert metrics.input_tokens == 15_000
    assert metrics.output_tokens == 50
    assert metrics.total_tokens == 15_050
    assert metrics.cache_read_tokens == 12_000
    assert metrics.cache_write_tokens == (3_000 if include_cache_write else 0)
    assert metrics.reasoning_tokens == 7
    assert metrics.audio_input_tokens == (11 if route == "chat" else 0)
    assert metrics.audio_output_tokens == (13 if route == "chat" else 0)
    assert metrics.audio_total_tokens == (24 if route == "chat" else 0)


def _assistant_with_argumentless_tool_call() -> Message:
    """Older Agno histories contain Anthropic calls without a function.arguments field."""
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
    parsed = model_cls(id="gpt-6-astra", api_key="test-key").parse_tool_calls([_sparse_tool_call_delta()])

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

    formatted = model_cls(id="gpt-6-astra", api_key="test-key")._format_all_messages([assistant])

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

    formatted = MindRoomOpenAIResponses(id="gpt-6-astra", api_key="test-key")._format_messages([assistant])

    assert formatted[0]["arguments"] == "{}"
    assert "arguments" not in assistant.tool_calls[0]["function"]


@pytest.mark.parametrize(
    "model",
    [
        MindRoomOpenAIResponses(id="gpt-6-astra", api_key="test-key"),
        MindRoomOpenAIResponses(id="custom-alias", api_key="test-key"),
        MindRoomOpenAIResponses(id="gpt-4.1", api_key="test-key"),
        MindRoomOpenAIResponses(id="reasoning-alias", reasoning_effort="high", api_key="test-key"),
        MindRoomOpenAIResponses(id="reasoning-alias", reasoning={"effort": "high"}, api_key="test-key"),
    ],
)
def test_responses_continue_tool_calls_independently_of_model_name(model: MindRoomOpenAIResponses) -> None:
    """Responses continuation must work for aliases and non-reasoning models too."""
    assistant = Message(
        role="assistant",
        tool_calls=[
            {
                "id": "fc_1",
                "call_id": "call_1",
                "type": "function",
                "function": {"name": "get_status", "arguments": "{}"},
            },
        ],
        provider_data={"response_id": "resp_1"},
    )
    tool_result = Message(role="tool", content="ready", tool_call_id="call_1")
    messages = [assistant, tool_result]

    request_params = model.get_request_params(messages=messages)
    formatted = model._format_messages(messages)

    assert request_params["store"] is True
    assert request_params["previous_response_id"] == "resp_1"
    assert formatted == [{"type": "function_call_output", "call_id": "call_1", "output": "ready"}]


@pytest.mark.parametrize("storage_kwargs", [{"store": False}, {"request_params": {"store": False}}])
def test_responses_reject_background_mode_with_disabled_storage(storage_kwargs: dict) -> None:
    """Background requests must not silently override a storage opt-out."""
    with pytest.raises(ValueError, match="Background Responses require store=True"):
        MindRoomOpenAIResponses(id="custom-alias", background=True, **storage_kwargs)


@pytest.mark.parametrize(
    "params",
    [
        {"request_params": {"previous_response_id": "resp_stale"}},
        {"extra_body": {"previous_response_id": "resp_stale", "custom_option": True}},
        {"request_params": {"extra_body": {"previous_response_id": "resp_stale", "custom_option": True}}},
    ],
)
def test_portable_replay_removes_explicit_continuation_overrides(params: dict) -> None:
    """Extra request overrides must not reintroduce context excluded by history fitting."""
    model = MindRoomOpenAIResponses(id="gpt-6-astra", **params)
    model.configure_portable_replay()
    request = model.get_request_params(messages=[Message(role="user", content="Continue")])
    assert "previous_response_id" not in request
    if "extra_body" in request:
        assert request["extra_body"] == {"custom_option": True}


@pytest.mark.parametrize("storage_kwargs", [{"store": False}, {"request_params": {"store": False}}])
def test_explicit_reasoning_respects_disabled_response_storage(storage_kwargs: dict) -> None:
    """Reasoning aliases must not turn a stateless request into server-side storage."""
    model = MindRoomOpenAIResponses(
        id="reasoning-alias",
        reasoning_effort="high",
        api_key="test-key",
        **storage_kwargs,
    )
    messages = [
        Message(role="assistant", content="Earlier reply", provider_data={"response_id": "resp_1"}),
        Message(role="user", content="Follow up"),
    ]

    request_params = model.get_request_params(messages=messages)

    assert request_params["store"] is False
    assert "previous_response_id" not in request_params
    assert "reasoning.encrypted_content" in request_params["include"]
    assert model._format_messages(messages) == [
        {"role": "assistant", "content": "Earlier reply"},
        {"role": "user", "content": "Follow up"},
    ]


@pytest.mark.parametrize(("model_cls", "_agno_cls"), _CHAT_WIRE_PAIRS)
def test_chat_models_leave_combined_tool_results_for_agno_normalization(
    model_cls: type[OpenAIChat],
    _agno_cls: type[OpenAIChat],
) -> None:
    """Argument repair must not consume non-assistant combined tool results."""
    tool_results = _legacy_combined_tool_results()

    formatted = model_cls(id="gpt-6-astra", api_key="test-key")._format_all_messages([tool_results])

    assert formatted == [
        {"role": "tool", "content": "first result", "tool_call_id": "toolu_1"},
        {"role": "tool", "content": "second result", "tool_call_id": "toolu_2"},
    ]


def test_openai_responses_leaves_combined_tool_results_for_agno_normalization() -> None:
    """Responses replay must preserve Agno's combined-result normalization path."""
    tool_results = _legacy_combined_tool_results()

    formatted = MindRoomOpenAIResponses(id="gpt-6-astra", api_key="test-key")._format_messages([tool_results])

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

    formatted = model_cls(id="gpt-6-astra", api_key="test-key")._format_all_messages(messages)

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

    formatted = MindRoomOpenAIResponses(id="gpt-6-astra", api_key="test-key")._format_messages(messages)

    assert len(formatted) == 2
    assert formatted[0]["type"] == "function_call"
    assert formatted[0]["name"] == "get_status"
    assert formatted[0]["arguments"] == "{}"
    assert formatted[1] == {
        "type": "function_call_output",
        "call_id": formatted[0]["call_id"],
        "output": "ready",
    }


def test_legacy_openai_tool_replay_keeps_unchanged_messages_by_identity() -> None:
    """Replay repair avoids copying an already valid historical message."""
    message = Message(
        role="assistant",
        tool_calls=[
            {
                "id": "call_abcdefghijklmnopqrstuvwx",
                "type": "function",
                "function": {"name": "get_status", "arguments": "{}"},
            },
        ],
    )

    assert repair_legacy_openai_tool_replay([message]) == [message]
    assert repair_legacy_openai_tool_replay([message])[0] is message
