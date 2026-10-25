"""What the leader reads for a finished member run, with and without streaming: Agno's rules, but never ``null``."""
# ruff: noqa: D103

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom.llm.runs import RunRecord, RunStatus, TeamRunRecord, ToolExecution
from mindroom.teams.delegate_tools import (
    DELEGATION_TOOLS,
    NO_MEMBER_RESPONSE,
    collaborate_result_text,
    delegate_functions,
    member_result_text,
)
from mindroom.teams.spec import TeamMode
from mindroom.tool_system.invocation import ToolCallContext, execute_tool_call
from tests.team_runtime.squad import member, scripted

if TYPE_CHECKING:
    from mindroom.teams.spec import TeamMember

_TOOLS = [ToolExecution(result="first"), ToolExecution(result=None), ToolExecution(result="second")]


def test_member_result_text_follows_agno_for_text_tool_results_and_silence() -> None:
    assert member_result_text(RunRecord(run_id="r", content="42"), stream=True) == "42"
    assert member_result_text(RunRecord(run_id="r", content="", tools=_TOOLS), stream=True) == "first,second"
    assert member_result_text(RunRecord(run_id="r", content=None, tools=_TOOLS), stream=True) == "first,second"
    assert member_result_text(RunRecord(run_id="r", content=None), stream=True) == NO_MEMBER_RESPONSE
    assert member_result_text(RunRecord(run_id="r", content=" ", tools=[]), stream=True) == NO_MEMBER_RESPONSE


def test_blocking_member_result_text_differs_only_for_a_blank_run_without_tools() -> None:
    assert member_result_text(RunRecord(run_id="r", content="42"), stream=False) == "42"
    assert member_result_text(RunRecord(run_id="r", content="", tools=_TOOLS), stream=False) == "first,second"
    assert member_result_text(RunRecord(run_id="r", content=None, tools=_TOOLS), stream=False) == "first,second"
    assert member_result_text(RunRecord(run_id="r", content=None), stream=False) == NO_MEMBER_RESPONSE
    # A blank run without tools reads empty, where the streamed rules give the no-response notice.
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
    record = TeamRunRecord(run_id="t", team_id="squad")

    blocking = collaborate_result_text(members, children, record, stream=False)
    streamed = collaborate_result_text(members, children, record, stream=True)

    assert blocking == "Agent Writer: 42Agent Checker: Checker overloaded"
    assert streamed == "42Checker overloaded"
    assert record.member_responses == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "arguments"),
    [
        (TeamMode.COORDINATE, {"member_id": "writer", "task": 5}),
        (TeamMode.COORDINATE, {"member_id": "writer"}),
        (TeamMode.COLLABORATE, {"task": ["two", "tasks"]}),
    ],
    ids=["mistyped", "missing", "collaborate-mistyped"],
)
async def test_a_delegation_with_arguments_its_definition_refuses_fails_and_runs_no_member(
    mode: TeamMode,
    arguments: dict[str, object],
) -> None:
    """A delegation call whose arguments do not fit its signature fails with the validation error the model reads."""
    asked: list[str] = []

    async def run_member(member: TeamMember, task: str) -> RunRecord:
        asked.append(task)
        return RunRecord(run_id=member.spec.id, status=RunStatus.completed, content="done")

    members = [member("writer", "Writer", scripted("writer-model"))]
    record = TeamRunRecord(run_id="t", team_id="squad")
    [function] = delegate_functions(members, mode, record=record, run_member=run_member, stream=True).functions

    outcome = await execute_tool_call(
        function,
        arguments,
        ToolCallContext(run_id="t", session_id="s", user_id=None),
        middleware=(),
    )

    assert function.definition() == DELEGATION_TOOLS[mode]
    assert outcome.is_error
    assert outcome.content.startswith(f"1 validation error for delegate_functions.<locals>.{function.name}\n")
    assert asked == []
