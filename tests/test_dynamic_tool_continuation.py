"""Tests for dynamic-tool same-turn continuation decisions."""

from __future__ import annotations

import json

from agno.models.response import ToolExecution

from mindroom.dynamic_tool_continuation import (
    DYNAMIC_TOOL_CONTINUATION_LIMIT,
    continuation_decision_from_tools,
)


def _dynamic_tool_execution(status: str = "loaded") -> ToolExecution:
    return ToolExecution(
        tool_call_id="call-load",
        tool_name="load_tool",
        tool_args={"tool_name": "sleep"},
        result=json.dumps(
            {
                "status": status,
                "takes_effect": "later_tool_call_step",
                "tool": "dynamic_tools",
                "tool_name": "sleep",
            },
        ),
        stop_after_tool_call=True,
    )


def test_continuation_decision_detects_dynamic_tool_call() -> None:
    """A dynamic manager call produces a continuation prompt."""
    decision = continuation_decision_from_tools(
        [_dynamic_tool_execution()],
        original_prompt="Book the campsite",
        continuation_count=0,
    )

    assert decision.should_continue is True
    assert decision.limit_message is None
    assert decision.next_prompt is not None
    assert "Book the campsite" in decision.next_prompt
    assert "After this tool result is processed" in decision.next_prompt
    assert "updated tool schema" in decision.next_prompt
    assert "Continue the same task" in decision.next_prompt
    assert "loaded" in decision.next_prompt


def test_continuation_decision_handles_failed_dynamic_tool_call_without_assuming_availability() -> None:
    """A failed dynamic manager call should continue without implying the requested tool is usable."""
    decision = continuation_decision_from_tools(
        [_dynamic_tool_execution(status="unknown")],
        original_prompt="Book the campsite",
        continuation_count=0,
    )

    assert decision.should_continue is True
    assert decision.next_prompt is not None
    assert "current dynamic tool state" in decision.next_prompt
    assert "If the requested tool is unavailable" in decision.next_prompt
    assert "explain that or choose another available deferred tool" in decision.next_prompt
    assert "updated tool schema" not in decision.next_prompt


def test_continuation_decision_ignores_non_dynamic_tool_payload() -> None:
    """A load_tool-shaped call from another toolkit should not trigger continuation."""
    execution = ToolExecution(
        tool_call_id="call-load",
        tool_name="load_tool",
        tool_args={"tool_name": "sleep"},
        result=json.dumps({"status": "loaded", "tool": "other"}),
    )

    decision = continuation_decision_from_tools(
        [execution],
        original_prompt="Book the campsite",
        continuation_count=0,
    )

    assert decision.should_continue is False
    assert decision.next_prompt is None
    assert decision.limit_message is None


def test_continuation_decision_returns_limit_message_at_limit() -> None:
    """Repeated dynamic manager calls produce visible fallback text at the limit."""
    decision = continuation_decision_from_tools(
        [_dynamic_tool_execution(status="unknown")],
        original_prompt="Book the campsite",
        continuation_count=DYNAMIC_TOOL_CONTINUATION_LIMIT,
    )

    assert decision.should_continue is False
    assert decision.next_prompt is None
    assert decision.limit_message is not None
    assert "Dynamic tool calls did not produce a final answer" in decision.limit_message
    assert "`load_tool` for `sleep`" in decision.limit_message
    assert "`unknown`" in decision.limit_message
