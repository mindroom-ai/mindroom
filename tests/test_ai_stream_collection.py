"""Tests for collecting stream-shaped AI output into one final response."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest
from agno.models.response import ToolExecution
from agno.run.agent import RunContentEvent, ToolCallCompletedEvent, ToolCallStartedEvent

from mindroom.ai import _collect_streamed_response_content, ai_response
from mindroom.config.main import Config
from mindroom.tool_system.events import ToolTraceEntry
from tests.conftest import make_turn_context

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

    body, trace = await _collect_streamed_response_content(
        stream(),
        show_tool_calls=True,
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

    body, trace = await _collect_streamed_response_content(
        stream(),
        show_tool_calls=False,
    )

    assert body == "Before. After."
    assert trace == []


@pytest.mark.asyncio
async def test_collect_streamed_response_resumes_pending_tool_by_exact_id() -> None:
    """A continuation completes the persisted marker in place and appends later events in order."""
    prior_trace = [
        ToolTraceEntry(
            type="tool_call_started",
            tool_name="inspect",
            args_preview="path=report.txt",
            tool_call_id="call-1",
        ),
    ]

    async def stream() -> AsyncGenerator[object, None]:
        yield ToolCallCompletedEvent(
            tool=ToolExecution(
                tool_call_id="call-1",
                tool_name="inspect",
                tool_args={"path": "report.txt"},
                result="details",
            ),
        )
        yield RunContentEvent(content="\nAfter approval.")
        yield ToolCallStartedEvent(
            tool=ToolExecution(
                tool_call_id="call-2",
                tool_name="inspect",
                tool_args={"path": "report.txt"},
            ),
        )

    body, trace = await _collect_streamed_response_content(
        stream(),
        show_tool_calls=True,
        initial_response_text="Before approval.\n\n🔧 `inspect` [1] ⏳",
        initial_tool_trace=prior_trace,
    )

    assert body == ("Before approval.\n\n🔧 `inspect` [1]\nAfter approval.\n\n🔧 `inspect` [2] ⏳\n\n")
    assert [entry.type for entry in trace] == ["tool_call_completed", "tool_call_started"]
    assert [entry.tool_call_id for entry in trace] == ["call-1", "call-2"]
    assert trace[0].result_preview == "details"


@pytest.mark.asyncio
async def test_collect_streamed_response_does_not_merge_equal_calls_with_distinct_ids() -> None:
    """Argument equality never collapses separate provider calls with stable identities."""

    async def stream() -> AsyncGenerator[object, None]:
        for call_id in ("call-1", "call-2"):
            yield ToolCallStartedEvent(
                tool=ToolExecution(
                    tool_call_id=call_id,
                    tool_name="inspect",
                    tool_args={},
                ),
            )

    body, trace = await _collect_streamed_response_content(stream(), show_tool_calls=True)

    assert body.count("🔧 `inspect`") == 2
    assert [entry.tool_call_id for entry in trace] == ["call-1", "call-2"]


@pytest.mark.asyncio
async def test_collect_streamed_response_ignores_repeated_start_for_restored_call() -> None:
    """A provider replay of the same stable start cannot create a second marker."""
    prior_trace = [
        ToolTraceEntry(type="tool_call_started", tool_name="inspect", tool_call_id="call-1"),
    ]

    async def stream() -> AsyncGenerator[object, None]:
        tool = ToolExecution(tool_call_id="call-1", tool_name="inspect", tool_args={})
        yield ToolCallStartedEvent(tool=tool)
        yield ToolCallCompletedEvent(
            tool=ToolExecution(tool_call_id="call-1", tool_name="inspect", tool_args={}, result="done"),
        )

    body, trace = await _collect_streamed_response_content(
        stream(),
        show_tool_calls=True,
        initial_response_text="🔧 `inspect` [1] ⏳",
        initial_tool_trace=prior_trace,
    )

    assert body == "🔧 `inspect` [1]"
    assert len(trace) == 1
    assert trace[0].type == "tool_call_completed"


@pytest.mark.asyncio
async def test_ai_response_honors_hidden_tool_marker_collection_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit stream collection should still work when inline tool markers are hidden."""
    seen_kwargs: dict[str, object] = {}

    async def fake_stream_agent_response(_ctx: object, **kwargs: object) -> AsyncGenerator[object, None]:
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
        make_turn_context("general", session_id="session"),
        prompt="Read",
        runtime_paths=cast("RuntimePaths", object()),
        config=Config(),
        show_tool_calls=False,
        collect_streamed_response=True,
        tool_trace_collector=trace,
    )

    assert body == "Before. After."
    assert trace == []
    assert seen_kwargs["show_tool_calls"] is False
