"""Tests for canonical interrupted-turn replay persistence."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agno.models.message import Message
from agno.models.response import ToolExecution
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.session.agent import AgentSession

from mindroom.agent_storage import create_state_storage, get_agent_session
from mindroom.history.interrupted_replay import (
    InterruptedReplaySnapshot,
    build_interrupted_replay_snapshot,
    persist_interrupted_replay_snapshot,
    split_interrupted_tool_trace,
)
from mindroom.history.interrupted_replay import (
    _build_interrupted_replay_run as build_interrupted_replay_run,
)
from mindroom.history.turn_recorder import TurnRecorder
from mindroom.tool_system.events import ToolTraceEntry

if TYPE_CHECKING:
    from pathlib import Path


def _assistant_text(run: object) -> str:
    messages = getattr(run, "messages", None) or []
    for message in messages:
        if getattr(message, "role", None) == "assistant":
            content = getattr(message, "content", None)
            if isinstance(content, str):
                return content
    return ""


def _completed_run(run_id: str, content: str) -> RunOutput:
    return RunOutput(
        run_id=run_id,
        agent_id="test_agent",
        session_id="session-1",
        content=content,
        messages=[Message(role="assistant", content=content)],
        status=RunStatus.completed,
    )


def test_split_interrupted_tool_trace_treats_explicit_success_without_result_as_completed() -> None:
    """Explicit successful terminal state should win over missing preview text."""
    completed, interrupted = split_interrupted_tool_trace(
        [
            ToolExecution(
                tool_name="noop",
                tool_args={"x": 1},
                result=None,
                tool_call_error=False,
            ),
        ],
    )

    assert [entry.tool_name for entry in completed] == ["noop"]
    assert interrupted == []


def test_split_interrupted_tool_trace_keeps_missing_terminal_state_as_interrupted() -> None:
    """Cancelled tools without an explicit terminal signal should remain interrupted."""
    completed, interrupted = split_interrupted_tool_trace(
        [
            ToolExecution(
                tool_name="noop",
                tool_args={"x": 1},
                result=None,
                tool_call_error=None,
            ),
        ],
    )

    assert completed == []
    assert [entry.tool_name for entry in interrupted] == ["noop"]


def test_build_interrupted_replay_run_creates_completed_agent_run_with_marker_and_tools() -> None:
    """Interrupted snapshots should replay through the normal completed history lane."""
    snapshot = InterruptedReplaySnapshot(
        user_message="Please continue",
        partial_text="Half done",
        completed_tools=(
            ToolTraceEntry(
                type="tool_call_completed",
                tool_name="run_shell_command",
                args_preview="cmd=pwd",
                result_preview="/app",
            ),
        ),
        interrupted_tools=(
            ToolTraceEntry(
                type="tool_call_started",
                tool_name="save_file",
                args_preview="file_name=main.py",
            ),
        ),
        seen_event_ids=("e1", "e2"),
        source_event_id="e1",
        source_event_ids=(),
        source_event_prompts=(),
        response_event_id="$reply",
    )

    run = build_interrupted_replay_run(
        snapshot=snapshot,
        run_id="run-123",
        scope_id="test_agent",
        session_id="session-1",
        is_team=False,
    )

    assert run.status is RunStatus.completed
    assert run.messages is not None
    assert [(message.role, message.content) for message in run.messages] == [
        ("user", "Please continue"),
        (
            "assistant",
            "Half done\n\n"
            "[tool:run_shell_command completed]\n"
            "  args: cmd=pwd\n"
            "  result: /app\n"
            "[tool:save_file interrupted]\n"
            "  args: file_name=main.py\n"
            "  result: <interrupted before completion>\n\n"
            "[interrupted]",
        ),
    ]


def test_build_interrupted_replay_run_tracks_replay_and_seen_event_metadata() -> None:
    """Interrupted replay runs should preserve the event-consumption metadata used by prompt prep."""
    snapshot = InterruptedReplaySnapshot(
        user_message="Please continue",
        partial_text="Half done",
        completed_tools=(),
        interrupted_tools=(),
        seen_event_ids=("e1", "e2"),
        source_event_id="e1",
        source_event_ids=(),
        source_event_prompts=(),
        response_event_id="$reply",
    )

    run = build_interrupted_replay_run(
        snapshot=snapshot,
        run_id="run-123",
        scope_id="test_agent",
        session_id="session-1",
        is_team=False,
    )

    assert run.metadata == {
        "matrix_event_id": "e1",
        "matrix_response_event_id": "$reply",
        "matrix_seen_event_ids": ["e1", "e2"],
        "mindroom_original_status": "cancelled",
        "mindroom_replay_state": "interrupted",
    }


def test_build_interrupted_replay_run_preserves_coalesced_source_metadata() -> None:
    """Interrupted replay runs should round-trip the same coalesced metadata as completed runs."""
    snapshot = build_interrupted_replay_snapshot(
        user_message="Please continue",
        partial_text="Half done",
        completed_tools=(),
        interrupted_tools=(),
        run_metadata={
            "matrix_event_id": "$anchor",
            "matrix_seen_event_ids": ["$first", "$anchor"],
            "matrix_source_event_ids": ["$first", "$anchor"],
            "matrix_source_event_prompts": {"$first": "first", "$anchor": "anchor"},
        },
        response_event_id="$reply",
    )

    run = build_interrupted_replay_run(
        snapshot=snapshot,
        run_id="run-123",
        scope_id="test_agent",
        session_id="session-1",
        is_team=False,
    )

    assert run.metadata == {
        "matrix_event_id": "$anchor",
        "matrix_response_event_id": "$reply",
        "matrix_seen_event_ids": ["$first", "$anchor"],
        "matrix_source_event_ids": ["$first", "$anchor"],
        "matrix_source_event_prompts": {"$first": "first", "$anchor": "anchor"},
        "mindroom_original_status": "cancelled",
        "mindroom_replay_state": "interrupted",
    }


def test_persist_interrupted_replay_snapshot_preserves_newer_persisted_runs(tmp_path: Path) -> None:
    """Interrupted replay persistence must merge against the latest stored session state."""
    storage = create_state_storage(
        "test_agent",
        tmp_path,
        subdir="sessions",
        session_table="test_agent_sessions",
    )
    try:
        storage.upsert_session(
            AgentSession(
                session_id="session-1",
                agent_id="test_agent",
                runs=[
                    _completed_run("old1", "First response"),
                    _completed_run("old2", "Second response"),
                ],
                metadata={},
                created_at=1,
                updated_at=1,
            ),
        )
        stale_session = AgentSession(
            session_id="session-1",
            agent_id="test_agent",
            runs=[_completed_run("old1", "First response")],
            metadata={},
            created_at=1,
            updated_at=1,
        )

        snapshot = build_interrupted_replay_snapshot(
            user_message="Please continue",
            partial_text="Half done",
            completed_tools=(),
            interrupted_tools=(),
            run_metadata=None,
        )
        persist_interrupted_replay_snapshot(
            storage=storage,
            session=stale_session,
            session_id="session-1",
            scope_id="test_agent",
            run_id="cancelled-run",
            snapshot=snapshot,
            is_team=False,
        )

        persisted = get_agent_session(storage, "session-1")
        assert persisted is not None
        assert persisted.runs is not None
        assert [run.run_id for run in persisted.runs] == ["old1", "old2", "cancelled-run"]
    finally:
        storage.close()


def test_turn_recorder_tracks_text_tools_and_metadata() -> None:
    """TurnRecorder should accumulate trusted interrupted-turn runtime facts."""
    recorder = TurnRecorder(
        user_message="Please continue",
        run_metadata={"matrix_event_id": "e1", "matrix_seen_event_ids": ["e1"]},
    )

    recorder.set_assistant_text("Half done")
    recorder.set_completed_tools(
        [
            ToolTraceEntry(
                type="tool_call_completed",
                tool_name="run_shell_command",
                args_preview="cmd=pwd",
                result_preview="/app",
            ),
        ],
    )
    recorder.set_interrupted_tools(
        [
            ToolTraceEntry(
                type="tool_call_started",
                tool_name="save_file",
                args_preview="file_name=main.py",
            ),
        ],
    )
    recorder.mark_interrupted()

    snapshot = recorder.interrupted_snapshot()

    assert snapshot.user_message == "Please continue"
    assert snapshot.partial_text == "Half done"
    assert snapshot.seen_event_ids == ("e1",)
    assert snapshot.source_event_id == "e1"
    assert [tool.tool_name for tool in snapshot.completed_tools] == ["run_shell_command"]
    assert [tool.tool_name for tool in snapshot.interrupted_tools] == ["save_file"]


def test_turn_recorder_record_helpers_capture_completed_and_interrupted_turns() -> None:
    """TurnRecorder helper methods should capture canonical completed/interrupted state."""
    completed_tool = ToolTraceEntry(
        type="tool_call_completed",
        tool_name="run_shell_command",
        args_preview="cmd=pwd",
        result_preview="/app",
    )
    interrupted_tool = ToolTraceEntry(
        type="tool_call_started",
        tool_name="save_file",
        args_preview="file_name=main.py",
    )
    recorder = TurnRecorder(user_message="Please continue")

    recorder.record_completed(
        run_metadata={"matrix_event_id": "e1", "matrix_seen_event_ids": ["e1"]},
        assistant_text="Half done",
        completed_tools=[completed_tool],
    )
    assert recorder.outcome == "completed"
    assert recorder.assistant_text == "Half done"
    assert [tool.tool_name for tool in recorder.completed_tools] == ["run_shell_command"]
    assert recorder.interrupted_tools == []

    recorder.record_interrupted(
        run_metadata={"matrix_event_id": "e2", "matrix_seen_event_ids": ["e2"]},
        assistant_text="Still working",
        completed_tools=[completed_tool],
        interrupted_tools=[interrupted_tool],
    )

    snapshot = recorder.interrupted_snapshot()
    assert recorder.outcome == "interrupted"
    assert snapshot.source_event_id == "e2"
    assert snapshot.partial_text == "Still working"
    assert [tool.tool_name for tool in snapshot.completed_tools] == ["run_shell_command"]
    assert [tool.tool_name for tool in snapshot.interrupted_tools] == ["save_file"]


def test_persist_interrupted_replay_snapshot_keeps_minimal_interrupted_turn(tmp_path: Path) -> None:
    """Even hard-cancelled turns with no observed assistant state should persist one interrupted record."""
    storage = create_state_storage(
        "test_agent",
        tmp_path,
        subdir="sessions",
        session_table="test_agent_sessions",
    )
    try:
        snapshot = build_interrupted_replay_snapshot(
            user_message="Please continue",
            partial_text="",
            completed_tools=(),
            interrupted_tools=(),
            run_metadata={"matrix_event_id": "e1", "matrix_seen_event_ids": ["e1"]},
        )

        persist_interrupted_replay_snapshot(
            storage=storage,
            session=None,
            session_id="session-1",
            scope_id="test_agent",
            run_id="cancelled-run",
            snapshot=snapshot,
            is_team=False,
        )

        persisted = get_agent_session(storage, "session-1")
        assert persisted is not None
        assert persisted.runs is not None
        assert len(persisted.runs) == 1
        assert _assistant_text(persisted.runs[0]) == "[interrupted]"
    finally:
        storage.close()
