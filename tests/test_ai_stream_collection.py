"""Tests for collecting stream-shaped AI output into one final response."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest
from agno.models.response import ToolExecution
from agno.run.agent import RunContentEvent, ToolCallCompletedEvent, ToolCallStartedEvent

from mindroom.ai import _collect_streamed_response_content, ai_response
from mindroom.config.main import Config
from mindroom.tool_system.events import ToolTraceEntry

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from mindroom.constants import RuntimePaths


@pytest.mark.asyncio
async def test_collect_streamed_response_preserves_tool_marker_order() -> None:
    """Silent collection should keep the same relative tool placement as streaming delivery."""

    async def stream() -> AsyncGenerator[object, None]:
        yield RunContentEvent(content="Before tool.\n")
        yield ToolCallStartedEvent(
            tool=ToolExecution(tool_name="run_shell_command", tool_args={"cmd": "git status"}),
        )
        yield RunContentEvent(content="\nAfter tool.")
        yield ToolCallCompletedEvent(
            tool=ToolExecution(
                tool_name="run_shell_command",
                tool_args={"cmd": "git status"},
                result="clean",
            ),
        )

    trace: list[ToolTraceEntry] = []
    body = await _collect_streamed_response_content(
        stream(),
        show_tool_calls=True,
        tool_trace_collector=trace,
    )

    assert body.index("Before tool.") < body.index("run_shell_command") < body.index("After tool.")
    assert trace == [
        ToolTraceEntry(
            type="tool_call_completed",
            tool_name="run_shell_command",
            args_preview="cmd=git status",
            result_preview="clean",
        ),
    ]


@pytest.mark.asyncio
async def test_collect_streamed_response_can_hide_tool_markers() -> None:
    """The collector still supports hidden-tool-call responses."""

    async def stream() -> AsyncGenerator[object, None]:
        yield RunContentEvent(content="Before.")
        yield ToolCallStartedEvent(tool=ToolExecution(tool_name="read_file", tool_args={"path": "README.md"}))
        yield ToolCallCompletedEvent(
            tool=ToolExecution(tool_name="read_file", tool_args={"path": "README.md"}, result="content"),
        )
        yield RunContentEvent(content=" After.")

    trace: list[ToolTraceEntry] = []
    body = await _collect_streamed_response_content(
        stream(),
        show_tool_calls=False,
        tool_trace_collector=trace,
    )

    assert body == "Before. After."
    assert trace == []


@pytest.mark.asyncio
async def test_ai_response_honors_hidden_tool_marker_collection_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit stream collection should still work when inline tool markers are hidden."""
    seen_kwargs: dict[str, object] = {}

    async def fake_stream_agent_response(**kwargs: object) -> AsyncGenerator[object, None]:
        seen_kwargs.update(kwargs)
        yield RunContentEvent(content="Before.")
        yield ToolCallStartedEvent(tool=ToolExecution(tool_name="read_file", tool_args={"path": "README.md"}))
        yield ToolCallCompletedEvent(
            tool=ToolExecution(tool_name="read_file", tool_args={"path": "README.md"}, result="content"),
        )
        yield RunContentEvent(content=" After.")

    monkeypatch.setattr("mindroom.ai.stream_agent_response", fake_stream_agent_response)

    trace: list[ToolTraceEntry] = []
    body = await ai_response(
        agent_name="general",
        prompt="Read",
        session_id="session",
        runtime_paths=cast("RuntimePaths", object()),
        config=Config(),
        show_tool_calls=False,
        collect_streamed_response=True,
        tool_trace_collector=trace,
    )

    assert body == "Before. After."
    assert trace == []
    assert seen_kwargs["show_tool_calls"] is False
