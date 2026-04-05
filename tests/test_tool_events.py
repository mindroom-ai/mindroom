"""Tests for tool event formatting and metadata payloads."""

import json

import pytest
from agno.models.response import ToolExecution

from mindroom.matrix.message_builder import markdown_to_html
from mindroom.tool_system.events import (
    _MAX_TOOL_RESULT_DISPLAY_CHARS,
    _MAX_TOOL_TRACE_EVENTS,
    _TOOL_TRACE_KEY,
    ToolTraceEntry,
    _format_tool_started,
    build_tool_trace_content,
    complete_pending_tool_block,
    extract_tool_completed_info,
    format_tool_combined,
    format_tool_completed_event,
)

TEST_CURSOR = "cursor_1234567890"


def _room_threads_result(
    *,
    thread_count: int,
    body_len: int,
    has_more: bool = True,
    next_token: str | None = TEST_CURSOR,
) -> tuple[str, list[str]]:
    thread_ids = [f"$thread_{i}_{'X' * 40}:localhost" for i in range(thread_count)]
    payload = {
        "action": "room-threads",
        "count": thread_count,
        "has_more": has_more,
        "next_token": next_token,
        "status": "ok",
        "tool": "matrix_message",
        "threads": [
            {
                "body_preview": "body " + ("y" * body_len),
                "sender": "@user:localhost",
                "thread_id": thread_id,
            }
            for thread_id in thread_ids
        ],
    }
    return json.dumps(payload, sort_keys=True), thread_ids


def _read_result(*, message_count: int, body_len: int) -> str:
    payload = {
        "action": "read",
        "limit": 20,
        "messages": [
            {
                "body": "message " + ("y" * body_len),
                "event_id": f"$event_{index}:localhost",
                "sender": "@user:localhost",
            }
            for index in range(message_count)
        ],
        "room_id": "!room:localhost",
        "status": "ok",
        "tool": "matrix_message",
    }
    return json.dumps(payload, sort_keys=True)


def _exact_limit_room_threads_result() -> tuple[str, list[str]]:
    base_result, thread_ids = _room_threads_result(
        thread_count=1,
        body_len=0,
        has_more=False,
        next_token=None,
    )
    body_len = _MAX_TOOL_RESULT_DISPLAY_CHARS - len(base_result)
    assert body_len >= 0

    result, thread_ids = _room_threads_result(
        thread_count=1,
        body_len=body_len,
        has_more=False,
        next_token=None,
    )
    assert len(result) == _MAX_TOOL_RESULT_DISPLAY_CHARS
    return result, thread_ids


def test_format_tool_started_uses_plain_marker_and_truncates() -> None:
    """Tool start messages should render as compact plain-text markers."""
    long_contents = "x" * 2000
    text, trace = _format_tool_started(
        "save_file",
        {
            "file_name": "notes.txt",
            "contents": f"@mindroom_code:localhost {long_contents}",
        },
        tool_index=1,
    )

    assert text.startswith("\n\n🔧 `save_file` [1]")
    assert text.endswith("\n")
    assert "🔧" in text
    assert "`save_file`" in text
    assert "[1]" in text
    assert "⏳" in text
    assert "file_name=" not in text  # args must not be in inline marker
    assert "@mindroom_code:localhost" not in text  # mention-neutralized
    assert trace.type == "tool_call_started"
    assert trace.tool_name == "save_file"
    assert trace.args_preview is not None
    assert trace.truncated is True


def test_format_tool_combined_with_result() -> None:
    """Combined formatting should produce a completed plain marker and trace metadata."""
    text, trace = format_tool_combined("run_shell_command", {"cmd": "pwd"}, "/app", tool_index=2)

    assert text.startswith("\n\n🔧 `run_shell_command` [2]")
    assert text.endswith("\n")
    assert "<validation>" not in text
    assert "`run_shell_command`" in text
    assert "[2]" in text
    assert "⏳" not in text
    assert "cmd=pwd" not in text
    assert "/app" not in text
    assert trace.type == "tool_call_completed"
    assert trace.tool_name == "run_shell_command"
    assert trace.result_preview == "/app"
    assert trace.truncated is False


def test_format_tool_combined_truncates_long_result() -> None:
    """Combined formatting should truncate long results."""
    text, trace = format_tool_combined("run_shell_command", {}, "done " + ("y" * 5000))

    assert text.startswith("\n\n🔧 `run_shell_command`")
    assert text.endswith("\n")
    assert trace.type == "tool_call_completed"
    assert trace.result_preview is not None
    assert trace.truncated is True


def test_format_tool_combined_truncates_structured_room_threads_by_entry() -> None:
    """Structured room-thread previews should drop whole entries and preserve metadata."""
    result, thread_ids = _room_threads_result(thread_count=4, body_len=400)

    _text, trace = format_tool_combined("matrix_message", {"action": "room-threads"}, result)

    assert trace.result_preview is not None
    assert len(trace.result_preview) <= _MAX_TOOL_RESULT_DISPLAY_CHARS
    assert trace.truncated is True

    preview = json.loads(trace.result_preview)
    assert preview["count"] == 4
    assert preview["has_more"] is True
    assert preview["next_token"] == TEST_CURSOR
    assert preview["truncated"] is True
    assert preview["threads"]
    assert len(preview["threads"]) < 4
    assert all(item["thread_id"] in thread_ids for item in preview["threads"])
    assert all(not item["thread_id"].endswith("…") for item in preview["threads"])


def test_format_tool_combined_truncates_body_preview_without_dropping_only_entry() -> None:
    """Single-entry structured previews should shorten body_preview before dropping the entry."""
    result, thread_ids = _room_threads_result(
        thread_count=1,
        body_len=400,
        has_more=False,
        next_token=None,
    )

    _text, trace = format_tool_combined("matrix_message", {"action": "room-threads"}, result)

    assert trace.result_preview is not None
    assert len(trace.result_preview) <= _MAX_TOOL_RESULT_DISPLAY_CHARS
    assert trace.truncated is True

    preview = json.loads(trace.result_preview)
    assert "truncated" not in preview
    assert preview["count"] == 1
    assert preview["has_more"] is False
    assert preview["next_token"] is None
    assert len(preview["threads"]) == 1
    assert preview["threads"][0]["thread_id"] == thread_ids[0]
    assert preview["threads"][0]["body_preview"].endswith("…")


def test_format_tool_combined_falls_back_for_matrix_message_read_payload() -> None:
    """Read payloads keep the original char-based truncation path."""
    result = _read_result(message_count=3, body_len=300)

    _text, trace = format_tool_combined("matrix_message", {"action": "read"}, result)

    assert trace.result_preview is not None
    assert trace.result_preview == f"{result[: _MAX_TOOL_RESULT_DISPLAY_CHARS - 1]}…"
    assert trace.truncated is True
    assert '"messages": []' not in trace.result_preview
    assert '"truncated": true' not in trace.result_preview


def test_format_tool_combined_falls_back_for_empty_threads_list_over_limit() -> None:
    """Empty thread lists should use plain truncation instead of smart structured truncation."""
    result = json.dumps(
        {
            "action": "room-threads",
            "count": 0,
            "has_more": True,
            "next_token": "N" * 600,
            "status": "ok",
            "threads": [],
            "tool": "matrix_message",
        },
        sort_keys=True,
    )

    _text, trace = format_tool_combined("matrix_message", {"action": "room-threads"}, result)

    assert trace.result_preview is not None
    assert trace.result_preview == f"{result[: _MAX_TOOL_RESULT_DISPLAY_CHARS - 1]}…"
    assert trace.truncated is True
    assert '"truncated": true' not in trace.result_preview


def test_format_tool_combined_preserves_exact_limit_structured_result() -> None:
    """Exact-at-limit thread payloads should not be marked truncated."""
    result, thread_ids = _exact_limit_room_threads_result()

    _text, trace = format_tool_combined("matrix_message", {"action": "room-threads"}, result)

    assert trace.result_preview == result
    assert trace.truncated is False
    preview = json.loads(trace.result_preview)
    assert preview["threads"][0]["thread_id"] == thread_ids[0]
    assert not preview["threads"][0]["body_preview"].endswith("…")


def test_format_tool_combined_with_none_result() -> None:
    """Combined formatting should handle missing results."""
    text, trace = format_tool_combined("save_file", {}, None, tool_index=1)

    assert text == "\n\n🔧 `save_file` [1]\n"
    assert trace.type == "tool_call_completed"
    assert trace.tool_name == "save_file"
    assert trace.result_preview is None
    assert trace.truncated is False


def test_format_tool_combined_with_empty_string_result() -> None:
    """Combined formatting should treat empty results as no-result."""
    text, trace = format_tool_combined("save_file", {"file": "a.py"}, "", tool_index=1)

    assert text == "\n\n🔧 `save_file` [1]\n"
    assert trace.type == "tool_call_completed"
    assert trace.result_preview is None
    assert trace.truncated is False


def test_complete_pending_tool_block_replaces_pending() -> None:
    """Should find a pending marker by id and mark it completed."""
    text = "hello\n\n🔧 `save_file` [1] ⏳\nworld"
    updated, trace = complete_pending_tool_block(text, "save_file", "ok", tool_index=1)

    assert "🔧 `save_file` [1]\n" in updated
    assert "⏳" not in updated
    assert "\nok\n" not in updated  # results are no longer injected inline
    assert "world" in updated
    assert trace.type == "tool_call_completed"
    assert trace.result_preview == "ok"


def test_complete_pending_tool_block_skips_already_completed() -> None:
    """Should leave an already-completed marker unchanged."""
    text = "🔧 `save_file` [1]"
    updated, trace = complete_pending_tool_block(text, "save_file", "new_result", tool_index=1)

    assert updated == text
    assert trace.type == "tool_call_completed"
    assert trace.result_preview == "new_result"


def test_complete_pending_tool_block_noops_when_no_pending() -> None:
    """Should not synthesize a completed marker when no pending marker is found."""
    text = "some text"
    updated, trace = complete_pending_tool_block(text, "save_file", "result", tool_index=3)

    assert updated == text
    assert trace.type == "tool_call_completed"


def test_complete_pending_tool_block_requires_tool_index() -> None:
    """V2 completion markers must be matched by index."""
    text = "some text"
    with pytest.raises(ValueError, match="tool_index"):
        complete_pending_tool_block(text, "save_file", None, tool_index=0)


def test_complete_pending_tool_block_no_result_no_change() -> None:
    """Should not modify anything when there's no result and no pending block."""
    text = "some text"
    updated, trace = complete_pending_tool_block(text, "save_file", None, tool_index=1)

    assert updated == text
    assert trace.result_preview is None


def test_complete_pending_tool_block_no_result_marks_completed() -> None:
    """Should mark pending block as completed even when result is None."""
    text = "🔧 `save_file` [1] ⏳"
    updated, trace = complete_pending_tool_block(text, "save_file", None, tool_index=1)

    assert updated == "🔧 `save_file` [1]"
    assert trace.result_preview is None


def test_build_tool_trace_content_preserves_all_events_for_v2_indexing() -> None:
    """V2 tool trace keeps all events so `[N] -> events[N-1]` remains valid."""
    entries = [
        ToolTraceEntry(type="tool_call_started", tool_name=f"tool_{i}") for i in range(_MAX_TOOL_TRACE_EVENTS + 5)
    ]
    payload = build_tool_trace_content(entries)
    assert payload is not None
    trace = payload[_TOOL_TRACE_KEY]
    assert trace["version"] == 2
    assert len(trace["events"]) == _MAX_TOOL_TRACE_EVENTS + 5
    assert "events_truncated" not in trace


def test_format_tool_started_with_empty_args() -> None:
    """Tool start formatting should handle empty argument maps."""
    text, trace = _format_tool_started("save_file", {}, tool_index=1)
    assert text == "\n\n🔧 `save_file` [1] ⏳\n"
    assert trace.type == "tool_call_started"
    assert trace.tool_name == "save_file"
    assert trace.args_preview is None
    assert trace.truncated is False


def test_format_tool_started_preserves_argument_order() -> None:
    """Tool start formatting should preserve input argument ordering."""
    _text, trace = _format_tool_started(
        "save_file",
        {
            "file_name": "a.py",
            "contents": "print('x')",
        },
    )
    assert trace.args_preview == "file_name=a.py, contents=print('x')"


def test_complete_pending_tool_block_roundtrip_with_marker_id() -> None:
    """Pending marker produced by format_tool_started should be completed in-place by id."""
    pending_text, _ = _format_tool_started(
        "save_file",
        {"file_name": "a.py", "contents": "print('hello')"},
        tool_index=5,
    )

    updated, trace = complete_pending_tool_block(pending_text, "save_file", "ok", tool_index=5)

    assert "`save_file`" in updated
    assert "[5]" in updated
    assert "⏳" not in updated
    assert updated.count("🔧") == 1
    assert trace.result_preview == "ok"


def test_format_tool_started_collapses_newlines_in_args() -> None:
    """Tool args with newlines should be collapsed to spaces."""
    text, trace = _format_tool_started(
        "save_file",
        {"contents": "line1\nline2\nline3"},
    )

    assert "line1 line2 line3" not in text  # inline markers never include args
    assert trace.args_preview is not None
    assert "line1 line2 line3" in trace.args_preview
    assert "\n" not in trace.args_preview


def test_complete_pending_tool_block_roundtrip_with_multiline_args() -> None:
    """format_tool_started with multiline args -> complete_pending_tool_block should succeed."""
    pending_text, _ = _format_tool_started(
        "save_file",
        {"file": "test.py", "contents": "def foo():\n    return 42\n"},
        tool_index=1,
    )

    # The marker line should remain single-line.
    marker_line = next(line for line in pending_text.splitlines() if line.strip())
    assert "\n" not in marker_line

    # Completing should work and produce exactly one block
    updated, trace = complete_pending_tool_block(pending_text, "save_file", "saved", tool_index=1)

    assert "⏳" not in updated
    assert updated.count("🔧") == 1
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


def test_format_tool_completed_event_without_tool_returns_empty() -> None:
    """None tool should return empty text and no trace."""
    text, trace = format_tool_completed_event(None)
    assert text == ""
    assert trace is None


def test_format_tool_completed_event_formats_combined_block() -> None:
    """Completion event helper should render canonical plain marker."""
    tool = ToolExecution(tool_name="run_shell", tool_args={"cmd": "pwd"}, result="/app")
    text, trace = format_tool_completed_event(tool, tool_index=1)
    assert text == "\n\n🔧 `run_shell` [1]\n"
    assert trace is not None
    assert trace.type == "tool_call_completed"
    assert trace.tool_name == "run_shell"
    assert trace.args_preview == "cmd=pwd"
    assert trace.result_preview == "/app"


# --- markdown_to_html: v2 plain markers + unsupported tag escaping ---


def test_markdown_to_html_escapes_tool_tags() -> None:
    """Legacy <tool> tags should be escaped (no backward compatibility)."""
    html = markdown_to_html("<tool>save_file(file=a.py)\nok</tool>")
    assert "<tool>" not in html
    assert "</tool>" not in html
    assert "&lt;tool&gt;" in html
    assert "&lt;/tool&gt;" in html
    assert "save_file(file=a.py)" in html


def test_markdown_to_html_escapes_unknown_tags_including_tool() -> None:
    """Unknown raw tags are escaped while supported tags stay intact."""
    body = (
        "<tool>save_file(file=a.py)\nok</tool>\n<code>example</code>\n<search>\n<query>Mindroom docs</query>\n</search>"
    )
    html = markdown_to_html(body)
    assert "<tool>" not in html
    assert "&lt;tool&gt;" in html
    assert "&lt;/tool&gt;" in html
    assert "<code>example</code>" in html
    assert "<search>" not in html
    assert "<query>" not in html
    assert "&lt;search&gt;" in html
    assert "&lt;query&gt;" in html
    assert "&lt;/query&gt;" in html
    assert "&lt;/search&gt;" in html
    assert "Mindroom docs" in html


# --- Contract test: v2 marker pipeline (plain text -> markdown HTML) ---


def test_tool_lifecycle_produces_expected_html() -> None:
    """Full pipeline: started -> completed -> markdown_to_html emits plain marker text with code spans."""
    # 1. Two tool calls start (pending)
    text1, _ = _format_tool_started("save_file", {"file": "a.py"}, tool_index=1)
    text2, _ = _format_tool_started("run_shell", {"cmd": "pwd"}, tool_index=2)
    body = text1 + text2

    # 2. Both complete
    body, _ = complete_pending_tool_block(body, "save_file", "ok", tool_index=1)
    body, _ = complete_pending_tool_block(body, "run_shell", "/app", tool_index=2)

    # 3. Convert to HTML
    html = markdown_to_html(body)

    assert "<code>save_file</code>" in html
    assert "<code>run_shell</code>" in html
    assert "[1]" in html
    assert "[2]" in html
    assert "🔧" in html
    assert "⏳" not in html
    assert "<tool>" not in html


def test_markdown_to_html_plain_tool_marker_renders_code_span() -> None:
    """V2 plain-text markers should render as normal markdown with a code span."""
    html = markdown_to_html("\n\n🔧 `search_web` [1] ⏳\n")
    assert "<code>search_web</code>" in html
    assert "🔧" in html
    assert "⏳" in html
