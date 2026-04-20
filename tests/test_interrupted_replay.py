"""Tests for canonical interrupted-turn replay persistence."""

from __future__ import annotations

from agno.run.base import RunStatus

from mindroom.history.interrupted_replay import (
    InterruptedReplaySnapshot,
    build_interrupted_replay_run,
)
from mindroom.tool_system.events import ToolTraceEntry


def _assistant_text(run: object) -> str:
    messages = getattr(run, "messages", None) or []
    for message in messages:
        if getattr(message, "role", None) == "assistant":
            content = getattr(message, "content", None)
            if isinstance(content, str):
                return content
    return ""


def test_build_interrupted_replay_run_creates_completed_agent_run_with_marker_and_tools() -> None:
    """Interrupted snapshots should replay through the normal completed history lane."""
    snapshot = InterruptedReplaySnapshot(
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
        response_event_id="$reply",
        interruption_reason="user_cancelled",
    )

    run = build_interrupted_replay_run(
        snapshot=snapshot,
        run_id="run-123",
        scope_id="test_agent",
        session_id="session-1",
        is_team=False,
    )

    assert run.status is RunStatus.completed
    assert _assistant_text(run) == (
        "Half done\n\n"
        "[tool:run_shell_command completed]\n"
        "  args: cmd=pwd\n"
        "  result: /app\n"
        "[tool:save_file interrupted]\n"
        "  args: file_name=main.py\n"
        "  result: <interrupted before completion>\n\n"
        "[interrupted by user]"
    )


def test_build_interrupted_replay_run_tracks_replay_and_seen_event_metadata() -> None:
    """Interrupted replay runs should preserve the event-consumption metadata used by prompt prep."""
    snapshot = InterruptedReplaySnapshot(
        partial_text="Half done",
        completed_tools=(),
        interrupted_tools=(),
        seen_event_ids=("e1", "e2"),
        source_event_id="e1",
        response_event_id="$reply",
        interruption_reason="user_cancelled",
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
