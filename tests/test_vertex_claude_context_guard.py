"""Exact request fitting for Vertex Claude."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from agno.exceptions import ContextWindowExceededError
from agno.media import Image
from agno.models.message import Message
from agno.models.response import ModelResponse
from agno.models.vertexai.claude import Claude as VertexAIClaude
from anthropic.lib.streaming import ParsedMessageStopEvent
from anthropic.types import Message as AnthropicMessage
from anthropic.types import ParsedMessage, Usage

from mindroom.claude_prompt_cache import (
    SERVER_TOOL_USE_BLOCK_TYPE,
    TOOL_SEARCH_RESULT_BLOCK_TYPE,
    TOOL_SEARCH_TOOL_TYPE,
)
from mindroom.claude_stream_retry import install_claude_stream_retry_hook
from mindroom.error_handling import ModelSafeguardRefusalError
from mindroom.token_budget import stable_serialize
from mindroom.vertex_claude_compat import (
    _EXACT_COUNT_THRESHOLD_RATIO,
    _VERTEX_TOOL_SEARCH_TOKEN_RESERVE,
    MindroomVertexAIClaude,
    _request_for_vertex_token_count,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


def _model() -> MindroomVertexAIClaude:
    return MindroomVertexAIClaude(
        id="claude-sonnet-4-6",
        project_id="demo-project",
        region="us-central1",
        cache_system_prompt=False,
        context_window=100,
        max_tokens=20,
    )


def _tool_loop_messages() -> list[Message]:
    return [
        Message(role="system", content="instructions"),
        Message(role="user", content="old question", from_history=True),
        Message(role="assistant", content="old answer", from_history=True),
        Message(role="user", content="current question"),
        Message(
            role="assistant",
            tool_calls=[
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": "{}"},
                },
            ],
        ),
        Message(role="tool", tool_call_id="call-1", content="large result"),
    ]


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


def test_non_streaming_safeguard_refusal_raises_typed_error() -> None:
    """Anthropic Messages exposes safeguards as stop_reason=refusal."""
    model = _model()

    with pytest.raises(ModelSafeguardRefusalError, match="stop_reason=refusal"):
        model._parse_provider_response(_safeguard_refusal_message())


def test_streaming_safeguard_refusal_raises_typed_error() -> None:
    """Streaming exposes stop_reason on the final message_stop payload."""
    model = _model()
    parsed_message = ParsedMessage[object].model_validate(_safeguard_refusal_message().model_dump())
    message_stop = ParsedMessageStopEvent(type="message_stop", message=parsed_message)

    with pytest.raises(ModelSafeguardRefusalError, match="stop_reason=refusal"):
        model._parse_provider_response_delta(message_stop)


def test_safeguard_refusal_survives_agno_error_translation() -> None:
    """Agno must not replace the typed refusal with a generic provider error."""
    model = _model()
    error = ModelSafeguardRefusalError(
        message="Vertex Claude returned stop_reason=refusal",
        model_name=model.name,
        model_id=model.id,
    )

    with pytest.raises(ModelSafeguardRefusalError) as raised:
        model._handle_api_error(error)

    assert raised.value is error


def test_safeguard_refusal_is_not_retryable_by_agno() -> None:
    """Agno's configured model retry loop must not repeat a refusal."""
    model = _model()
    error = ModelSafeguardRefusalError(
        message="Vertex Claude returned stop_reason=refusal",
        model_name=model.name,
        model_id=model.id,
    )

    assert model._is_retryable_error(error) is False


def test_estimate_request_input_tokens_uses_full_provider_payload() -> None:
    """Local estimation includes formatted messages, system, tools, and schema."""
    model = _model()
    tools = [
        {
            "type": "function",
            "function": {
                "name": "lookup",
                "description": "Look up a value.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
    ]
    response_format = {"type": "object", "properties": {"answer": {"type": "string"}}}

    with (
        patch(
            "mindroom.vertex_claude_compat.estimate_compaction_input_tokens",
            return_value=30,
        ) as estimator,
        patch("mindroom.vertex_claude_compat.count_schema_tokens", return_value=5),
    ):
        estimated_tokens = model._estimate_request_input_tokens(
            _tool_loop_messages(),
            tools=tools,
            response_format=response_format,
            compress_tool_results=False,
        )

    serialized_payload = estimator.call_args.args[0]
    assert estimated_tokens == 35
    assert all(key in serialized_payload for key in ('"messages"', '"model"', '"system"', '"tools"'))


def test_estimate_requires_exact_count_for_images() -> None:
    """Images bypass local estimation regardless of their encoded size."""
    model = _model()
    image_bytes = b"\x89PNG\r\n\x1a\n" + b"fake pixel data"
    messages = [
        Message(role="user", content="what is in this picture?", images=[Image(content=image_bytes)]),
    ]

    with patch("mindroom.vertex_claude_compat.estimate_compaction_input_tokens") as estimator:
        estimated_tokens = model._estimate_request_input_tokens(
            messages,
            tools=None,
            response_format=None,
            compress_tool_results=False,
        )

    assert estimated_tokens is None
    estimator.assert_not_called()


def test_estimate_requires_exact_count_for_base64_documents() -> None:
    """Compressed documents bypass byte-size heuristics and use Vertex counting."""
    model = _model()
    request_kwargs = {
        "model": model.id,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": "compressed-pdf-data",
                        },
                    },
                ],
            },
        ],
    }

    with (
        patch.object(model, "_request_input_kwargs", return_value=request_kwargs),
        patch("mindroom.vertex_claude_compat.estimate_compaction_input_tokens") as estimator,
    ):
        estimated_tokens = model._estimate_request_input_tokens(
            [Message(role="user", content="summarize")],
            tools=None,
            response_format=None,
            compress_tool_results=False,
        )

    assert estimated_tokens is None
    estimator.assert_not_called()


def test_vertex_token_count_request_preserves_native_tool_search_as_countable_text() -> None:
    """Only the count payload is adapted for Vertex's older request schema."""
    large_schema_description = "large schema " * 10_000
    search_use = {
        "type": SERVER_TOOL_USE_BLOCK_TYPE,
        "id": "srvtoolu-1",
        "name": "tool_search_tool_regex",
        "input": {"pattern": "weather"},
    }
    search_result = {
        "type": TOOL_SEARCH_RESULT_BLOCK_TYPE,
        "tool_use_id": "srvtoolu-1",
        "content": {
            "type": "tool_search_tool_search_result",
            "tool_references": [{"type": "tool_reference", "tool_name": "weather_lookup"}],
        },
    }
    request_kwargs = {
        "model": "claude-sonnet-4-6",
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Searching."},
                    search_use,
                    search_result,
                ],
            },
        ],
        "tools": [
            {"type": TOOL_SEARCH_TOOL_TYPE, "name": "tool_search_tool_regex"},
            {"name": "always_tool", "input_schema": {"type": "object"}},
            {
                "name": "weather_lookup",
                "description": large_schema_description,
                "input_schema": {"type": "object"},
                "defer_loading": True,
            },
            {
                "name": "unused_lookup",
                "description": "Never selected.",
                "input_schema": {"type": "object"},
                "defer_loading": True,
            },
        ],
    }

    count_kwargs, reserve = _request_for_vertex_token_count(request_kwargs)

    assert reserve == _VERTEX_TOOL_SEARCH_TOKEN_RESERVE
    assert count_kwargs["tools"] == [
        {"name": "always_tool", "input_schema": {"type": "object"}},
        {
            "name": "weather_lookup",
            "description": large_schema_description,
            "input_schema": {"type": "object"},
        },
    ]
    assert count_kwargs["messages"][0]["content"] == [
        {"type": "text", "text": "Searching."},
        {
            "type": "text",
            "text": (
                '{"id":"srvtoolu-1","input":{"pattern":"weather"},'
                '"name":"tool_search_tool_regex","type":"server_tool_use"}'
            ),
        },
        {
            "type": "text",
            "text": (
                '{"content":{"tool_references":[{"tool_name":"weather_lookup",'
                '"type":"tool_reference"}],"type":"tool_search_tool_search_result"},'
                '"tool_use_id":"srvtoolu-1","type":"tool_search_tool_result"}'
            ),
        },
    ]
    assert request_kwargs["tools"][0]["type"] == TOOL_SEARCH_TOOL_TYPE
    assert request_kwargs["messages"][0]["content"][1] is search_use
    assert request_kwargs["messages"][0]["content"][2] is search_result


def test_vertex_token_count_adapts_search_history_without_current_search_tool() -> None:
    """Replayed search blocks remain countable after the current tool surface changes."""
    request_kwargs = {
        "model": "claude-sonnet-4-6",
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": SERVER_TOOL_USE_BLOCK_TYPE,
                        "id": "srvtoolu-1",
                        "name": "tool_search_tool_regex",
                        "input": {"pattern": "weather"},
                    },
                ],
            },
        ],
    }

    count_kwargs, reserve = _request_for_vertex_token_count(request_kwargs)

    assert reserve == 0
    assert count_kwargs["messages"][0]["content"][0] == {
        "type": "text",
        "text": (
            '{"id":"srvtoolu-1","input":{"pattern":"weather"},"name":"tool_search_tool_regex","type":"server_tool_use"}'
        ),
    }


def test_vertex_token_count_request_leaves_regular_requests_unchanged() -> None:
    """Requests without native search keep their original count payload."""
    request_kwargs = {
        "model": "claude-sonnet-4-6",
        "messages": [{"role": "user", "content": "hello"}],
        "tools": [{"name": "lookup", "input_schema": {"type": "object"}}],
    }

    count_kwargs, reserve = _request_for_vertex_token_count(request_kwargs)

    assert count_kwargs is request_kwargs
    assert reserve == 0


@pytest.mark.asyncio
async def test_exact_count_uses_vertex_compatible_tool_search_payload() -> None:
    """Exact counting includes the native-search reserve after sanitization."""
    model = _model()
    request_kwargs = {
        "model": model.id,
        "messages": [{"role": "user", "content": "hello"}],
        "tools": [
            {"type": TOOL_SEARCH_TOOL_TYPE, "name": "tool_search_tool_regex"},
            {"name": "always_tool", "input_schema": {"type": "object"}},
            {"name": "deferred_tool", "input_schema": {"type": "object"}, "defer_loading": True},
        ],
    }
    count_tokens = AsyncMock(return_value=SimpleNamespace(input_tokens=700))
    client = SimpleNamespace(messages=SimpleNamespace(count_tokens=count_tokens))

    with (
        patch.object(model, "_request_input_kwargs", return_value=request_kwargs),
        patch.object(model, "get_async_client", return_value=client),
        patch("mindroom.vertex_claude_compat.count_schema_tokens", return_value=5),
    ):
        input_tokens = await model._count_request_input_tokens(
            [Message(role="user", content="hello")],
            tools=None,
            response_format=None,
            compress_tool_results=False,
        )

    assert input_tokens == 700 + _VERTEX_TOOL_SEARCH_TOKEN_RESERVE + 5
    count_tokens.assert_awaited_once_with(
        model=model.id,
        messages=request_kwargs["messages"],
        tools=[{"name": "always_tool", "input_schema": {"type": "object"}}],
    )


@pytest.mark.asyncio
async def test_fit_request_messages_drops_oldest_replay_and_keeps_current_tool_loop() -> None:
    """Exact counting trims history while preserving the full current turn."""
    model = _model()

    async def _count(messages: list[Message], **_kwargs: object) -> int:
        return 110 if len(messages) > 4 else 70

    counter = AsyncMock(side_effect=_count)
    with (
        patch.object(model, "_estimate_request_input_tokens", return_value=40),
        patch.object(model, "_count_request_input_tokens", new=counter),
    ):
        fitted = await model._fit_request_messages(
            _tool_loop_messages(),
            tools=None,
            response_format=None,
            compress_tool_results=False,
        )

    assert [(message.role, message.content) for message in fitted] == [
        ("system", "instructions"),
        ("user", "current question"),
        ("assistant", None),
        ("tool", "large result"),
    ]
    assert counter.await_count == 2


@pytest.mark.asyncio
async def test_fit_request_messages_trims_minimally_across_multiple_history_turns() -> None:
    """The binary search settles on the earliest cut that fits, keeping newer turns."""
    model = _model()
    messages = [
        Message(role="system", content="instructions"),
        Message(role="user", content="turn one", from_history=True),
        Message(role="assistant", content="answer one", from_history=True),
        Message(role="user", content="turn two", from_history=True),
        Message(role="assistant", content="answer two", from_history=True),
        Message(role="user", content="turn three", from_history=True),
        Message(role="assistant", content="answer three", from_history=True),
        Message(role="user", content="current question"),
    ]
    tokens_by_message_count = {8: 110, 6: 90, 4: 75, 2: 60}

    async def _count(candidate: list[Message], **_kwargs: object) -> int:
        return tokens_by_message_count[len(candidate)]

    counter = AsyncMock(side_effect=_count)
    with (
        patch.object(model, "_estimate_request_input_tokens", return_value=40),
        patch.object(model, "_count_request_input_tokens", new=counter),
    ):
        fitted = await model._fit_request_messages(
            messages,
            tools=None,
            response_format=None,
            compress_tool_results=False,
        )

    assert [(message.role, message.content) for message in fitted] == [
        ("system", "instructions"),
        ("user", "turn three"),
        ("assistant", "answer three"),
        ("user", "current question"),
    ]
    assert counter.await_count == 3


@pytest.mark.asyncio
async def test_async_invocations_delegate_with_fitted_messages() -> None:
    """Both async provider paths send the fitted message list."""
    model = _model()
    fitted = [Message(role="user", content="fitted")]
    fit = AsyncMock(return_value=fitted)
    regular_calls: list[list[Message]] = []
    stream_calls: list[list[Message]] = []

    async def _ainvoke(
        _model: VertexAIClaude,
        messages: list[Message],
        _assistant_message: Message,
        **_kwargs: object,
    ) -> ModelResponse:
        regular_calls.append(messages)
        return ModelResponse(content="regular")

    async def _ainvoke_stream(
        _model: VertexAIClaude,
        messages: list[Message],
        _assistant_message: Message,
        **_kwargs: object,
    ) -> AsyncIterator[ModelResponse]:
        stream_calls.append(messages)
        yield ModelResponse(content="stream")

    assistant_message = Message(role="assistant")
    with (
        patch.object(model, "_fit_request_messages", new=fit),
        patch.object(VertexAIClaude, "ainvoke", new=_ainvoke),
        patch.object(VertexAIClaude, "ainvoke_stream", new=_ainvoke_stream),
    ):
        regular_response = await model.ainvoke(_tool_loop_messages(), assistant_message)
        stream_responses = [
            response async for response in model.ainvoke_stream(_tool_loop_messages(), assistant_message)
        ]

    assert regular_response.content == "regular"
    assert [response.content for response in stream_responses] == ["stream"]
    assert regular_calls == [fitted]
    assert stream_calls == [fitted]
    assert fit.await_count == 2


@pytest.mark.asyncio
async def test_fit_request_messages_skips_exact_count_below_half_budget() -> None:
    """Small requests avoid the Vertex token-count network call."""
    model = _model()
    messages = _tool_loop_messages()
    counter = AsyncMock()

    with (
        patch.object(model, "_estimate_request_input_tokens", return_value=39),
        patch.object(model, "_count_request_input_tokens", new=counter),
    ):
        fitted = await model._fit_request_messages(
            messages,
            tools=None,
            response_format=None,
            compress_tool_results=False,
        )

    assert fitted is messages
    counter.assert_not_awaited()


@pytest.mark.asyncio
async def test_fit_request_messages_counts_exactly_at_half_budget() -> None:
    """Requests at the threshold use Vertex's exact tokenizer."""
    model = _model()
    messages = _tool_loop_messages()
    counter = AsyncMock(return_value=80)

    with (
        patch.object(model, "_estimate_request_input_tokens", return_value=40),
        patch.object(model, "_count_request_input_tokens", new=counter),
    ):
        fitted = await model._fit_request_messages(
            messages,
            tools=None,
            response_format=None,
            compress_tool_results=False,
        )

    assert fitted is messages
    counter.assert_awaited_once()


@pytest.mark.asyncio
async def test_conservative_fallback_crossing_half_budget_delegates_to_exact_count() -> None:
    """The byte fallback may deliberately cause an earlier Vertex count call."""
    model = _model()
    messages = [Message(role="user", content="hello")]
    request_kwargs = {
        "model": model.id,
        "messages": [{"role": "user", "content": "hello"}],
    }
    counter = AsyncMock(return_value=20)

    with (
        patch.object(model, "_request_input_kwargs", return_value=request_kwargs),
        patch.object(model, "_count_request_input_tokens", new=counter),
        patch("mindroom.vertex_claude_compat.count_schema_tokens", return_value=0),
    ):
        estimated_tokens = model._estimate_request_input_tokens(
            messages,
            tools=None,
            response_format=None,
            compress_tool_results=False,
        )
        fitted = await model._fit_request_messages(
            messages,
            tools=None,
            response_format=None,
            compress_tool_results=False,
        )

    assert estimated_tokens == len(stable_serialize(request_kwargs).encode("utf-8"))
    assert model.context_window is not None
    assert model.max_tokens is not None
    exact_count_threshold = (model.context_window - model.max_tokens) * _EXACT_COUNT_THRESHOLD_RATIO
    assert estimated_tokens >= exact_count_threshold
    assert fitted is messages
    counter.assert_awaited_once()


@pytest.mark.asyncio
async def test_fit_request_messages_counts_media_exactly() -> None:
    """Media requests always use Vertex's exact tokenizer."""
    model = _model()
    messages = _tool_loop_messages()
    counter = AsyncMock(return_value=80)

    with (
        patch.object(model, "_estimate_request_input_tokens", return_value=None),
        patch.object(model, "_count_request_input_tokens", new=counter),
    ):
        fitted = await model._fit_request_messages(
            messages,
            tools=None,
            response_format=None,
            compress_tool_results=False,
        )

    assert fitted is messages
    counter.assert_awaited_once()


@pytest.mark.asyncio
async def test_fit_request_messages_rejects_current_turn_that_cannot_fit() -> None:
    """The guard fails visibly instead of sending an oversized current turn."""
    model = _model()

    with (
        patch.object(model, "_estimate_request_input_tokens", return_value=40),
        patch.object(model, "_count_request_input_tokens", new=AsyncMock(return_value=90)),
        pytest.raises(ContextWindowExceededError, match="current turn"),
    ):
        await model._fit_request_messages(
            _tool_loop_messages(),
            tools=None,
            response_format=None,
            compress_tool_results=False,
        )


@pytest.mark.asyncio
async def test_stream_retry_does_not_repeat_current_turn_fit_failure() -> None:
    """The installed stream wrapper preserves one typed local fit failure."""
    model = _model()
    install_claude_stream_retry_hook(model)
    counter = AsyncMock(return_value=90)

    with (
        patch.object(model, "_estimate_request_input_tokens", return_value=40) as estimator,
        patch.object(model, "_count_request_input_tokens", new=counter),
        pytest.raises(ContextWindowExceededError) as raised,
    ):
        _ = [
            response
            async for response in model.ainvoke_stream(
                _tool_loop_messages(),
                Message(role="assistant"),
            )
        ]

    assert raised.value.message == "Vertex Claude current turn uses more than the 80-token input limit."
    estimator.assert_called_once()
