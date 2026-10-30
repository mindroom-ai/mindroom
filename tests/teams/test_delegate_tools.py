"""What the leader reads for a finished member run: its text, else its tool results, else a notice, never ``null``.

``answer_delegation`` answers a delegation call that waited on a paused member once the member's run ends.
"""
# ruff: noqa: D103

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom.llm.runs import RunRecord, RunStatus, TeamRunRecord, ToolExecution
from mindroom.teams.delegate_tools import (
    DELEGATION_TOOLS,
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


def test_member_result_text_gives_text_else_tool_results_else_the_no_response_notice() -> None:
    assert member_result_text(RunRecord(run_id="r", content="42")) == "42"
    assert member_result_text(RunRecord(run_id="r", content="", tools=_TOOLS)) == "first,second"
    assert member_result_text(RunRecord(run_id="r", content=None, tools=_TOOLS)) == "first,second"
    assert member_result_text(RunRecord(run_id="r", content=None)) == NO_MEMBER_RESPONSE
    assert member_result_text(RunRecord(run_id="r", content=" ", tools=[])) == NO_MEMBER_RESPONSE


def test_collaborate_result_text_labels_every_member_s_piece_and_separates_them() -> None:
    members = [
        member("writer", "Writer", scripted("writer-model")),
        member("checker", "Checker", scripted("checker-model")),
        member("reader", "Reader", scripted("reader-model")),
    ]
    # A failed member hands over the reason its run recorded instead of the text it wrote, which the broadcast reads
    # like any result; a member that ran tools without text gives their results.
    children = [
        RunRecord(run_id="w", status=RunStatus.completed, content="42"),
        RunRecord(run_id="c", status=RunStatus.error, content="I will check.", error="Checker overloaded"),
        RunRecord(run_id="r", status=RunStatus.completed, content=None, tools=_TOOLS),
    ]
    record = TeamRunRecord(run_id="t", team_id="squad")

    assert collaborate_result_text(members, children, record) == (
        "Agent Writer: 42\n\nAgent Checker: Checker overloaded\n\nAgent Reader: first,second"
    )
    assert record.member_responses == []


def test_each_of_two_paused_pieces_under_one_name_is_answered_by_its_own_member() -> None:
    """Two members that share a display name and both paused hold two equal pieces; each answer takes one of them."""
    writer, checker = (member(agent_id, "Twin", scripted(f"{agent_id}-model")) for agent_id in ("writer", "checker"))
    paused_piece = "Agent Twin: Requires human input before continuing."
    [tool] = tools = [
        ToolExecution(
            tool_call_id="call-all",
            tool_name="delegate_task_to_members",
            result=f"{paused_piece}\n\n{paused_piece}",
        ),
    ]

    answer_delegation(tools, writer, RunRecord(run_id="w", content="Wrote."))
    answer_delegation(tools, checker, RunRecord(run_id="c", content="Checked."))

    assert tool.result == "Agent Twin: Wrote.\n\nAgent Twin: Checked."


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
        return RunRecord(run_id=member.id, status=RunStatus.completed, content="done")

    members = [member("writer", "Writer", scripted("writer-model"))]
    record = TeamRunRecord(run_id="t", team_id="squad")
    [function] = delegate_functions(members, mode, record=record, run_member=run_member).functions

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
