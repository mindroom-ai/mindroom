"""Tests for dynamic-tool same-turn continuation decisions."""

from __future__ import annotations

import json

from agno.models.response import ToolExecution

from mindroom.dynamic_tool_continuation import (
    DYNAMIC_TOOL_CONTINUATION_LIMIT,
    continuation_decision_from_tools,
)


def _dynamic_tool_execution(
    status: str = "loaded",
    *,
    function_name: str = "load_tool",
    tool_name: str = "sleep",
) -> ToolExecution:
    return ToolExecution(
        tool_call_id=f"call-{function_name}",
        tool_name=function_name,
        tool_args={"tool_name": tool_name},
        result=json.dumps(
            {
                "status": status,
                "tool": "dynamic_tools",
                "tool_name": tool_name,
            },
        ),
        stop_after_tool_call=True,
    )


def _model_switch_execution(when: str, *, model_name: str = "large") -> ToolExecution:
    return ToolExecution(
        tool_call_id="call-switch-model",
        tool_name="switch_thread_model",
        tool_args={"model_name": model_name, "when": when},
        result=json.dumps(
            {
                "action": "switch",
                "model": model_name,
                "status": "ok",
                "tool": "thread_model",
                "when": when,
            },
        ),
        stop_after_tool_call=True,
    )


def test_continuation_decision_switches_model_after_tool_call() -> None:
    """Ignoring an immediate model-switch result would end the turn before the new model can continue."""
    decision = continuation_decision_from_tools(
        [_model_switch_execution("after-toolcall")],
        original_prompt="Solve the problem",
        continuation_count=0,
    )

    assert decision.should_continue is True
    assert decision.model_switch_name == "large"
    assert decision.model_switch_when == "after-toolcall"
    assert decision.next_prompt is not None
    assert "Solve the problem" in decision.next_prompt
    assert "Continue the same task with `large`" in decision.next_prompt


def test_continuation_decision_defers_model_switch_until_next_turn() -> None:
    """A next-turn switch still needs the original model to finish the stopped provider run."""
    decision = continuation_decision_from_tools(
        [_model_switch_execution("next-turn")],
        original_prompt="Solve the problem",
        continuation_count=0,
    )

    assert decision.should_continue is True
    assert decision.model_switch_name == "large"
    assert decision.model_switch_when == "next-turn"
    assert decision.next_prompt is not None
    assert "Continue the same task with the model that started this response" in decision.next_prompt


def test_failed_model_switch_resumes_with_current_model() -> None:
    """A rejected switch must not strand the response at its stop-after-tool boundary."""
    execution = ToolExecution(
        tool_call_id="call-switch-model",
        tool_name="switch_thread_model",
        tool_args={"model_name": "missing", "when": "after-toolcall"},
        result=json.dumps(
            {
                "action": "switch",
                "message": "Unknown model 'missing'.",
                "status": "error",
                "tool": "thread_model",
            },
        ),
        stop_after_tool_call=True,
    )

    decision = continuation_decision_from_tools(
        [execution],
        original_prompt="Solve the problem",
        continuation_count=0,
    )

    assert decision.should_continue is True
    assert decision.model_switch_name is None
    assert decision.model_switch_when is None
    assert decision.next_prompt is not None
    assert "Continue the same task with the current model" in decision.next_prompt


def test_malformed_model_switch_result_resumes_with_current_model() -> None:
    """A malformed switch result must not strand the response at its stop-after-tool boundary."""
    execution = ToolExecution(
        tool_call_id="call-switch-model",
        tool_name="switch_thread_model",
        tool_args={"model_name": "large", "when": "after-toolcall"},
        result="not-json",
        stop_after_tool_call=True,
    )

    decision = continuation_decision_from_tools(
        [execution],
        original_prompt="Solve the problem",
        continuation_count=0,
    )

    assert decision.should_continue is True
    assert decision.model_switch_name is None
    assert decision.model_switch_when is None


def test_latest_successful_model_switch_wins_over_later_rejection() -> None:
    """A rejected later call cannot undo an earlier switch that was already persisted."""
    rejected = ToolExecution(
        tool_call_id="call-rejected-switch",
        tool_name="switch_thread_model",
        tool_args={"model_name": "missing", "when": "after-toolcall"},
        result=json.dumps(
            {
                "action": "switch",
                "message": "Unknown model 'missing'.",
                "status": "error",
                "tool": "thread_model",
            },
        ),
        stop_after_tool_call=True,
    )

    decision = continuation_decision_from_tools(
        [_model_switch_execution("after-toolcall"), rejected],
        original_prompt="Solve the problem",
        continuation_count=0,
    )

    assert decision.model_switch_name == "large"
    assert decision.model_switch_when == "after-toolcall"


def test_latest_successful_model_switch_wins_among_multiple_valid_calls() -> None:
    """Provider call order determines the active alias when several switches succeed."""
    decision = continuation_decision_from_tools(
        [
            _model_switch_execution("after-toolcall"),
            _model_switch_execution("after-toolcall", model_name="default"),
        ],
        original_prompt="Solve the problem",
        continuation_count=0,
    )

    assert decision.model_switch_name == "default"
    assert decision.model_switch_when == "after-toolcall"


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


def test_continuation_decision_detects_unload_tool_call() -> None:
    """An unload_tool manager call continues the same task without the tool."""
    decision = continuation_decision_from_tools(
        [_dynamic_tool_execution(status="unloaded", function_name="unload_tool")],
        original_prompt="Tidy up the tools",
        continuation_count=0,
    )

    assert decision.should_continue is True
    assert decision.next_prompt is not None
    assert "unload_tool" in decision.next_prompt
    assert "without the unloaded tool" in decision.next_prompt
    assert "If the requested tool is unavailable" not in decision.next_prompt


def test_continuation_decision_limit_message_names_unload_tool() -> None:
    """The limit fallback names the unload_tool call that did not converge."""
    decision = continuation_decision_from_tools(
        [_dynamic_tool_execution(status="unloaded", function_name="unload_tool")],
        original_prompt="Tidy up the tools",
        continuation_count=DYNAMIC_TOOL_CONTINUATION_LIMIT,
    )

    assert decision.should_continue is False
    assert decision.limit_message is not None
    assert "`unload_tool` for `sleep`" in decision.limit_message


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
