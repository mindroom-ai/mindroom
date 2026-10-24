"""Tests for same-turn continuation decisions after a thread-model switch."""

from __future__ import annotations

import json

from mindroom.llm.runs import ToolExecution
from mindroom.turn_continuation import (
    TURN_CONTINUATION_LIMIT,
    continuation_decision_from_tools,
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
        stop_after_call=True,
    )


def test_continuation_decision_switches_model_after_tool_call() -> None:
    """Ignoring an immediate model-switch result would end the turn before the new model can continue."""
    decision = continuation_decision_from_tools(
        [_model_switch_execution("after-toolcall").call],
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
        [_model_switch_execution("next-turn").call],
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
        stop_after_call=True,
    )

    decision = continuation_decision_from_tools(
        [execution.call],
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
        stop_after_call=True,
    )

    decision = continuation_decision_from_tools(
        [execution.call],
        original_prompt="Solve the problem",
        continuation_count=0,
    )

    assert decision.should_continue is True
    assert decision.model_switch_name is None
    assert decision.model_switch_when is None
    assert decision.next_prompt is not None
    assert "Continue the same task with the current model" in decision.next_prompt


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
        stop_after_call=True,
    )

    decision = continuation_decision_from_tools(
        [_model_switch_execution("after-toolcall").call, rejected.call],
        original_prompt="Solve the problem",
        continuation_count=0,
    )

    assert decision.model_switch_name == "large"
    assert decision.model_switch_when == "after-toolcall"


def test_latest_successful_model_switch_wins_among_multiple_valid_calls() -> None:
    """Provider call order determines the active alias when several switches succeed."""
    decision = continuation_decision_from_tools(
        [
            _model_switch_execution("after-toolcall").call,
            _model_switch_execution("after-toolcall", model_name="default").call,
        ],
        original_prompt="Solve the problem",
        continuation_count=0,
    )

    assert decision.model_switch_name == "default"
    assert decision.model_switch_when == "after-toolcall"


def test_tool_search_and_call_tool_results_never_continue() -> None:
    """Deferred-tool discovery runs inside the provider loop, so its results never restart the turn."""
    search = ToolExecution(
        tool_call_id="call-search",
        tool_name="tool_search",
        tool_args={"query": "sleep"},
        result=json.dumps({"status": "ok", "tool": "dynamic_tools", "matches": []}),
    )

    decision = continuation_decision_from_tools([search.call], original_prompt="Nap", continuation_count=0)

    assert decision.should_continue is False
    assert decision.limit_message is None


def test_model_switch_at_the_limit_returns_limit_message() -> None:
    """A model switch past the continuation limit answers with visible fallback text instead of continuing."""
    decision = continuation_decision_from_tools(
        [_model_switch_execution("after-toolcall").call],
        original_prompt="Book the campsite",
        continuation_count=TURN_CONTINUATION_LIMIT,
    )

    assert decision.should_continue is False
    assert decision.limit_message is not None
    assert decision.model_switch_name == "large"
