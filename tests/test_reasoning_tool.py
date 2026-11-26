"""Native reasoning scratchpad toolkit behavior and function-scoped instructions."""

from __future__ import annotations

import json
from typing import Any

import pytest

from mindroom.custom_tools.reasoning import ReasoningTools
from mindroom.tool_system.agno_toolkits import FunctionScopedInstructions, adapt_toolkit, with_offered_instructions
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.invocation import ToolCallContext, execute_tool_call
from mindroom.tools.reasoning import reasoning_tools


def test_reasoning_toolkit_is_native_and_respects_enable_switches() -> None:
    """The reasoning factory returns a native Toolkit honoring think/analyze switches and all."""
    tool_class = reasoning_tools()

    assert issubclass(tool_class, Toolkit)
    assert tool_class().name == "reasoning_tools"
    assert list(tool_class().functions) == ["think", "analyze"]
    assert list(tool_class(enable_think=False).functions) == ["analyze"]
    assert list(tool_class(enable_analyze=False).functions) == ["think"]
    assert list(tool_class(enable_think=False, enable_analyze=False).functions) == []
    assert list(tool_class(enable_think=False, enable_analyze=False, all=True).functions) == ["think", "analyze"]


def test_reasoning_instructions_scope_to_offered_functions_and_few_shot_options() -> None:
    """Default instructions require both think and analyze; custom instructions show for any offered function."""
    full = ReasoningTools()
    assert isinstance(full, FunctionScopedInstructions)
    assert full.instructions.startswith("<reasoning_instructions>\n")
    assert full.instructions.endswith("\n</reasoning_instructions>\n")
    assert full.instructions_for(["think", "analyze"]) == full.instructions
    assert full.instructions_for(["think"]) == ""
    assert full.instructions_for(["analyze"]) == ""
    assert full.instructions_for([]) == ""

    narrowed = ReasoningTools(enable_think=False)
    assert narrowed.instructions == ""
    assert (
        with_offered_instructions(narrowed, adapt_toolkit(narrowed), authored_instructions=False).instructions is None
    )

    with_default_examples = ReasoningTools(add_few_shot=True)
    assert ReasoningTools.FEW_SHOT_EXAMPLES in with_default_examples.instructions
    with_custom_examples = ReasoningTools(add_few_shot=True, few_shot_examples="Example: think then analyze.")
    assert "Example: think then analyze." in with_custom_examples.instructions

    custom = ReasoningTools(enable_think=False, instructions="Use analyze carefully.")
    assert custom.instructions == "Use analyze carefully."
    assert custom.instructions_for(["analyze"]) == "Use analyze carefully."
    assert custom.instructions_for([]) == ""
    adapted = with_offered_instructions(custom, adapt_toolkit(custom), authored_instructions=True)
    assert adapted.instructions == adapted.guidance == "Use analyze carefully."


@pytest.mark.asyncio
async def test_think_and_analyze_persist_steps_per_run_in_session_state() -> None:
    """Steps accumulate in session_state under each run_id without leaking across runs."""
    toolkit = ReasoningTools()
    functions = {fn.name: fn for fn in adapt_toolkit(toolkit).functions}
    session_state: dict[str, Any] = {}

    run_one = ToolCallContext(run_id="run-1", session_id="sess-1", user_id=None, session_state=session_state)
    first = await execute_tool_call(
        functions["think"],
        {"title": "Plan", "thought": "Break down the query", "action": "Search", "confidence": 0.9},
        run_one,
        middleware=(),
    )
    second = await execute_tool_call(
        functions["analyze"],
        {
            "title": "Check",
            "result": "Found 2 facts",
            "analysis": "Enough to answer",
            "next_action": "finalize",
            "confidence": 1.0,
        },
        run_one,
        middleware=(),
    )

    assert not first.is_error
    assert not second.is_error
    assert first.content == "Step 1:\nTitle: Plan\nReasoning: Break down the query\nAction: Search\nConfidence: 0.9"
    assert second.content == (
        "Step 1:\nTitle: Plan\nReasoning: Break down the query\nAction: Search\nConfidence: 0.9\n\n"
        "Step 2:\nTitle: Check\nReasoning: Enough to answer\nAction: None\nConfidence: 1.0"
    )

    stored_run_one = [json.loads(raw) for raw in session_state["reasoning_steps"]["run-1"]]
    assert stored_run_one[0]["next_action"] == "continue"
    assert stored_run_one[1]["result"] == "Found 2 facts"
    assert stored_run_one[1]["next_action"] == "final_answer"

    run_two = ToolCallContext(run_id="run-2", session_id="sess-1", user_id=None, session_state=session_state)
    third = await execute_tool_call(
        functions["analyze"],
        {"title": "Verify", "result": "Draft", "analysis": "Need confirmation", "next_action": "validate"},
        run_two,
        middleware=(),
    )
    assert not third.is_error
    assert third.content.startswith("Step 1:\nTitle: Verify\n")
    stored_run_two = [json.loads(raw) for raw in session_state["reasoning_steps"]["run-2"]]
    assert stored_run_two == [
        {
            "title": "Verify",
            "action": None,
            "result": "Draft",
            "reasoning": "Need confirmation",
            "next_action": "validate",
            "confidence": 0.8,
        },
    ]


def test_direct_think_and_analyze_use_fallback_scratchpad() -> None:
    """Direct calls outside execute_tool_call still format and retain steps on the toolkit instance."""
    toolkit = ReasoningTools()

    first = toolkit.think(title="Start", thought="Direct call")
    second = toolkit.analyze(title="Finish", result="Done", analysis="All clear", next_action="unknown")

    assert "Step 1:\nTitle: Start\n" in first
    assert "Step 1:\nTitle: Start\n" in second
    assert "Step 2:\nTitle: Finish\n" in second
