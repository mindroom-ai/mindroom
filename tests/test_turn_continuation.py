"""Tests for same-turn continuation decisions after a thread-model switch."""

from __future__ import annotations

import json

import pytest

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


def _rejected_execution() -> ToolExecution:
    return ToolExecution(
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
    )


def test_continuation_decision_switches_model_after_tool_call() -> None:
    """Ignoring an immediate model-switch result would end the turn before the new model can continue."""
    decision = continuation_decision_from_tools(
        [_model_switch_execution("after-toolcall").call],
        original_prompt="Solve the problem",
        continuation_count=0,
    )

    assert decision.continuation is not None
    assert decision.continuation.model_name == "large"
    assert decision.continuation.prompt == (
        "Solve the problem\n\n"
        "[SYSTEM NOTICE - MODEL SWITCH TOOL CALL COMPLETED]\n"
        "The previous model step switched this thread to `large` with timing `after-toolcall`. "
        "Continue the same task with `large` now. Do not repeat the model switch."
    )


@pytest.mark.parametrize(
    "result",
    [
        _model_switch_execution("next-turn").result,
        _rejected_execution().result,
        json.dumps(
            {"action": "switch", "model": "large", "status": "error", "tool": "thread_model", "when": "after-toolcall"},
        ),
        "not-json",
    ],
    ids=["next-turn", "rejected", "not-ok-naming-a-model", "malformed"],
)
def test_only_a_successful_switch_for_the_current_response_continues_the_turn(result: str) -> None:
    """A switch that takes effect later, or did not take effect, was answered in its run, so nothing continues."""
    execution = _model_switch_execution("after-toolcall")
    execution.result = result

    decision = continuation_decision_from_tools([execution.call], original_prompt="Solve", continuation_count=0)

    assert decision.continuation is None
    assert decision.limit_message is None


def test_latest_successful_model_switch_wins_over_later_rejection() -> None:
    """A rejected later call cannot undo an earlier switch that was already persisted."""
    decision = continuation_decision_from_tools(
        [_model_switch_execution("after-toolcall").call, _rejected_execution().call],
        original_prompt="Solve the problem",
        continuation_count=0,
    )

    assert decision.continuation is not None
    assert decision.continuation.model_name == "large"


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

    assert decision.continuation is not None
    assert decision.continuation.model_name == "default"


def test_tool_search_and_call_tool_results_never_continue() -> None:
    """Deferred-tool discovery runs inside the provider loop, so its results never restart the turn."""
    search = ToolExecution(
        tool_call_id="call-search",
        tool_name="tool_search",
        tool_args={"query": "sleep"},
        result=json.dumps({"status": "ok", "tool": "dynamic_tools", "matches": []}),
    )

    decision = continuation_decision_from_tools([search.call], original_prompt="Nap", continuation_count=0)

    assert decision.continuation is None
    assert decision.limit_message is None


def test_model_switch_at_the_limit_returns_limit_message() -> None:
    """A model switch past the continuation limit answers with visible fallback text instead of continuing."""
    decision = continuation_decision_from_tools(
        [_model_switch_execution("after-toolcall").call],
        original_prompt="Book the campsite",
        continuation_count=TURN_CONTINUATION_LIMIT,
    )

    assert decision.continuation is None
    assert decision.limit_message == (
        f"Model switching did not produce a final answer after {TURN_CONTINUATION_LIMIT} continuation attempts. "
        "The last request selected `large` with timing `after-toolcall`."
    )
