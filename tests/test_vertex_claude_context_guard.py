"""Exact request fitting for Vertex Claude."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from agno.exceptions import ModelProviderError
from agno.models.message import Message
from agno.models.response import ModelResponse
from agno.models.vertexai.claude import Claude as VertexAIClaude

from mindroom.vertex_claude_compat import MindroomVertexAIClaude

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


@pytest.mark.asyncio
async def test_fit_request_messages_drops_oldest_replay_and_keeps_current_tool_loop() -> None:
    """Exact counting trims history while preserving the full current turn."""
    model = _model()

    async def _count(messages: list[Message], **_kwargs: object) -> int:
        return 110 if len(messages) > 4 else 70

    counter = AsyncMock(side_effect=_count)
    with patch.object(model, "_count_request_input_tokens", new=counter):
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
async def test_fit_request_messages_leaves_fitting_request_unchanged() -> None:
    """Requests inside the exact budget are passed through unchanged."""
    model = _model()
    messages = _tool_loop_messages()
    counter = AsyncMock(return_value=80)

    with patch.object(model, "_count_request_input_tokens", new=counter):
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
        patch.object(model, "_count_request_input_tokens", new=AsyncMock(return_value=90)),
        pytest.raises(ModelProviderError, match="current turn"),
    ):
        await model._fit_request_messages(
            _tool_loop_messages(),
            tools=None,
            response_format=None,
            compress_tool_results=False,
        )
