"""Tests for bounded active-turn checkpoint decisions and replay persistence."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agno.metrics import MessageMetrics
from agno.models.message import Message
from agno.models.response import ToolExecution
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.session.agent import AgentSession

from mindroom.active_turn_checkpoint import (
    _MAX_TOOL_CONTEXT_CHARS,
    ActiveTurnCheckpointTrigger,
    _render_tool_checkpoint_sections,
    build_active_turn_checkpoint,
    build_active_turn_context_guard,
    install_active_turn_checkpoint_hook,
)
from mindroom.agent_storage import create_state_storage, get_agent_session
from mindroom.history.continuation_checkpoint import persist_continuation_checkpoint
from mindroom.tool_system.events import ToolTraceEntry

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


class _FakeModel:
    """Small Agno-model surface used to exercise result formatting hooks."""

    def format_function_call_results(
        self,
        messages: list[Message],
        function_call_results: list[Message],
        compress_tool_results: bool = False,
        **_kwargs: object,
    ) -> None:
        _ = compress_tool_results
        messages.extend(function_call_results)


def _tool_boundary(
    result: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> tuple[list[Message], list[Message]]:
    messages = [
        Message(role="user", content="work"),
        Message(
            role="assistant",
            content="calling tool",
            metrics=MessageMetrics(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read_tokens,
                cache_write_tokens=cache_write_tokens,
            ),
        ),
    ]
    results = [Message(role="tool", tool_call_id="call-1", tool_name="side_effect", content=result)]
    return messages, results


def _trigger() -> ActiveTurnCheckpointTrigger:
    return ActiveTurnCheckpointTrigger(
        estimated_input_tokens=920,
        input_limit_tokens=900,
        context_window_tokens=1_000,
        used_actual_input_tokens=True,
    )


def test_guard_uses_latest_actual_provider_input_at_completed_boundary() -> None:
    """Actual request usage wins and the normal Agno stop flag is set after tool completion."""
    guard = build_active_turn_context_guard(
        context_window_tokens=1_000,
        headroom_tokens=100,
        prepared_context_tokens=100,
        configured_provider="anthropic",
        model_id="claude-sonnet-5",
    )
    assert guard is not None
    model = _FakeModel()
    install_active_turn_checkpoint_hook(model, guard)  # type: ignore[arg-type]
    messages, results = _tool_boundary(
        "created artifact.txt",
        input_tokens=100,
        output_tokens=10,
        cache_read_tokens=750,
    )

    model.format_function_call_results(messages, results)

    assert results[0].stop_after_tool_call is True
    assert guard.trigger is not None
    assert guard.trigger.used_actual_input_tokens is True
    assert guard.trigger.estimated_input_tokens >= guard.trigger.input_limit_tokens


def test_guard_falls_back_to_conservative_cumulative_estimate() -> None:
    """Missing provider usage accumulates completed batches from prepared-context estimate."""
    guard = build_active_turn_context_guard(
        context_window_tokens=1_000,
        headroom_tokens=200,
        prepared_context_tokens=300,
    )
    assert guard is not None
    model = _FakeModel()
    install_active_turn_checkpoint_hook(model, guard)  # type: ignore[arg-type]

    first_messages, first_results = _tool_boundary("a" * 400)
    model.format_function_call_results(first_messages, first_results)
    second_messages, second_results = _tool_boundary("b" * 400)
    model.format_function_call_results(second_messages, second_results)

    assert first_results[0].stop_after_tool_call is False
    assert second_results[0].stop_after_tool_call is True
    assert guard.trigger is not None
    assert guard.trigger.used_actual_input_tokens is False


def test_guard_is_disabled_without_effective_context_limit() -> None:
    """Unknown context windows do not pretend a safe checkpoint threshold exists."""
    assert (
        build_active_turn_context_guard(
            context_window_tokens=None,
            headroom_tokens=100,
            prepared_context_tokens=300,
        )
        is None
    )


def test_checkpoint_replaces_raw_tool_run_without_replayable_calls(tmp_path: Path) -> None:
    """Persisted checkpoint keeps results as prose and removes executable tool history."""
    storage = create_state_storage(
        "general",
        tmp_path,
        subdir="sessions",
        session_table="general_sessions",
    )
    try:
        raw_run = RunOutput(
            run_id="run-tool-heavy",
            agent_id="general",
            session_id="session-1",
            content=None,
            messages=[
                Message(role="assistant", content="calling side effect"),
                Message(role="tool", tool_call_id="call-1", tool_name="create_file", content="artifact.txt"),
            ],
            tools=[
                ToolExecution(
                    tool_call_id="call-1",
                    tool_name="create_file",
                    tool_args={"path": "artifact.txt"},
                    result="artifact.txt",
                ),
            ],
            metadata={"matrix_event_id": "$event"},
            status=RunStatus.completed,
        )
        session = AgentSession(
            session_id="session-1",
            agent_id="general",
            runs=[raw_run],
            metadata={},
            created_at=1,
            updated_at=1,
        )
        storage.upsert_session(session)
        checkpoint = build_active_turn_checkpoint(
            goal="Create the requested artifact",
            partial_text="Artifact creation finished.",
            completed_tools=[
                ToolTraceEntry(
                    type="tool_call_completed",
                    tool_name="create_file",
                    args_preview="path=artifact.txt",
                    result_preview="artifact.txt",
                ),
            ],
            trigger=_trigger(),
        )

        for _ in range(2):
            persist_continuation_checkpoint(
                storage=storage,
                session=session,
                session_id="session-1",
                scope_id="general",
                run_id="run-tool-heavy",
                checkpoint=checkpoint,
                run_metadata={"matrix_event_id": "$event"},
            )

        persisted = get_agent_session(storage, "session-1")
        assert persisted is not None
        assert persisted.runs is not None
        assert len(persisted.runs) == 1
        checkpoint_run = persisted.runs[0]
        assert isinstance(checkpoint_run, RunOutput)
        assert not checkpoint_run.tools
        assert all(message.role != "tool" for message in checkpoint_run.messages or [])
        assert "artifact.txt" in str(checkpoint_run.content)
        assert checkpoint_run.metadata is not None
        assert checkpoint_run.metadata["mindroom_replay_state"] == "active_turn_checkpoint"
        assert checkpoint_run.metadata["matrix_event_id"] == "$event"
        assert session.runs is not None
        assert isinstance(session.runs[0], RunOutput)
        assert not session.runs[0].tools
        assert all(message.role != "tool" for message in session.runs[0].messages or [])
    finally:
        storage.close()


def test_checkpoint_content_stays_bounded_for_long_tool_sequence() -> None:
    """Many large tool previews collapse into one bounded continuation checkpoint."""
    completed_tools = [
        ToolTraceEntry(
            type="tool_call_completed",
            tool_name=f"tool-{index}",
            args_preview="a" * 1_200,
            result_preview="r" * 500,
            truncated=True,
        )
        for index in range(120)
    ]
    checkpoint = build_active_turn_checkpoint(
        goal="Finish the long task",
        partial_text="working",
        completed_tools=completed_tools,
        trigger=_trigger(),
    )
    completed_work, key_results = _render_tool_checkpoint_sections(completed_tools)

    assert len(checkpoint.content) < 30_000
    assert len(completed_work) + len(key_results) <= _MAX_TOOL_CONTEXT_CHARS
    assert "omitted to keep the checkpoint bounded" in checkpoint.content
    assert "Pending steps:" in checkpoint.content


def test_tool_checkpoint_omission_handles_tiny_character_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """An undersized bound still produces bounded prose instead of popping an empty list."""
    monkeypatch.setattr("mindroom.active_turn_checkpoint._MAX_TOOL_CONTEXT_CHARS", 20)

    completed_work, key_results = _render_tool_checkpoint_sections(
        [
            ToolTraceEntry(
                type="tool_call_completed",
                tool_name="large-tool",
                args_preview="a" * 100,
                result_preview="r" * 100,
            ),
        ],
    )

    assert len(completed_work) + len(key_results) <= 20
