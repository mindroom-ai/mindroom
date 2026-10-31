"""Tests for collecting stream-shaped AI output into one final response."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from mindroom.ai import (
    _materialize_paused_agent_tool_events,
    _process_stream_events,
    _StreamingAttemptState,
    ai_response,
)
from mindroom.config.main import Config
from mindroom.llm.events import ReasoningDelta, RunFinished, TextDelta, ToolCompleted, ToolProgress, ToolStarted
from mindroom.llm.runs import RunRecord, RunRequirement, ToolExecution
from mindroom.response_turn import (
    PausedAttempt,
    ResponsePausedForApproval,
    collect_streamed_response_content,
)
from mindroom.tool_system.events import CollectedStreamPresentation, StructuredStreamChunk, ToolTraceEntry
from tests.conftest import make_turn_context, test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path

    from mindroom.response_turn import ResponseTurnContext


@pytest.mark.asyncio
async def test_collect_streamed_response_preserves_tool_marker_order() -> None:
    """Silent collection should keep the same relative tool placement as streaming delivery."""

    async def stream() -> AsyncGenerator[object]:
        yield TextDelta("Before tool.\n")
        yield ToolStarted(
            call=ToolExecution(
                tool_call_id="call-run_shell_command",
                tool_name="run_shell_command",
                tool_args={"cmd": "git status"},
            ).call,
        )
        yield TextDelta("\nAfter tool.")
        yield ToolCompleted(
            call=ToolExecution(
                tool_call_id="call-run_shell_command",
                tool_name="run_shell_command",
                tool_args={"cmd": "git status"},
                result="clean",
            ).call,
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

    async def stream() -> AsyncGenerator[object]:
        yield TextDelta("Before.")
        yield ToolStarted(
            call=ToolExecution(
                tool_call_id="call-read_file",
                tool_name="read_file",
                tool_args={"path": "README.md"},
            ).call,
        )
        yield ToolCompleted(
            call=ToolExecution(
                tool_call_id="call-read_file",
                tool_name="read_file",
                tool_args={"path": "README.md"},
                result="content",
            ).call,
        )
        yield TextDelta(" After.")

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

    async def stream() -> AsyncGenerator[object]:
        yield ToolCompleted(
            call=ToolExecution(
                tool_call_id="call-1",
                tool_name="inspect",
                tool_args={"path": "report.txt"},
                result="details",
            ).call,
        )
        yield TextDelta("\nAfter approval.")
        yield ToolStarted(
            call=ToolExecution(
                tool_call_id="call-2",
                tool_name="inspect",
                tool_args={"path": "report.txt"},
            ).call,
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

    async def stream() -> AsyncGenerator[object]:
        for call_id in ("call-1", "call-2"):
            yield ToolStarted(
                call=ToolExecution(
                    tool_call_id=call_id,
                    tool_name="inspect",
                    tool_args={},
                ).call,
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

    async def stream() -> AsyncGenerator[object]:
        tool = ToolExecution(tool_call_id="call-1", tool_name="inspect", tool_args={})
        yield ToolStarted(call=tool.call)
        yield ToolCompleted(
            call=ToolExecution(tool_call_id="call-1", tool_name="inspect", tool_args={}, result="done").call,
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
async def test_a_paused_call_the_stream_already_announced_is_not_announced_again() -> None:
    """A paused run's calls are announced once: those its stream started already are skipped, and each counts once."""
    announced = ToolExecution(tool_call_id="call-announced", tool_name="save_file", tool_args={"file": "a.py"})
    unannounced = ToolExecution(tool_call_id="call-unannounced", tool_name="send_message", tool_args={"text": "hi"})

    async def stream() -> AsyncGenerator[object]:
        yield ToolStarted(call=announced.call)

    state = _StreamingAttemptState()
    _ = [
        chunk
        async for chunk in _process_stream_events(
            stream(),
            state=state,
            show_tool_calls=True,
            show_reasoning=False,
            agent_name="a",
        )
    ]
    events = _materialize_paused_agent_tool_events(state, [announced, unannounced])

    assert [event.call.tool_call_id for event in events] == ["call-unannounced"]
    assert state.observed_tool_calls == 2
    assert [pending.tool_call_id for pending in state.pending_tools] == ["call-announced", "call-unannounced"]


@pytest.mark.asyncio
async def test_ai_response_collects_the_stream_without_tool_markers_when_tool_calls_are_hidden(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A reply that hides its tool calls collects its streamed text without their markers, as the streamer shows it.

    The text around a hidden call stays a paragraph apart, and the collected trace still names the call.
    """
    seen_show_tool_calls: list[bool] = []

    async def fake_stream_agent_response(
        ctx: ResponseTurnContext,
        *_args: object,
        **_kwargs: object,
    ) -> AsyncGenerator[object]:
        seen_show_tool_calls.append(ctx.show_tool_calls)
        yield TextDelta("Before.")
        yield ToolStarted(
            call=ToolExecution(
                tool_call_id="call-read_file",
                tool_name="read_file",
                tool_args={"path": "README.md"},
            ).call,
        )
        yield ToolCompleted(
            call=ToolExecution(
                tool_call_id="call-read_file",
                tool_name="read_file",
                tool_args={"path": "README.md"},
                result="content",
            ).call,
        )
        yield TextDelta(" After.")

    monkeypatch.setattr("mindroom.ai.stream_agent_response", fake_stream_agent_response)

    body = await ai_response(
        replace(make_turn_context("general", session_id="session"), prompt="Read", show_tool_calls=False),
        test_runtime_paths(tmp_path),
        Config(),
    )

    assert body.text == "Before.\n\n After."
    assert seen_show_tool_calls == [False]


@pytest.mark.asyncio
async def test_a_collector_whose_update_fails_closes_the_stream_before_the_failure_propagates() -> None:
    """The stream's producer stops when its collector fails, not whenever the abandoned stream is collected."""
    closed: list[bool] = []

    async def stream() -> AsyncGenerator[object]:
        try:
            yield TextDelta("Working.")
            yield TextDelta(" More.")
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


@pytest.mark.asyncio
async def test_a_run_that_streamed_no_text_is_collected_as_its_final_body() -> None:
    """A run whose text arrived only in its final record is the collected reply."""

    async def stream() -> AsyncGenerator[object]:
        yield RunFinished(RunRecord(run_id="run-final", content="Final only."))

    body, trace = await collect_streamed_response_content(
        stream(),
        presentation=CollectedStreamPresentation(show_tool_calls=True),
    )

    assert (body, trace) == ("Final only.", [])


@pytest.mark.asyncio
async def test_a_collected_team_pause_keeps_the_document_state_its_resume_restores() -> None:
    """A team document collected whole hands its structured state to the approval pause."""
    state: dict[str, object] = {"kind": "team_stream", "members": ["general"]}
    pause = ResponsePausedForApproval(
        PausedAttempt(
            session_id="session-1",
            run_id="run-paused",
            requirements=tuple(map(RunRequirement, ())),
            toolkit_owners={},
        ),
    )

    async def stream() -> AsyncGenerator[object]:
        yield StructuredStreamChunk(content="Team document.", tool_trace=[], presentation_state=state)
        raise pause

    with pytest.raises(ResponsePausedForApproval):
        await collect_streamed_response_content(
            stream(),
            presentation=CollectedStreamPresentation(show_tool_calls=True),
        )

    assert pause.presentation is not None
    assert (pause.presentation.response_text, pause.presentation.state) == ("Team document.", state)


@pytest.mark.asyncio
async def test_text_after_a_whole_document_replaces_it_in_a_document_stream() -> None:
    """A team's failure or notice replaces the document it collected, as the streamed team reply shows it."""

    async def stream() -> AsyncGenerator[object]:
        yield StructuredStreamChunk(content="Team document.", tool_trace=[])
        yield "⚠️ Error: the leader failed"

    body, _trace = await collect_streamed_response_content(
        stream(),
        presentation=CollectedStreamPresentation(show_tool_calls=True, replaces_text=True),
    )

    assert body == "⚠️ Error: the leader failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("show_reasoning", [True, False])
async def test_reasoning_reaches_the_stream_only_when_the_turn_shows_it(show_reasoning: bool) -> None:
    """`show_reasoning: false` keeps the model's reasoning out of the reply stream entirely."""

    async def stream() -> AsyncGenerator[object]:
        yield ReasoningDelta("Checking the cache")
        yield TextDelta("Answer")

    chunks = [
        chunk
        async for chunk in _process_stream_events(
            stream(),
            state=_StreamingAttemptState(),
            show_tool_calls=True,
            show_reasoning=show_reasoning,
            agent_name="a",
        )
    ]

    expected = [ReasoningDelta("Checking the cache"), TextDelta("Answer")] if show_reasoning else [TextDelta("Answer")]
    assert chunks == expected


@pytest.mark.asyncio
async def test_a_collected_reply_drops_reasoning() -> None:
    """A reply that is not streamed keeps its answer and drops the reasoning its stream carried."""

    async def stream() -> AsyncGenerator[object]:
        yield ReasoningDelta("Checking the cache")
        yield TextDelta("Answer")

    body, trace = await collect_streamed_response_content(
        stream(),
        presentation=CollectedStreamPresentation(show_tool_calls=True),
    )

    assert body == "Answer"
    assert trace == []


@pytest.mark.asyncio
async def test_a_running_tools_output_reaches_the_reply_stream() -> None:
    """The progress a running call reports passes through to the reply stream unchanged."""

    async def stream() -> AsyncGenerator[object]:
        yield ToolProgress("call-1", "step 1")
        yield TextDelta("Answer")

    chunks = [
        chunk
        async for chunk in _process_stream_events(
            stream(),
            state=_StreamingAttemptState(),
            show_tool_calls=True,
            show_reasoning=True,
            agent_name="a",
        )
    ]

    assert chunks == [ToolProgress("call-1", "step 1"), TextDelta("Answer")]


@pytest.mark.asyncio
async def test_a_collected_reply_keeps_tool_output_out_of_its_answer() -> None:
    """A collected reply's answer and trace never include a running call's output; its presentation keeps it to publish."""
    presentation = CollectedStreamPresentation(show_tool_calls=True)

    async def stream() -> AsyncGenerator[object]:
        yield TextDelta("A")
        yield ToolProgress("call-1", "step 1")
        yield TextDelta("nswer")

    body, trace = await collect_streamed_response_content(stream(), presentation=presentation)

    assert body == "Answer"
    assert trace == []
    assert presentation.tool_progress == {"call-1": "step 1"}
