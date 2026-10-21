"""What the leader reads for a finished member run follows Agno's result rules."""
# ruff: noqa: D103

from __future__ import annotations

from mindroom.llm.runs import RunRecord, ToolExecution
from mindroom.teams.delegate_tools import NO_MEMBER_RESPONSE, member_result_text


def test_member_result_text_follows_agno_for_text_tool_results_and_silence() -> None:
    assert member_result_text(RunRecord(run_id="r", content="42")) == "42"
    assert member_result_text(RunRecord(run_id="r", content={"answer": 42})) == '{\n  "answer": 42\n}'
    tools = [ToolExecution(result="first"), ToolExecution(result=None), ToolExecution(result="second")]
    assert member_result_text(RunRecord(run_id="r", content="", tools=tools)) == "first,second"
    assert member_result_text(RunRecord(run_id="r", content=None, tools=tools)) == "first,second"
    assert member_result_text(RunRecord(run_id="r", content=None)) == NO_MEMBER_RESPONSE
    assert member_result_text(RunRecord(run_id="r", content=" ", tools=[])) == NO_MEMBER_RESPONSE
