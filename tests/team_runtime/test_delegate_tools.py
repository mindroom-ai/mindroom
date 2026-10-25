"""What the leader reads for a finished member run follows Agno's result rules, with and without streaming."""
# ruff: noqa: D103

from __future__ import annotations

from mindroom.llm.runs import RunRecord, RunStatus, TeamRunRecord, ToolExecution
from mindroom.teams.delegate_tools import NO_MEMBER_RESPONSE, collaborate_result_text, member_result_text
from tests.team_runtime.squad import member, scripted

_TOOLS = [ToolExecution(result="first"), ToolExecution(result=None), ToolExecution(result="second")]


def test_member_result_text_follows_agno_for_text_tool_results_and_silence() -> None:
    assert member_result_text(RunRecord(run_id="r", content="42"), stream=True) == "42"
    assert member_result_text(RunRecord(run_id="r", content="", tools=_TOOLS), stream=True) == "first,second"
    assert member_result_text(RunRecord(run_id="r", content=None, tools=_TOOLS), stream=True) == "first,second"
    assert member_result_text(RunRecord(run_id="r", content=None), stream=True) == NO_MEMBER_RESPONSE
    assert member_result_text(RunRecord(run_id="r", content=" ", tools=[]), stream=True) == NO_MEMBER_RESPONSE


def test_blocking_member_result_text_follows_agno_s_non_stream_delegation() -> None:
    assert member_result_text(RunRecord(run_id="r", content="42"), stream=False) == "42"
    assert member_result_text(RunRecord(run_id="r", content="", tools=_TOOLS), stream=False) == "first,second"
    # Only blank text selects the tool results; a run with tools and no text at all reads ``null``.
    assert member_result_text(RunRecord(run_id="r", content=None, tools=_TOOLS), stream=False) == "null"
    assert member_result_text(RunRecord(run_id="r", content=None), stream=False) == NO_MEMBER_RESPONSE
    assert member_result_text(RunRecord(run_id="r", content=" ", tools=[]), stream=False) == ""


def test_collaborate_result_text_labels_content_and_a_failed_member_only_without_streaming() -> None:
    members = [
        member("writer", "Writer", scripted("writer-model")),
        member("checker", "Checker", scripted("checker-model")),
    ]
    # A failed member hands over the reason its run recorded instead of the text it wrote, which the broadcast reads
    # like any result.
    children = [
        RunRecord(run_id="w", status=RunStatus.completed, content="42"),
        RunRecord(run_id="c", status=RunStatus.error, content="I will check.", error="Checker overloaded"),
    ]
    record = TeamRunRecord(run_id="t", team_id="squad", team_name="Squad")

    blocking = collaborate_result_text(members, children, record, stream=False)
    streamed = collaborate_result_text(members, children, record, stream=True)

    assert blocking == "Agent Writer: 42Agent Checker: Checker overloaded"
    assert streamed == "42Checker overloaded"
    assert record.member_responses == []
