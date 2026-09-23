"""Tests for collecting stream-shaped AI output into one final response."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from agno.models.response import ToolExecution
from agno.run.agent import RunCompletedEvent, RunContentEvent, ToolCallCompletedEvent, ToolCallStartedEvent

from mindroom.ai import ai_response, collect_streamed_response_content
from mindroom.config.main import Config
from mindroom.response_turn import PausedAttempt, ResponsePausedForApproval
from mindroom.tool_jobs.completion import background_wait_notice
from mindroom.tool_system.events import (
    BackgroundWaitChunk,
    CollectedStreamPresentation,
    ToolTraceEntry,
    tool_markers_match_trace,
)
from tests.conftest import make_turn_context, test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path

    from mindroom.streaming import StreamingPresentation


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
        yield RunContentEvent(content="Before.")
        yield ToolCallStartedEvent(tool=ToolExecution(tool_name="read_file", tool_args={"path": "README.md"}))
        yield ToolCallCompletedEvent(
            tool=ToolExecution(tool_name="read_file", tool_args={"path": "README.md"}, result="content"),
        )
        yield RunContentEvent(content=" After.")

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
            yield ToolCallStartedEvent(
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
        yield ToolCallStartedEvent(tool=tool)
        yield ToolCallCompletedEvent(
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
@pytest.mark.parametrize("quiet", [False, True])
async def test_collected_wait_updates_owner_before_requesting_next_chunk(*, quiet: bool) -> None:
    """A nonstream Matrix response must expose wait progress before its generator parks."""
    notices: list[tuple[str, str | None]] = []

    async def notice(presentation: StreamingPresentation, notice: str | None) -> None:
        notices.append((presentation.response_text, notice))

    async def stream() -> AsyncGenerator[object, None]:
        yield "Independent work done."
        yield BackgroundWaitChunk(" Waiting for background work")
        assert notices == [("Independent work done.", " Waiting for background work")]
        yield " Result received."

    with background_wait_notice(notice):
        body, _trace = await collect_streamed_response_content(
            stream(),
            presentation=CollectedStreamPresentation(show_tool_calls=True),
            suppress_quiet_attempts=quiet,
        )
    assert body == "Independent work done. Result received."


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_only", [False, True])
@pytest.mark.parametrize("show_tools", [False, True])
@pytest.mark.parametrize("first", ["NO_REPLY", "First finding"])
async def test_quiet_collection_preserves_attempt_tool_order(
    *,
    terminal_only: bool,
    show_tools: bool,
    first: str,
) -> None:
    """Exact quiet attempts vanish without moving tools or removing literal control-token prose."""
    first_tool = ToolExecution(tool_call_id="one", tool_name="first_tool", tool_args={}, result="first")
    second_tool = ToolExecution(tool_call_id="two", tool_name="second_tool", tool_args={}, result="second")

    async def stream() -> AsyncGenerator[object, None]:
        if not terminal_only:
            yield RunContentEvent(content=first[:3])
        yield ToolCallStartedEvent(tool=first_tool)
        if not terminal_only:
            yield RunContentEvent(content=first[3:])
        yield ToolCallCompletedEvent(tool=first_tool)
        yield RunCompletedEvent(content=first)
        if terminal_only:
            yield RunContentEvent(content=first)
        yield RunContentEvent(content="The literal NO_REPLY stays.")
        yield ToolCallStartedEvent(tool=second_tool)
        yield RunContentEvent(content="Final finding.")
        yield ToolCallCompletedEvent(tool=second_tool)
        yield RunCompletedEvent(content="The literal NO_REPLY stays.Final finding.")

    body, trace = await collect_streamed_response_content(
        stream(),
        presentation=CollectedStreamPresentation(show_tool_calls=show_tools),
        suppress_quiet_attempts=True,
    )
    assert body.count("NO_REPLY") == 1
    assert body.count("The literal NO_REPLY stays.") == 1
    assert body.count("Final finding.") == 1
    if first != "NO_REPLY":
        assert body.count("Fir") == body.count("st finding") == 1
    if show_tools:
        assert tool_markers_match_trace(body, trace)
        assert body.index("`first_tool`") < body.index("The literal NO_REPLY stays.")
        assert body.index("The literal NO_REPLY stays.") < body.index("`second_tool`") < body.index("Final finding.")
        assert [entry.result_preview for entry in trace] == ["first", "second"]
    else:
        assert trace == []
        assert "🔧" not in body


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["error", "cancel", "approval"])
async def test_quiet_collection_preserves_unfinished_presentation(interruption: str) -> None:
    """A partial attempt remains available to cancellation and approval presentation owners."""
    tool = ToolExecution(tool_call_id="pending", tool_name="pending_tool", tool_args={})
    failures = {
        "error": RuntimeError("Interrupted"),
        "cancel": asyncio.CancelledError(),
        "approval": ResponsePausedForApproval(
            PausedAttempt(session_id="session", run_id="run", tools=(tool,), toolkit_owners={}),
        ),
    }
    failure = failures[interruption]

    async def stream() -> AsyncGenerator[object, None]:
        yield RunContentEvent(content="Partial finding.")
        yield ToolCallStartedEvent(tool=tool)
        raise failure

    presentation = CollectedStreamPresentation(show_tool_calls=True)
    with pytest.raises(type(failure)):
        await collect_streamed_response_content(stream(), presentation=presentation, suppress_quiet_attempts=True)
    assert presentation.response_text.startswith("Partial finding.")
    assert tool_markers_match_trace(presentation.response_text, presentation.tool_trace)
    assert presentation.tool_trace[0].type == "tool_call_started"
    if isinstance(failure, ResponsePausedForApproval):
        assert failure.presentation.response_text == presentation.response_text.rstrip()
        assert failure.presentation.tool_trace == tuple(presentation.tool_trace)
