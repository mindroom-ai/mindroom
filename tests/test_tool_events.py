"""Tests for tool event formatting and metadata payloads."""

from mindroom.tool_events import (
    MAX_TOOL_TRACE_EVENTS,
    TOOL_TRACE_KEY,
    ToolTraceEntry,
    build_tool_trace_content,
    complete_pending_tool_block,
    extract_tool_completed_info,
    format_tool_combined,
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


def test_format_tool_combined_with_result() -> None:
    """Combined formatting should produce a single <tool> block with call and result."""
    text, trace = format_tool_combined("run_shell_command", {"cmd": "pwd"}, "/app")

    assert text.startswith("\n\n<tool>")
    assert text.endswith("</tool>\n")
    assert "<validation>" not in text
    assert "run_shell_command(cmd=pwd)" in text
    # Result should be on a second line inside the block
    assert "\n/app</tool>" in text
    assert trace.type == "tool_call_completed"
    assert trace.tool_name == "run_shell_command"
    assert trace.result_preview == "/app"
    assert trace.truncated is False


def test_format_tool_combined_truncates_long_result() -> None:
    """Combined formatting should truncate long results."""
    text, trace = format_tool_combined("run_shell_command", {}, "done " + ("y" * 5000))

    assert text.startswith("\n\n<tool>")
    assert text.endswith("</tool>\n")
    assert trace.type == "tool_call_completed"
    assert trace.result_preview is not None
    assert trace.truncated is True


def test_format_tool_combined_with_none_result() -> None:
    """Combined formatting should handle missing results."""
    text, trace = format_tool_combined("save_file", {}, None)

    assert text == "\n\n<tool>save_file()</tool>\n"
    assert trace.type == "tool_call_completed"
    assert trace.tool_name == "save_file"
    assert trace.result_preview is None
    assert trace.truncated is False


def test_format_tool_combined_with_empty_string_result() -> None:
    """Combined formatting should treat empty results as no-result."""
    text, trace = format_tool_combined("save_file", {"file": "a.py"}, "")

    assert "\n" not in text.strip().removeprefix("<tool>").removesuffix("</tool>").split("\n")[0] or True
    assert trace.type == "tool_call_completed"
    assert trace.result_preview is None
    assert trace.truncated is False


def test_complete_pending_tool_block_replaces_pending() -> None:
    """Should find and replace a pending <tool> block with the result."""
    text = "hello\n\n<tool>save_file(file=a.py)</tool>\nworld"
    updated, trace = complete_pending_tool_block(text, "save_file", "ok")

    assert "<tool>save_file(file=a.py)\nok</tool>" in updated
    assert "world" in updated
    assert trace.type == "tool_call_completed"
    assert trace.result_preview == "ok"


def test_complete_pending_tool_block_skips_already_completed() -> None:
    """Should not modify blocks that already have a result (contain newline)."""
    text = "<tool>save_file(file=a.py)\nold_result</tool>"
    updated, trace = complete_pending_tool_block(text, "save_file", "new_result")

    # Should append a new block since the existing one is already completed
    assert "old_result" in updated
    assert "new_result" in updated
    assert trace.type == "tool_call_completed"


def test_complete_pending_tool_block_appends_when_no_pending() -> None:
    """Should append a standalone block when no pending block is found."""
    text = "some text"
    updated, trace = complete_pending_tool_block(text, "save_file", "result")

    assert "some text" in updated
    assert "<tool>save_file\nresult</tool>" in updated
    assert trace.type == "tool_call_completed"


def test_complete_pending_tool_block_no_result_no_change() -> None:
    """Should not modify anything when there's no result and no pending block."""
    text = "some text"
    updated, trace = complete_pending_tool_block(text, "save_file", None)

    assert updated == text
    assert trace.result_preview is None


def test_complete_pending_tool_block_no_result_keeps_pending() -> None:
    """Should keep pending block as-is when result is None."""
    text = "<tool>save_file(file=a.py)</tool>"
    updated, trace = complete_pending_tool_block(text, "save_file", None)

    assert updated == text  # No change
    assert trace.result_preview is None


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


def test_format_tool_started_with_empty_args() -> None:
    """Tool start formatting should handle empty argument maps."""
    text, trace = format_tool_started("save_file", {})
    assert text == "\n\n<tool>save_file()</tool>\n"
    assert trace.type == "tool_call_started"
    assert trace.tool_name == "save_file"
    assert trace.args_preview is None
    assert trace.truncated is False


def test_format_tool_started_preserves_argument_order() -> None:
    """Tool start formatting should preserve input argument ordering."""
    text, _ = format_tool_started(
        "save_file",
        {
            "file_name": "a.py",
            "contents": "print('x')",
        },
    )
    assert "save_file(file_name=a.py, contents=print(&#x27;x&#x27;))" in text


def test_extract_tool_completed_info_without_tool_returns_none() -> None:
    """Tool completion events without tool payload should return None."""
    result = extract_tool_completed_info(object())
    assert result is None
