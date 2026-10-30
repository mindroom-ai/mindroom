"""Tests for the shared custom-tool payload helpers."""

from __future__ import annotations

from mindroom.custom_tools.matrix_room import MatrixRoomTools
from mindroom.custom_tools.thread_summary import ThreadSummaryTools
from mindroom.custom_tools.thread_tags import ThreadTagsTools
from mindroom.custom_tools.tool_payloads import tool_context_or_error


def test_tool_context_or_error_returns_the_unavailable_payload_without_a_context() -> None:
    """Outside a tool call the helper returns the exact error payload text the tools send to the model."""
    assert tool_context_or_error(MatrixRoomTools._payload, "Matrix room") == (
        '{"message": "Matrix room tool context is unavailable in this runtime path.", '
        '"status": "error", "tool": "matrix_room"}'
    )
    assert tool_context_or_error(ThreadSummaryTools._payload, "Thread summary", action="set") == (
        '{"action": "set", "message": "Thread summary tool context is unavailable in this runtime path.", '
        '"status": "error", "tool": "thread_summary"}'
    )
    assert tool_context_or_error(ThreadTagsTools._payload, "Thread tags") == (
        '{"status": "error", "tool": "thread_tags", '
        '"message": "Thread tags tool context is unavailable in this runtime path."}'
    )
