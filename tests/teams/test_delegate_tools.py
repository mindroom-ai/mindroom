"""What the leader reads for a finished member run, with and without streaming, which is never ``null``.

``answer_delegation`` answers a delegation call that waited on a paused member once the member's run ends.
"""
# ruff: noqa: D103

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom.llm.runs import RunRecord, RunStatus, TeamRunRecord, ToolExecution
from mindroom.teams.delegate_tools import (
    DELEGATION_TOOLS,
    MEMBER_PAUSED_RESULT,
    NO_MEMBER_RESPONSE,
    answer_delegation,
    collaborate_result_text,
    delegate_functions,
    member_result_text,
)
from mindroom.teams.spec import TeamMode
from mindroom.tool_system.invocation import ToolCallContext, execute_tool_call
from tests.teams.squad import member, scripted

if TYPE_CHECKING:
    from mindroom.teams.spec import TeamMember

_TOOLS = [
    ToolExecution(tool_call_id="c1", tool_name="t", result="first"),
    ToolExecution(tool_call_id="c2", tool_name="t", result=None),
    ToolExecution(tool_call_id="c3", tool_name="t", result="second"),
]


def test_a_streamed_member_result_is_its_text_else_its_tool_results_else_the_no_response_notice() -> None:
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


def test_a_saved_coordinate_call_without_its_member_run_waits_on_the_member_it_names() -> None:
    """A call stored before calls named their member run is matched by member id and pause notice.

    Two members may share a display name, and so a pause notice; the call answered is the one naming the member.
    """
    writer, checker = (member(agent_id, "Twin", scripted(f"{agent_id}-model")) for agent_id in ("writer", "checker"))
    notice = MEMBER_PAUSED_RESULT.format(name="Twin")
    tools = [
        ToolExecution(
            tool_call_id=call_id,
            tool_name="delegate_task_to_member",
            tool_args={"member_id": agent_id},
            result=notice,
        )
        for call_id, agent_id in (("call-w", "writer"), ("call-c", "checker"))
    ]

    answered = answer_delegation(tools, checker, RunRecord(run_id="c", content="Checked."), stream=True)

    assert answered is tools[1]
    assert [tool.result for tool in tools] == [notice, "Checked."]
    assert answer_delegation(tools, writer, RunRecord(run_id="w", content="Wrote."), stream=True) is tools[0]


def test_two_saved_calls_to_one_member_are_answered_in_the_order_its_runs_resume() -> None:
    """Calls stored without their member runs cannot be told apart, so the first run to resume answers the first."""
    checker = member("checker", "Checker", scripted("checker-model"))
    notice = MEMBER_PAUSED_RESULT.format(name="Checker")
    tools = [
        ToolExecution(
            tool_call_id=call_id,
            tool_name="delegate_task_to_member",
            tool_args={"member_id": "checker"},
            result=notice,
        )
        for call_id in ("call-1", "call-2")
    ]

    answer_delegation(tools, checker, RunRecord(run_id="first", content="First."), stream=True)
    answer_delegation(tools, checker, RunRecord(run_id="second", content="Second."), stream=True)

    assert [tool.result for tool in tools] == ["First.", "Second."]


def test_each_of_two_paused_pieces_under_one_name_is_answered_by_its_own_member() -> None:
    """Two members that share a display name and both paused hold two equal pieces; each answer takes one of them."""
    writer, checker = (member(agent_id, "Twin", scripted(f"{agent_id}-model")) for agent_id in ("writer", "checker"))
    paused_piece = "Agent Twin: Requires human input before continuing."
    [tool] = tools = [
        ToolExecution(tool_call_id="call-all", tool_name="delegate_task_to_members", result=paused_piece * 2),
    ]

    answer_delegation(tools, writer, RunRecord(run_id="w", content="Wrote."), stream=True)
    answer_delegation(tools, checker, RunRecord(run_id="c", content="Checked."), stream=True)

    assert tool.result == "Agent Twin: Wrote.Agent Twin: Checked."


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
