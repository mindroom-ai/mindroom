"""Exact request fitting for Vertex Claude."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from agno.exceptions import ModelProviderError
from agno.models.message import Message

from mindroom.vertex_claude_compat import MindroomVertexAIClaude


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
        Message(role="user", content="old question"),
        Message(role="assistant", content="old answer"),
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

    model._count_request_input_tokens = AsyncMock(side_effect=_count)  # type: ignore[method-assign]

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


@pytest.mark.asyncio
async def test_fit_request_messages_leaves_fitting_request_unchanged() -> None:
    """Requests inside the exact budget are passed through unchanged."""
    model = _model()
    messages = _tool_loop_messages()
    model._count_request_input_tokens = AsyncMock(return_value=80)  # type: ignore[method-assign]

    fitted = await model._fit_request_messages(
        messages,
        tools=None,
        response_format=None,
        compress_tool_results=False,
    )

    assert fitted is messages
    model._count_request_input_tokens.assert_awaited_once()


@pytest.mark.asyncio
async def test_fit_request_messages_rejects_current_turn_that_cannot_fit() -> None:
    """The guard fails visibly instead of sending an oversized current turn."""
    model = _model()
    model._count_request_input_tokens = AsyncMock(return_value=90)  # type: ignore[method-assign]

    with pytest.raises(ModelProviderError, match="current turn"):
        await model._fit_request_messages(
            _tool_loop_messages(),
            tools=None,
            response_format=None,
            compress_tool_results=False,
        )
