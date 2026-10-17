"""Vertex Claude safeguard refusals and stream usage across attempts."""

from __future__ import annotations

import asyncio

import pytest
from agno.metrics import MessageMetrics
from agno.models.message import Message
from anthropic.lib.streaming import ParsedMessageStopEvent
from anthropic.types import Message as AnthropicMessage
from anthropic.types import ParsedMessage, Usage

from mindroom.error_handling import MODEL_SAFEGUARD_REFUSAL_MESSAGE, ModelSafeguardRefusalError
from mindroom.providers.vertex_claude_compat import (
    MindroomVertexAIClaude,
)


def _model() -> MindroomVertexAIClaude:
    return MindroomVertexAIClaude(
        id="claude-sonnet-5",
        project_id="demo-project",
        region="us-central1",
        cache_system_prompt=False,
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
        model="claude-fable-5-1",
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
        message=MODEL_SAFEGUARD_REFUSAL_MESSAGE,
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
        message=MODEL_SAFEGUARD_REFUSAL_MESSAGE,
        model_name=model.name,
        model_id=model.id,
    )

    assert model._is_retryable_error(error) is False


@pytest.mark.asyncio
async def test_stream_attempt_forgets_earlier_start_usage() -> None:
    """A Stop before a retry sends its request must not settle the earlier attempt's start usage."""
    model = _model()
    model._stream_start_usage = MessageMetrics(input_tokens=1200, cache_read_tokens=48000)

    def stopped_before_sending(_messages: list[Message]) -> list[Message]:
        raise asyncio.CancelledError

    model._request_messages = stopped_before_sending  # type: ignore[method-assign]
    with pytest.raises(asyncio.CancelledError):
        async for _ in model.ainvoke_stream(_tool_loop_messages(), Message(role="assistant")):
            pass

    assert model.take_unfinished_stream_usage() is None
