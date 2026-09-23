"""Tests for the built-in synthetic model."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from agno.models.message import Message
from agno.models.response import ModelResponse, ModelResponseEvent

from mindroom.synthetic_model import SyntheticModel
from mindroom.tool_system.metadata import get_tool_by_name
from tests.conftest import test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path


def _model(**changes: object) -> SyntheticModel:
    defaults: dict[str, object] = {
        "id": "lorem-ipsum",
        "seed": 17,
        "min_response_chars": 128,
        "max_response_chars": 128,
        "chunk_chars": 17,
        "chars_per_second": 0,
    }
    return SyntheticModel(**(defaults | changes))


def test_streams_exact_lorem_length_in_configured_chunks() -> None:
    """Configured response length and chunk size control streamed Lorem Ipsum."""
    model = _model()

    chunks = [response.content or "" for response in model.invoke_stream([Message(role="user", content="hello")])]
    body = "".join(chunks)

    assert body.startswith("Lorem ipsum")
    assert len(body) == 128
    assert all(len(chunk) == 17 for chunk in chunks[:-1])
    assert len(chunks[-1]) <= 17


def test_fixed_rate_applies_to_each_streamed_chunk() -> None:
    """Each chunk waits for its character count at the configured fixed rate."""
    model = _model(min_response_chars=10, max_response_chars=10, chunk_chars=4, chars_per_second=2)

    with patch("mindroom.synthetic_model.time.sleep") as sleep:
        list(model.invoke_stream([Message(role="user", content="hello")]))

    assert [call.args[0] for call in sleep.call_args_list] == [2.0, 2.0, 1.0]


def test_missing_shell_tool_returns_complete_text_without_tool_call() -> None:
    """Tool selection stays disabled when the agent does not expose shell."""
    model = _model(tool_call_probability=1)

    responses = list(
        model.invoke_stream(
            [Message(role="user", content="hello")],
            tools=[],
        ),
    )

    assert len("".join(response.content or "" for response in responses)) == 128
    assert not any(response.tool_calls for response in responses)


@pytest.mark.asyncio
async def test_shell_tool_runs_echo_hi_then_lorem_stream_continues(tmp_path: Path) -> None:
    """A forced tool turn executes real shell echo and then resumes the same response."""
    model = _model(tool_call_probability=1)
    shell = get_tool_by_name(
        "shell",
        test_runtime_paths(tmp_path),
        disable_sandbox_proxy=True,
        worker_target=None,
    )
    messages = [Message(role="user", content="exercise shell")]
    responses = [
        response
        async for response in model.aresponse_stream(
            messages,
            tools=[shell.async_functions["run_shell_command"]],
        )
        if isinstance(response, ModelResponse)
    ]

    assistant_content = "".join(
        response.content
        for response in responses
        if response.event == ModelResponseEvent.assistant_response.value and isinstance(response.content, str)
    )
    completed_tools = [
        execution
        for response in responses
        if response.event == ModelResponseEvent.tool_call_completed.value
        for execution in response.tool_executions or ()
    ]

    assert len(assistant_content) == 128
    assert assistant_content.startswith("Lorem ipsum")
    assert [execution.tool_name for execution in completed_tools] == ["run_shell_command"]
    assert [execution.tool_args for execution in completed_tools] == [{"args": ["echo", "hi"]}]
    assert [execution.result for execution in completed_tools] == ["hi"]
    assert [message.role for message in messages] == ["user", "assistant", "tool", "assistant"]


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"min_response_chars": 1}, "at least 2"),
        ({"min_response_chars": 10, "max_response_chars": 9}, "greater than or equal"),
        ({"chunk_chars": 0}, "chunk_chars must be positive"),
        ({"chars_per_second": -1}, "chars_per_second must be non-negative"),
        ({"tool_call_probability": 1.1}, "between 0 and 1"),
    ],
)
def test_invalid_settings_fail_at_construction(changes: dict[str, object], message: str) -> None:
    """Invalid generation settings fail before serving any requests."""
    with pytest.raises(ValueError, match=message):
        _model(**changes)
