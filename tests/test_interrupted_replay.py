"""Tests for canonical interrupted-turn replay persistence."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agno.models.message import Message
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.session.agent import AgentSession

from mindroom.agents import create_state_storage_db, get_agent_session
from mindroom.history.interrupted_replay import (
    InterruptedReplaySnapshot,
    build_interrupted_replay_run,
    build_interrupted_replay_snapshot,
    persist_interrupted_replay_snapshot,
)
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


def test_persist_interrupted_replay_snapshot_preserves_newer_persisted_runs(tmp_path: Path) -> None:
    """Interrupted replay persistence must merge against the latest stored session state."""
    storage = create_state_storage_db(
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
            partial_text="Half done",
            completed_tools=(),
            interrupted_tools=(),
            run_metadata=None,
            interruption_reason="user_cancelled",
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
