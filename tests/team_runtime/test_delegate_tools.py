"""What the leader reads for a finished member run follows Agno's result rules, per mode."""
# ruff: noqa: D103

from __future__ import annotations

from mindroom.llm.runs import RunRecord, ToolExecution
from mindroom.teams.delegate_tools import NO_MEMBER_RESPONSE, member_result_text

_TOOLS = [ToolExecution(result="first"), ToolExecution(result=None), ToolExecution(result="second")]


def test_member_result_text_follows_agno_for_text_tool_results_and_silence() -> None:
    assert member_result_text(RunRecord(run_id="r", content="42"), stream=True) == "42"
    assert member_result_text(RunRecord(run_id="r", content={"answer": 42}), stream=True) == '{\n  "answer": 42\n}'
    assert member_result_text(RunRecord(run_id="r", content="", tools=_TOOLS), stream=True) == "first,second"
    assert member_result_text(RunRecord(run_id="r", content=None, tools=_TOOLS), stream=True) == "first,second"
    assert member_result_text(RunRecord(run_id="r", content=None), stream=True) == NO_MEMBER_RESPONSE
    assert member_result_text(RunRecord(run_id="r", content=" ", tools=[]), stream=True) == NO_MEMBER_RESPONSE


def test_blocking_member_result_text_follows_agno_s_non_stream_delegation() -> None:
    assert member_result_text(RunRecord(run_id="r", content="42"), stream=False) == "42"
    assert member_result_text(RunRecord(run_id="r", content={"answer": 42}), stream=False) == '{\n  "answer": 42\n}'
    assert member_result_text(RunRecord(run_id="r", content="", tools=_TOOLS), stream=False) == "first,second"
    # Only blank text selects the tool results; a run with tools and no text at all is JSON-encoded.
    assert member_result_text(RunRecord(run_id="r", content=None, tools=_TOOLS), stream=False) == "null"
    assert member_result_text(RunRecord(run_id="r", content=None), stream=False) == NO_MEMBER_RESPONSE
    assert member_result_text(RunRecord(run_id="r", content=" ", tools=[]), stream=False) == ""
