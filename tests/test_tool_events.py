"""Tests for tool event formatting and metadata payloads."""

from agno.models.response import ToolExecution

from mindroom.matrix.client import markdown_to_html
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

    # Trailing \n inside the block signals "completed" even without a result
    assert text == "\n\n<tool>save_file()\n</tool>\n"
    assert trace.type == "tool_call_completed"
    assert trace.tool_name == "save_file"
    assert trace.result_preview is None
    assert trace.truncated is False


def test_format_tool_combined_with_empty_string_result() -> None:
    """Combined formatting should treat empty results as no-result."""
    text, trace = format_tool_combined("save_file", {"file": "a.py"}, "")

    # Trailing \n inside the block signals "completed" even with empty result
    assert text == "\n\n<tool>save_file(file=a.py)\n</tool>\n"
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


def test_complete_pending_tool_block_no_result_marks_completed() -> None:
    """Should mark pending block as completed even when result is None."""
    text = "<tool>save_file(file=a.py)</tool>"
    updated, trace = complete_pending_tool_block(text, "save_file", None)

    # Newline injected to mark as completed, even without a result
    assert updated == "<tool>save_file(file=a.py)\n</tool>"
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


def test_complete_pending_tool_block_with_escaped_content() -> None:
    """Should match pending blocks produced by format_tool_started (HTML-escaped + mention-neutralized)."""
    # format_tool_started produces HTML-escaped, mention-neutralized content
    pending_text, _ = format_tool_started("save_file", {"file_name": "a.py", "contents": "print('hello')"})

    # complete_pending_tool_block should find and replace this pending block
    updated, trace = complete_pending_tool_block(pending_text, "save_file", "ok")

    assert "ok</tool>" in updated
    # The original call should still be present (not duplicated)
    assert updated.count("<tool>") == 1
    assert updated.count("</tool>") == 1
    assert trace.result_preview == "ok"


def test_format_tool_started_collapses_newlines_in_args() -> None:
    """Tool args with newlines should be collapsed to spaces."""
    text, trace = format_tool_started(
        "save_file",
        {"contents": "line1\nline2\nline3"},
    )

    # Newlines in args would break pending/completed detection
    assert "\n" not in text.split("<tool>")[1].split("</tool>")[0]
    assert "line1 line2 line3" in text
    assert trace.args_preview is not None
    assert "\n" not in trace.args_preview


def test_complete_pending_tool_block_roundtrip_with_multiline_args() -> None:
    """format_tool_started with multiline args -> complete_pending_tool_block should succeed."""
    pending_text, _ = format_tool_started(
        "save_file",
        {"file": "test.py", "contents": "def foo():\n    return 42\n"},
    )

    # The pending block should have no newline inside (args were collapsed)
    inner = pending_text.split("<tool>")[1].split("</tool>")[0]
    assert "\n" not in inner

    # Completing should work and produce exactly one block
    updated, trace = complete_pending_tool_block(pending_text, "save_file", "saved")

    assert "saved</tool>" in updated
    assert updated.count("<tool>") == 1
    assert updated.count("</tool>") == 1
    assert trace.result_preview == "saved"


def test_extract_tool_completed_info_without_tool_returns_none() -> None:
    """None tool should return None."""
    assert extract_tool_completed_info(None) is None


def test_extract_tool_completed_info_uses_tool_result() -> None:
    """Should return tool.result (actual output)."""
    tool = ToolExecution(tool_name="check", result="actual output")
    info = extract_tool_completed_info(tool)
    assert info is not None
    tool_name, result = info
    assert tool_name == "check"
    assert result == "actual output"


# --- markdown_to_html: tool block handling ---


def test_markdown_to_html_tool_not_wrapped_in_p() -> None:
    """<tool> should be block-level, not wrapped in <p> tags."""
    html = markdown_to_html("<tool>save_file(file=a.py)\nok</tool>")
    assert "<p>" not in html
    assert "<br" not in html
    assert html == "<tool>save_file(file=a.py)\nok</tool>"


def test_markdown_to_html_groups_consecutive_tools() -> None:
    """Consecutive <tool> blocks should be wrapped in <tool-group>."""
    body = "\n\n<tool>save_file(file=a.py)\nok</tool>\n\n<tool>run_shell(cmd=pwd)\n/app</tool>\n"
    html = markdown_to_html(body)
    assert "<tool-group>" in html
    assert html.count("<tool>") == 2
    assert "<p>" not in html


def test_markdown_to_html_does_not_group_separated_tools() -> None:
    """Tool blocks separated by non-tool content should not be grouped."""
    body = "<tool>a\nb</tool>\n\nSome text\n\n<tool>c\nd</tool>"
    html = markdown_to_html(body)
    assert "<tool-group>" not in html


def test_markdown_to_html_escapes_unknown_tool_like_tags() -> None:
    """Unknown raw tags should be escaped so they remain visible on strict clients."""
    body = "<search>\n<query>Mindroom docs</query>\n</search>"
    html = markdown_to_html(body)
    assert "<search>" not in html
    assert "<query>" not in html
    assert "&lt;search&gt;" in html
    assert "&lt;query&gt;" in html
    assert "&lt;/query&gt;" in html
    assert "&lt;/search&gt;" in html
    assert "Mindroom docs" in html


# --- Contract test: full pending→completed→HTML pipeline ---


def test_tool_lifecycle_produces_expected_html() -> None:
    """Full pipeline: started → completed → markdown_to_html must produce the exact HTML the frontend expects.

    This is a contract test — if the output format changes, the corresponding
    frontend test (collapsible-test.tsx "renders backend contract HTML") must
    be updated in sync.
    """
    # 1. Two tool calls start (pending)
    text1, _ = format_tool_started("save_file", {"file": "a.py"})
    text2, _ = format_tool_started("run_shell", {"cmd": "pwd"})
    body = text1 + text2

    # 2. Both complete
    body, _ = complete_pending_tool_block(body, "save_file", "ok")
    body, _ = complete_pending_tool_block(body, "run_shell", "/app")

    # 3. Convert to HTML
    html = markdown_to_html(body)

    # Contract assertions — the frontend relies on this exact structure:
    # - No <p> wrapping, no <br> conversion
    assert "<p>" not in html
    assert "<br" not in html
    # - Consecutive completed blocks grouped in <tool-group>
    assert "<tool-group>" in html
    assert html.count("<tool>") == 2
    # - Call and result separated by \n inside each <tool> block
    assert "save_file(file=a.py)\nok</tool>" in html
    assert "run_shell(cmd=pwd)\n/app</tool>" in html
