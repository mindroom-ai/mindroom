"""Tests for AI turn-state recorder helpers."""

from __future__ import annotations

from mindroom.ai_turn_state import AITurnState
from mindroom.history.turn_recorder import TurnRecorder
from mindroom.tool_system.events import ToolTraceEntry


def _tool(name: str) -> ToolTraceEntry:
    return ToolTraceEntry(type="tool_call_completed", tool_name=name)


def test_ai_turn_state_applies_prior_completed_tools_to_recorder_updates() -> None:
    """Prior completed tools are prepended when recording attempt state."""
    recorder = TurnRecorder(user_message="test")
    load_tool = _tool("load_tool")
    shell_tool = _tool("run_shell_command")
    pending_tool = ToolTraceEntry(type="tool_call_started", tool_name="save_file")

    state = AITurnState(prior_completed_tools=(load_tool,))
    state.record_interrupted(
        recorder,
        run_metadata={"run": "1"},
        assistant_text="partial",
        completed_tools=[shell_tool],
        interrupted_tools=[pending_tool],
    )

    assert recorder.assistant_text == "partial"
    assert recorder.run_metadata == {"run": "1"}
    assert [tool.tool_name for tool in recorder.completed_tools] == ["load_tool", "run_shell_command"]
    assert [tool.tool_name for tool in recorder.interrupted_tools] == ["save_file"]
