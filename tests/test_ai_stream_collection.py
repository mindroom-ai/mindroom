"""Tests for collecting stream-shaped AI output into one final response."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from agno.models.response import ToolExecution
from agno.run.agent import RunContentEvent, ToolCallCompletedEvent, ToolCallStartedEvent

from mindroom.ai import _collect_streamed_response_content
from mindroom.tool_system.events import ToolTraceEntry

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


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
