"""Tests for collecting stream-shaped AI output into one final response."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom.ai import ai_response, collect_streamed_response_content
from mindroom.config.main import Config
from mindroom.llm.events import ContentDelta, ToolCompleted, ToolStarted
from mindroom.llm.runs import ToolExecution
from mindroom.tool_system.events import CollectedStreamPresentation, ToolTraceEntry
from tests.conftest import make_turn_context, test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path


@pytest.mark.asyncio
async def test_collect_streamed_response_preserves_tool_marker_order() -> None:
    """Silent collection should keep the same relative tool placement as streaming delivery."""

    async def stream() -> AsyncGenerator[object, None]:
        yield ContentDelta("Before tool.\n")
        yield ToolStarted(
            tool=ToolExecution(tool_name="run_shell_command", tool_args={"cmd": "git status"}),
        )
        yield ContentDelta("\nAfter tool.")
        yield ToolCompleted(
            tool=ToolExecution(
                tool_name="run_shell_command",
                tool_args={"cmd": "git status"},
                result="clean",
            ),
        )

    body, trace = await collect_streamed_response_content(
        stream(),
        presentation=CollectedStreamPresentation(show_tool_calls=True),
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
        yield ContentDelta("Before.")
        yield ToolStarted(tool=ToolExecution(tool_name="read_file", tool_args={"path": "README.md"}))
        yield ToolCompleted(
            tool=ToolExecution(tool_name="read_file", tool_args={"path": "README.md"}, result="content"),
        )
        yield ContentDelta(" After.")

    body, trace = await collect_streamed_response_content(
        stream(),
        presentation=CollectedStreamPresentation(show_tool_calls=False),
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
        yield ToolCompleted(
            tool=ToolExecution(
                tool_call_id="call-1",
                tool_name="inspect",
                tool_args={"path": "report.txt"},
                result="details",
            ),
        )
        yield ContentDelta("\nAfter approval.")
        yield ToolStarted(
            tool=ToolExecution(
                tool_call_id="call-2",
                tool_name="inspect",
                tool_args={"path": "report.txt"},
            ),
        )

    body, trace = await collect_streamed_response_content(
        stream(),
        presentation=CollectedStreamPresentation(
            show_tool_calls=True,
            response_text="Before approval.\n\n🔧 `inspect` [1] ⏳",
            tool_trace=prior_trace,
        ),
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
            yield ToolStarted(
                tool=ToolExecution(
                    tool_call_id=call_id,
                    tool_name="inspect",
                    tool_args={},
                ),
            )

    body, trace = await collect_streamed_response_content(
        stream(),
        presentation=CollectedStreamPresentation(show_tool_calls=True),
    )

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
        yield ToolStarted(tool=tool)
        yield ToolCompleted(
            tool=ToolExecution(tool_call_id="call-1", tool_name="inspect", tool_args={}, result="done"),
        )

    body, trace = await collect_streamed_response_content(
        stream(),
        presentation=CollectedStreamPresentation(
            show_tool_calls=True,
            response_text="🔧 `inspect` [1] ⏳",
            tool_trace=prior_trace,
        ),
    )

    assert body == "🔧 `inspect` [1]"
    assert len(trace) == 1
    assert trace[0].type == "tool_call_completed"


@pytest.mark.asyncio
async def test_ai_response_honors_hidden_tool_marker_collection_opt_in(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Explicit stream collection should still work when inline tool markers are hidden."""
    seen_kwargs: dict[str, object] = {}

    async def fake_stream_agent_response(_ctx: object, **kwargs: object) -> AsyncGenerator[object, None]:
        seen_kwargs.update(kwargs)
        yield ContentDelta("Before.")
        yield ToolStarted(tool=ToolExecution(tool_name="read_file", tool_args={"path": "README.md"}))
        yield ToolCompleted(
            tool=ToolExecution(tool_name="read_file", tool_args={"path": "README.md"}, result="content"),
        )
        yield ContentDelta(" After.")

    monkeypatch.setattr("mindroom.ai.stream_agent_response", fake_stream_agent_response)

    trace: list[ToolTraceEntry] = []
    body = await ai_response(
        make_turn_context("general", session_id="session"),
        prompt="Read",
        runtime_paths=test_runtime_paths(tmp_path),
        config=Config(),
        show_tool_calls=False,
        collect_streamed_response=True,
        tool_trace_collector=trace,
    )

    assert body == "Before. After."
    assert trace == []
    assert seen_kwargs["show_tool_calls"] is False


@pytest.mark.asyncio
async def test_a_collector_whose_update_fails_closes_the_stream_before_the_failure_propagates() -> None:
    """The stream's producer stops when its collector fails, not whenever the abandoned stream is collected."""
    closed: list[bool] = []

    async def stream() -> AsyncGenerator[object, None]:
        try:
            yield ContentDelta("Working.")
            yield ContentDelta(" More.")
        finally:
            closed.append(True)

    async def fail() -> None:
        msg = "delivery failed"
        raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="delivery failed"):
        await collect_streamed_response_content(
            stream(),
            presentation=CollectedStreamPresentation(show_tool_calls=True),
            on_update=fail,
        )
    assert closed == [True]
