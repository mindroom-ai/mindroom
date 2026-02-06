"""Tests for tool event formatting and metadata payloads."""

from mindroom.tool_events import (
    MAX_TOOL_TRACE_EVENTS,
    TOOL_TRACE_KEY,
    ToolTraceEntry,
    build_tool_trace_content,
    format_tool_completed,
    format_tool_started,
)


def test_format_tool_started_uses_tool_block_and_truncates() -> None:
    """Tool start messages should render as explicit tool blocks."""
    long_contents = "x" * 2000
    text, trace = format_tool_started(
        "save_file",
        {
            "file_name": "notes.txt",
            "contents": f"@mindroom_code:localhost {long_contents}",
        },
    )

    assert text.startswith("\n\n<tool>")
    assert text.endswith("</tool>\n")
    assert "@mindroom_code:localhost" not in text  # mention-neutralized
    assert trace.type == "tool_call_started"
    assert trace.tool_name == "save_file"
    assert trace.args_preview is not None
    assert trace.truncated is True


def test_format_tool_completed_uses_validation_block() -> None:
    """Tool completion messages should render as explicit validation blocks."""
    text, trace = format_tool_completed("run_shell_command", "done " + ("y" * 5000))

    assert text.startswith("<validation>")
    assert text.endswith("</validation>\n\n")
    assert trace.type == "tool_call_completed"
    assert trace.tool_name == "run_shell_command"
    assert trace.result_preview is not None
    assert trace.truncated is True


def test_build_tool_trace_content_limits_event_count() -> None:
    """Tool trace payload should cap stored events and mark overflow."""
    entries = [
        ToolTraceEntry(type="tool_call_started", tool_name=f"tool_{i}") for i in range(MAX_TOOL_TRACE_EVENTS + 5)
    ]
    payload = build_tool_trace_content(entries)
    assert payload is not None
    trace = payload[TOOL_TRACE_KEY]
    assert len(trace["events"]) == MAX_TOOL_TRACE_EVENTS
    assert trace["events_truncated"] == 5
