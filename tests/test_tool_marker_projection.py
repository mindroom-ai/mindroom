"""Tool display markers stay in Matrix bodies: models get plain text, judges get tags."""

from __future__ import annotations

from mindroom.tool_system.events import (
    ToolTraceEntry,
    body_with_tool_tags,
    body_without_tool_markers,
    build_tool_trace_content,
    message_tool_trace,
    render_tool_tag,
)

_TRACE = [
    ToolTraceEntry(
        type="tool_call_completed",
        tool_name="shell",
        args_preview="command=pytest -x",
        result_preview='3 "failed"',
    ),
    ToolTraceEntry(type="tool_call_started", tool_name="file", args_preview="path=src/auth.py", truncated=True),
]
_BODY = "Running tests.\n\n🔧 `shell` [1]\n\nFixing.\n\n🔧 `file` [2] ⏳"


def test_markers_are_stripped_only_from_traced_messages() -> None:
    """Without the tool trace there is no proof the lines are MindRoom chrome, so human text stays."""
    content = build_tool_trace_content(_TRACE) or {}
    assert "🔧" not in body_without_tool_markers(_BODY, content)
    assert "Fixing." in body_without_tool_markers(_BODY, content)
    assert body_without_tool_markers(_BODY, {"msgtype": "m.text"}) == _BODY


def test_message_tool_trace_reads_the_stored_entries() -> None:
    """The stored trace round-trips into entries, and absent metadata reads as None."""
    content = build_tool_trace_content(_TRACE) or {}
    assert message_tool_trace(content) == _TRACE
    assert message_tool_trace({"msgtype": "m.text"}) is None


def test_render_tool_tag_quotes_previews() -> None:
    """Previews are tool output, so they are attribute-quoted data, never markup."""
    assert render_tool_tag(_TRACE[0]) == '<tool name="shell" input="command=pytest -x" output=\'3 "failed"\'/>'
    assert render_tool_tag(_TRACE[1]) == '<tool name="file" input="path=src/auth.py" truncated="true"/>'


def test_body_with_tool_tags_replaces_markers_in_place() -> None:
    """Each marker becomes its own trace entry's tag, and a marker without an entry disappears."""
    assert body_with_tool_tags(_BODY, _TRACE) == (
        "Running tests.\n\n"
        '<tool name="shell" input="command=pytest -x" output=\'3 "failed"\'/>\n\n'
        "Fixing.\n\n"
        '<tool name="file" input="path=src/auth.py" truncated="true"/>'
    )
    assert body_with_tool_tags(_BODY, _TRACE[:1]).endswith("Fixing.\n")
