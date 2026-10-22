"""Team resumes route each decision to the member that waits for it, then continue the leader as Agno did.

The C6 pre-resume dump of ``team-approved`` (a team paused on its checker's ``multiply``) is resumed with the
member's approval and compared with the ``team-approved`` rows Agno stored after the same resume.
"""
# ruff: noqa: D103

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from mindroom.history.request_messages import build_resume_request
from mindroom.llm.errors import ProviderError
from mindroom.llm.events import ToolCompleted, ToolStarted
from mindroom.llm.provider import TextDelta
from mindroom.llm.runs import RunStatus, TeamRunRecord
from mindroom.teams.delegate_tools import COLLABORATE_MEMBER_PAUSED_RESULT, MEMBER_PAUSED_RESULT
from mindroom.teams.runtime import MemberEvent, continue_team, run_team
from mindroom.teams.spec import TeamMode
from tests.agent_loop.helpers import echo, external, gated
from tests.fakes.scripted_model import Await, answer, calls, tool_call, usage
from tests.team_runtime.squad import (
    PROVIDER,
    SQUAD,
    SQUAD_APPROVED,
    Store,
    approval,
    calculator,
    collect,
    comparable,
    golden_metadata,
    golden_run,
    member_records,
    pre_resume_runs,
    prompt,
    run_context,
    scripted,
    squad,
)

if TYPE_CHECKING:
    from mindroom.llm.messages import Message

pytestmark = pytest.mark.asyncio

TIMED = ["duration", "end_time", "start_time"]
APPROVED_CALL = "call-checker-multiply-approved"


def _paused_team_approved() -> TeamRunRecord:
    paused = TeamRunRecord.from_dict(pre_resume_runs("team_squad__before_team-approved")["team-approved"])
    assert paused.status is RunStatus.paused
    return paused


def _resume_request(paused: TeamRunRecord) -> list[Message]:
    return build_resume_request(paused=paused.messages, summary=None, history=[])


async def test_an_approved_member_call_resumes_into_agno_s_team_approved_rows() -> None:
    paused = _paused_team_approved()
    before = paused.to_dict()
    team_row = golden_run("team_squad", SQUAD_APPROVED, "team-approved")
    checker_row = golden_run("team_squad", SQUAD_APPROVED, "00000000-0000-4000-8000-000000000063")
    leader = scripted("leader-model", answer("The checker found 42.", usage(470, 7, time_to_first_token=0.25)))
    checker = scripted("checker-model", answer("42", usage(95, 1, time_to_first_token=0.25)))
    store = Store()

    events, record = await collect(
        continue_team(
            squad(leader, scripted("writer-model"), checker),
            paused,
            _resume_request(paused),
            approved={
                ("checker", APPROVED_CALL): approval(APPROVED_CALL, "multiply", {"a": 6, "b": 7}, agent="checker"),
            },
            ctx=run_context("team-approved", SQUAD_APPROVED, golden_metadata(team_row)),
            persist=store,
        ),
    )

    assert comparable(record.to_dict()) == comparable(team_row)
    [member] = member_records(events)
    expected_member = comparable(checker_row)
    # Agno recorded no timing for the call it ran on resume (``metrics: null``); the owned loop times every call it
    # runs, on the tool entry and on the requirement that shares it. Everything else is equal by value.
    assert expected_member["tools"][0]["metrics"] is None
    expected_member["tools"][0]["metrics"] = TIMED
    expected_member["requirements"][0]["tool_execution"]["metrics"] = TIMED
    assert comparable(member.to_dict()) == expected_member
    assert paused.to_dict() == before
    assert store.statuses == [("checker", "COMPLETED"), ("squad", "COMPLETED")]
    # The leader reads the member's result where it read the pause notice, and keeps its stored prompt first.
    [request] = leader.requests
    assert [message.role for message in request.messages] == ["system", "user", "assistant", "tool"]
    assert request.messages[-1].content == "Member results after human-in-the-loop resolution:\n[Checker]: 42"
    assert [tool.name for tool in request.tools] == ["delegate_task_to_member"]
    # The member ran exactly the approved call, announced as its own tool events, from its stored request.
    checker_events = [event.event for event in events if isinstance(event, MemberEvent)]
    assert [type(event).__name__ for event in checker_events if isinstance(event, ToolStarted | ToolCompleted)] == [
        "ToolStarted",
        "ToolCompleted",
    ]
    [checker_request] = checker.requests
    assert [message.role for message in checker_request.messages] == ["system", "user", "assistant", "tool"]
    assert checker_request.messages[-1].content == '{"operation": "multiplication", "result": 42.0}'


async def test_a_member_that_pauses_again_keeps_the_team_paused_without_a_leader_request() -> None:
    paused = _paused_team_approved()
    leader = scripted("leader-model")  # Any request would fail: the scripted model has no turn for it.
    checker = scripted("checker-model", calls(tool_call("multiply", "call-again", a=2, b=3), metrics=usage(10, 1)))
    store = Store()

    events, record = await collect(
        continue_team(
            squad(leader, scripted("writer-model"), checker),
            paused,
            _resume_request(paused),
            approved={
                ("checker", APPROVED_CALL): approval(APPROVED_CALL, "multiply", {"a": 6, "b": 7}, agent="checker"),
            },
            ctx=run_context("team-approved", SQUAD_APPROVED),
            persist=store,
        ),
    )

    assert (record.status, leader.requests) == (RunStatus.paused, [])
    [member] = member_records(events)
    assert member.status is RunStatus.paused
    assert record.member_responses == [member]
    assert record.requirements is not None
    [waiting] = [requirement for requirement in record.requirements if not requirement.is_resolved()]
    assert (waiting.tool_execution.tool_call_id, waiting.member_agent_id, waiting.member_run_id) == (
        "call-again",
        "checker",
        member.run_id,
    )
    assert record.content == "Team run paused. The following require input:\n- Checker: multiply requires confirmation"
    # The delegation still reports the pause: the member results reach the leader once every member is done.
    assert record.tools is not None
    assert record.tools[0].result == MEMBER_PAUSED_RESULT.format(name="Checker")
    assert store.statuses == [("checker", "PAUSED"), ("squad", "PAUSED")]


async def test_a_denied_member_call_hands_the_leader_the_member_s_answer_to_the_denial() -> None:
    paused = _paused_team_approved()
    leader = scripted("leader-model", answer("The checker did not multiply.", usage(1, 1)))
    checker = scripted("checker-model", answer("I did not multiply.", usage(1, 1)))

    events, record = await collect(
        continue_team(
            squad(leader, scripted("writer-model"), checker),
            paused,
            _resume_request(paused),
            denied={("checker", APPROVED_CALL): "Alice declined."},
            ctx=run_context("team-approved", SQUAD_APPROVED),
        ),
    )

    assert record.status is RunStatus.completed
    assert record.tools is not None
    assert (
        record.tools[0].result == "Member results after human-in-the-loop resolution:\n[Checker]: I did not multiply."
    )
    [member] = member_records(events)
    assert member.requirements is not None
    assert (member.requirements[0].confirmation, member.requirements[0].confirmation_note) == (
        False,
        "Alice declined.",
    )
    [checker_request] = checker.requests
    assert checker_request.messages[-1].content == "Alice declined."


async def test_a_collaborate_resume_hands_the_leader_only_the_resumed_members_results_as_agno_did() -> None:
    """Pin Agno's continuation: the members' results replace the whole delegation result, siblings' text included.

    A collaborate delegation returns every member's text joined; when one member pauses, Agno's resume rewrote that
    result as the heading plus the members resumed in this call, so the text of a member that had completed earlier
    (the writer's here) no longer reaches the leader. The owned runtime keeps that request body for parity.
    """
    leader = scripted(
        "leader-model",
        calls(tool_call("delegate_task_to_members", "call-all", task="Multiply 6 by 7."), metrics=usage(10, 2)),
        answer("The panel says 42.", usage(10, 2)),
    )
    writer = scripted("writer-model", answer("Six times seven is 42."))
    checker = scripted(
        "checker-model",
        calls(tool_call("multiply", "call-mul", a=6, b=7), metrics=usage(5, 1)),
        answer("42", usage(5, 1)),
    )
    team = squad(leader, writer, checker, mode=TeamMode.COLLABORATE)
    _, paused = await collect(
        run_team(team, prompt({"messages": [{"role": "user", "content": "hi"}]}), ctx=run_context("r", SQUAD)),
    )
    assert paused.tools is not None
    assert paused.tools[0].result == "Six times seven is 42." + COLLABORATE_MEMBER_PAUSED_RESULT.format(name="Checker")

    events, record = await collect(
        continue_team(
            team,
            paused,
            _resume_request(paused),
            approved={("checker", "call-mul"): approval("call-mul", "multiply", {"a": 6, "b": 7}, agent="checker")},
            ctx=run_context("r", SQUAD),
        ),
    )

    assert (record.status, record.content) == (RunStatus.completed, "The panel says 42.")
    # The writer's text is gone from the tool entry and from what the leader reads: only the resumed checker remains.
    resolved = "Member results after human-in-the-loop resolution:\n[Checker]: 42"
    assert record.tools is not None
    assert record.tools[0].result == resolved
    assert leader.requests[-1].messages[-1].content == resolved
    # Only the paused member resumes; the writer is neither re-run nor re-read.
    assert [member.agent_id for member in member_records(events)] == ["checker"]
    assert len(writer.requests) == 1


async def test_a_leader_level_approval_runs_the_leader_s_call_and_continues() -> None:
    leader = scripted(
        "leader-model",
        calls(tool_call("echo", "call-echo", text="hello"), metrics=usage(10, 2)),
        answer("Echoed hello.", usage(10, 2)),
    )
    team = squad(
        leader,
        scripted("writer-model"),
        scripted("checker-model"),
        leader_toolkits=(gated(echo, name="job_controls"),),
    )
    _, paused = await collect(
        run_team(team, prompt({"messages": [{"role": "user", "content": "hi"}]}), ctx=run_context("r", SQUAD)),
    )
    assert paused.status is RunStatus.paused

    events, record = await collect(
        continue_team(
            team,
            paused,
            _resume_request(paused),
            approved={(None, "call-echo"): approval("call-echo", "echo", {"text": "hello"}, agent="squad")},
            ctx=run_context("r", SQUAD),
        ),
    )

    assert (record.status, record.content) == (RunStatus.completed, "Echoed hello.")
    assert record.tools is not None
    assert (record.tools[0].result, record.tools[0].confirmed) == ("hello", True)
    # The leader's own call is announced as the team's tool events, not as a member's.
    assert [type(event).__name__ for event in events if isinstance(event, ToolStarted | ToolCompleted)] == [
        "ToolStarted",
        "ToolCompleted",
    ]
    assert member_records(events) == []
    assert leader.requests[-1].messages[-1].content == "hello"


async def test_an_external_result_resumes_the_member_that_waits_for_it() -> None:
    def run_subagent(agent_name: str, task: str) -> str:
        """Delegate a task to another agent.

        Args:
            agent_name: The agent to run.
            task: What it should do.

        """
        msg = f"{agent_name} runs outside the loop: {task}"
        raise AssertionError(msg)

    leader = scripted(
        "leader-model",
        calls(
            tool_call("delegate_task_to_member", "call-team-sub", member_id="checker", task="Ask the helper"),
            metrics=usage(10, 2),
        ),
        answer("The helper found 42.", usage(10, 2)),
    )
    checker = scripted(
        "checker-model",
        calls(tool_call("run_subagent", "call-sub", agent_name="helper", task="Multiply"), metrics=usage(5, 1)),
        answer("42", usage(5, 1)),
    )
    team = squad(leader, scripted("writer-model"), checker, checker_toolkits=(external(run_subagent),))
    _, paused = await collect(
        run_team(team, prompt({"messages": [{"role": "user", "content": "hi"}]}), ctx=run_context("r", SQUAD)),
    )

    events, record = await collect(
        continue_team(
            team,
            paused,
            _resume_request(paused),
            external_results={("checker", "call-sub"): "Helper says 42"},
            ctx=run_context("r", SQUAD),
        ),
    )

    assert (record.status, record.content) == (RunStatus.completed, "The helper found 42.")
    assert record.tools is not None
    assert record.tools[0].result == "Member results after human-in-the-loop resolution:\n[Checker]: 42"
    [member] = member_records(events)
    assert member.requirements is not None
    assert member.requirements[0].external_execution_result == "Helper says 42"
    assert checker.requests[-1].messages[-1].content == "Helper says 42"
    assert record.requirements == []


async def test_refusals_leave_the_paused_run_untouched_and_run_nothing() -> None:
    paused = _paused_team_approved()
    before = paused.to_dict()
    leader, writer, checker = scripted("leader-model"), scripted("writer-model"), scripted("checker-model")
    team = squad(leader, writer, checker)
    ctx = run_context("team-approved", SQUAD_APPROVED)
    decision = approval(APPROVED_CALL, "multiply", {"a": 6, "b": 7}, agent="checker")

    with pytest.raises(ValueError, match="Decisions do not cover exactly the calls"):
        continue_team(team, paused, _resume_request(paused), ctx=ctx)
    with pytest.raises(ValueError, match="name calls the team does not wait for"):
        continue_team(
            team,
            paused,
            _resume_request(paused),
            approved={
                ("checker", APPROVED_CALL): decision,
                (None, "call-unknown"): approval("call-unknown", "add", {"a": 1, "b": 2}, agent="squad"),
            },
            ctx=ctx,
        )
    # A decision on the right call id but the wrong member leaves the checker's call undecided.
    with pytest.raises(ValueError, match="Decisions do not cover exactly the calls"):
        continue_team(
            team,
            paused,
            _resume_request(paused),
            approved={("writer", APPROVED_CALL): decision},
            ctx=ctx,
        )
    with pytest.raises(ValueError, match="name calls the team does not wait for"):
        continue_team(
            team,
            paused,
            _resume_request(paused),
            approved={("checker", APPROVED_CALL): decision},
            denied={("writer", "call-elsewhere"): None},
            ctx=ctx,
        )
    without_checker = replace(team, members=team.members[:1])
    with pytest.raises(ValueError, match="not a member of team 'squad'"):
        continue_team(
            without_checker,
            paused,
            _resume_request(paused),
            approved={("checker", APPROVED_CALL): decision},
            ctx=ctx,
        )
    orphaned = replace(paused, member_responses=[])
    with pytest.raises(ValueError, match="no longer holds member run"):
        continue_team(team, orphaned, _resume_request(paused), approved={("checker", APPROVED_CALL): decision}, ctx=ctx)
    assert paused.requirements is not None
    unbound = replace(paused, requirements=[replace(paused.requirements[0], member_run_id=None)])
    with pytest.raises(ValueError, match="has no paused run identity"):
        continue_team(team, unbound, _resume_request(paused), approved={("checker", APPROVED_CALL): decision}, ctx=ctx)
    completed = replace(paused, status=RunStatus.completed)
    with pytest.raises(ValueError, match="Only a paused run can be resumed"):
        continue_team(
            team,
            completed,
            _resume_request(paused),
            approved={("checker", APPROVED_CALL): decision},
            ctx=ctx,
        )

    assert paused.to_dict() == before
    assert (leader.requests, writer.requests, checker.requests) == ([], [], [])


async def test_a_collaborate_resume_routes_each_approval_to_the_member_that_waits_for_it() -> None:
    """Two paused members resume from one approval, each on its own call, and the leader reads both results."""
    leader = scripted(
        "leader-model",
        calls(tool_call("delegate_task_to_members", "call-all", task="Multiply 6 by 7."), metrics=usage(10, 2)),
        answer("Both say 42.", usage(10, 2)),
    )
    # Both members pause on a call with the same id: the member names the call, not the id alone.
    writer = scripted(
        "writer-model",
        calls(tool_call("multiply", "call-mul", a=6, b=7), metrics=usage(5, 1)),
        answer("Writer: 42", usage(5, 1)),
    )
    checker = scripted(
        "checker-model",
        calls(tool_call("multiply", "call-mul", a=7, b=6), metrics=usage(5, 1)),
        answer("Checker: 42", usage(5, 1)),
    )
    team = squad(
        leader,
        writer,
        checker,
        mode=TeamMode.COLLABORATE,
        writer_toolkits=(calculator(gate_multiply=True),),
    )
    _, paused = await collect(
        run_team(team, prompt({"messages": [{"role": "user", "content": "hi"}]}), ctx=run_context("r", SQUAD)),
    )
    assert paused.requirements is not None
    assert sorted((r.member_agent_id, r.tool_execution.tool_call_id) for r in paused.requirements) == [
        ("checker", "call-mul"),
        ("writer", "call-mul"),
    ]
    assert [member.agent_id for member in paused.member_responses] == ["writer", "checker"]

    events, record = await collect(
        continue_team(
            team,
            paused,
            _resume_request(paused),
            approved={
                ("writer", "call-mul"): approval("call-mul", "multiply", {"a": 6, "b": 7}, agent="writer"),
                ("checker", "call-mul"): approval("call-mul", "multiply", {"a": 7, "b": 6}, agent="checker"),
            },
            ctx=run_context("r", SQUAD),
        ),
    )

    assert (record.status, record.content) == (RunStatus.completed, "Both say 42.")
    resumed = member_records(events)
    assert [(member.agent_id, member.status) for member in resumed] == [
        ("writer", RunStatus.completed),
        ("checker", RunStatus.completed),
    ]
    # Each member ran its own approved call with its own arguments.
    assert writer.requests[-1].messages[-1].content == '{"operation": "multiplication", "result": 42.0}'
    assert checker.requests[-1].messages[-1].content == '{"operation": "multiplication", "result": 42.0}'
    assert record.tools is not None
    assert record.tools[0].result == (
        "Member results after human-in-the-loop resolution:\n[Writer]: Writer: 42\n[Checker]: Checker: 42"
    )
    assert leader.requests[-1].messages[-1].content == record.tools[0].result
    assert record.requirements == []


@pytest.mark.parametrize("both_pause", [True, False], ids=["both-paused", "one-paused"])
async def test_a_coordinate_resume_answers_each_waiting_delegation_with_the_resumed_members_results(
    both_pause: bool,
) -> None:
    """Two delegations of one leader response: each paused member resumes on its own call, as Agno's continuation did.

    Every delegation that waited on a member reads the resumed members' results; one whose member completed before
    the pause keeps that member's own answer.
    """
    leader = scripted(
        "leader-model",
        calls(
            tool_call("delegate_task_to_member", "call-w", member_id="writer", task="Multiply 6 by 7."),
            tool_call("delegate_task_to_member", "call-c", member_id="checker", task="Multiply 7 by 6."),
            metrics=usage(10, 2),
        ),
        answer("Both say 42.", usage(10, 2)),
    )
    writer_turns = [answer("Writer: 42", usage(5, 1))]
    if both_pause:
        writer_turns.insert(0, calls(tool_call("multiply", "call-mul", a=6, b=7), metrics=usage(5, 1)))
    writer = scripted("writer-model", *writer_turns)
    checker = scripted(
        "checker-model",
        calls(tool_call("multiply", "call-mul", a=7, b=6), metrics=usage(5, 1)),
        answer("Checker: 42", usage(5, 1)),
    )
    team = squad(leader, writer, checker, writer_toolkits=(calculator(gate_multiply=True),))
    _, paused = await collect(
        run_team(team, prompt({"messages": [{"role": "user", "content": "hi"}]}), ctx=run_context("r", SQUAD)),
    )
    assert paused.status is RunStatus.paused
    assert paused.requirements is not None
    waiting = sorted((r.member_agent_id, r.tool_execution.tool_call_id) for r in paused.requirements)
    assert waiting == ([("checker", "call-mul"), ("writer", "call-mul")] if both_pause else [("checker", "call-mul")])
    assert paused.tools is not None
    assert [(tool.tool_call_id, tool.result) for tool in paused.tools] == [
        ("call-w", MEMBER_PAUSED_RESULT.format(name="Writer") if both_pause else "Writer: 42"),
        ("call-c", MEMBER_PAUSED_RESULT.format(name="Checker")),
    ]
    approved = {("checker", "call-mul"): approval("call-mul", "multiply", {"a": 7, "b": 6}, agent="checker")}
    if both_pause:
        approved[("writer", "call-mul")] = approval("call-mul", "multiply", {"a": 6, "b": 7}, agent="writer")

    events, record = await collect(
        continue_team(team, paused, _resume_request(paused), approved=approved, ctx=run_context("r", SQUAD)),
    )

    assert (record.status, record.content) == (RunStatus.completed, "Both say 42.")
    assert [member.agent_id for member in member_records(events)] == (
        ["writer", "checker"] if both_pause else ["checker"]
    )
    assert (len(writer.requests), len(checker.requests)) == ((2, 2) if both_pause else (1, 2))
    heading = "Member results after human-in-the-loop resolution:"
    resumed = (
        f"{heading}\n[Writer]: Writer: 42\n[Checker]: Checker: 42"
        if both_pause
        else f"{heading}\n[Checker]: Checker: 42"
    )
    assert record.tools is not None
    assert {tool.tool_call_id: tool.result for tool in record.tools} == {
        "call-w": resumed if both_pause else "Writer: 42",
        "call-c": resumed,
    }
    assert [message.content for message in leader.requests[-1].messages if message.role == "tool"] == [
        resumed if both_pause else "Writer: 42",
        resumed,
    ]
    assert record.requirements == []


async def test_a_member_approval_that_shares_an_id_with_a_leader_call_resumes_the_member() -> None:
    """A member's paused call with the id of the leader's completed delegation resumes, and that delegation stays.

    Call ids come from separate provider requests, so nothing keeps a member's ids apart from the leader's; the
    member's call belongs to the member's run and never stands in for the leader's tool entry.
    """
    leader = scripted(
        "leader-model",
        calls(
            tool_call("delegate_task_to_member", "call-w", member_id="writer", task="Multiply 6 by 7."),
            tool_call("delegate_task_to_member", "call-c", member_id="checker", task="Multiply 7 by 6."),
            metrics=usage(10, 2),
        ),
        answer("Both say 42.", usage(10, 2)),
    )
    writer = scripted("writer-model", answer("Writer: 42", usage(5, 1)))
    checker = scripted(
        "checker-model",
        calls(tool_call("multiply", "call-w", a=7, b=6), metrics=usage(5, 1)),
        answer("Checker: 42", usage(5, 1)),
    )
    team = squad(leader, writer, checker)
    _, paused = await collect(
        run_team(team, prompt({"messages": [{"role": "user", "content": "hi"}]}), ctx=run_context("r", SQUAD)),
    )
    assert paused.requirements is not None
    assert [(r.member_agent_id, r.tool_execution.tool_call_id) for r in paused.requirements] == [("checker", "call-w")]
    assert paused.tools is not None
    assert [(tool.tool_call_id, tool.result) for tool in paused.tools] == [
        ("call-w", "Writer: 42"),
        ("call-c", MEMBER_PAUSED_RESULT.format(name="Checker")),
    ]

    events, record = await collect(
        continue_team(
            team,
            paused,
            _resume_request(paused),
            approved={("checker", "call-w"): approval("call-w", "multiply", {"a": 7, "b": 6}, agent="checker")},
            ctx=run_context("r", SQUAD),
        ),
    )

    assert (record.status, record.content) == (RunStatus.completed, "Both say 42.")
    [member] = member_records(events)
    assert (member.agent_id, member.status) == ("checker", RunStatus.completed)
    assert checker.requests[-1].messages[-1].content == '{"operation": "multiplication", "result": 42.0}'
    resumed = "Member results after human-in-the-loop resolution:\n[Checker]: Checker: 42"
    assert record.tools is not None
    assert [(tool.tool_call_id, tool.tool_name, tool.result) for tool in record.tools] == [
        ("call-w", "delegate_task_to_member", "Writer: 42"),
        ("call-c", "delegate_task_to_member", resumed),
    ]
    assert [message.content for message in leader.requests[-1].messages if message.role == "tool"] == [
        "Writer: 42",
        resumed,
    ]
    assert record.requirements == []


async def test_a_resume_with_member_and_leader_decisions_runs_both_and_hands_the_leader_the_member_s_result() -> None:
    """A mixed resume answers the delegation with the member's result and runs the leader's own approved call.

    Agno's continuation took its team-level branch here and left the delegation result as the pause notice; the owned
    runtime gives the leader the member's result in the same entry, a deviation that favors the leader.
    """
    leader = scripted(
        "leader-model",
        calls(
            tool_call("delegate_task_to_member", "call-team", member_id="checker", task="Multiply 6 by 7"),
            tool_call("echo", "call-echo", text="hello"),
            metrics=usage(10, 2),
        ),
        answer("42 and hello.", usage(10, 2)),
    )
    checker = scripted(
        "checker-model",
        calls(tool_call("multiply", "call-mul", a=6, b=7), metrics=usage(5, 1)),
        answer("42", usage(5, 1)),
    )
    team = squad(leader, scripted("writer-model"), checker, leader_toolkits=(gated(echo, name="job_controls"),))
    _, paused = await collect(
        run_team(team, prompt({"messages": [{"role": "user", "content": "hi"}]}), ctx=run_context("r", SQUAD)),
    )
    assert paused.requirements is not None
    assert {(r.member_agent_id, r.tool_execution.tool_call_id) for r in paused.requirements} == {
        (None, "call-echo"),
        ("checker", "call-mul"),
    }

    _, record = await collect(
        continue_team(
            team,
            paused,
            _resume_request(paused),
            approved={
                ("checker", "call-mul"): approval("call-mul", "multiply", {"a": 6, "b": 7}, agent="checker"),
                (None, "call-echo"): approval("call-echo", "echo", {"text": "hello"}, agent="squad"),
            },
            ctx=run_context("r", SQUAD),
        ),
    )

    assert (record.status, record.content) == (RunStatus.completed, "42 and hello.")
    assert record.tools is not None
    results = {tool.tool_name: tool.result for tool in record.tools}
    assert results == {
        "delegate_task_to_member": "Member results after human-in-the-loop resolution:\n[Checker]: 42",
        "echo": "hello",
    }
    assert [message.content for message in leader.requests[-1].messages if message.role == "tool"] == [
        results["delegate_task_to_member"],
        "hello",
    ]


async def test_a_failed_resume_stores_its_error_over_the_paused_team_row_as_agno_did() -> None:
    """A leader request that fails after the member resumed ends the team run ``ERROR`` over its ``PAUSED`` row (D15).

    The member's resumed row is stored completed first, so the approved call is not offered again.
    """
    paused = _paused_team_approved()

    async def lose_the_provider() -> None:
        msg = "provider connection lost"
        raise ProviderError(msg, provider=PROVIDER)

    store = Store()
    events = continue_team(
        squad(
            scripted("leader-model", [Await(lose_the_provider)]),
            scripted("writer-model"),
            scripted(
                "checker-model",
                answer("42", usage(5, 1)),
            ),
        ),
        paused,
        _resume_request(paused),
        approved={("checker", APPROVED_CALL): approval(APPROVED_CALL, "multiply", {"a": 6, "b": 7}, agent="checker")},
        ctx=run_context("team-approved", SQUAD_APPROVED),
        persist=store,
    )

    with pytest.raises(ProviderError, match="provider connection lost"):
        await collect(events)

    assert store.statuses == [("checker", "COMPLETED"), ("squad", "ERROR")]
    member_row, team_row = store.records
    assert member_row["run_id"] == paused.member_responses[0].run_id
    assert team_row["run_id"] == "team-approved"
    assert "provider connection lost" in team_row["content"]
    assert team_row["requirements"] == []


async def test_cancelling_a_resume_stores_cancelled_rows_over_the_paused_ones_as_agno_did() -> None:
    """A Stop during a member's resume ends the member and the team ``CANCELLED``; the approval is not resumable again.

    Agno's continuation stored the same rows, so the owned runtime keeps that; the envelope writes its interrupted
    record over the team row afterwards.
    """
    paused = _paused_team_approved()
    resuming = asyncio.Event()

    async def wait_to_be_cancelled() -> None:
        resuming.set()
        await asyncio.Event().wait()

    checker = scripted("checker-model", [TextDelta("Dra"), Await(wait_to_be_cancelled)])
    store = Store()

    async def consume() -> None:
        events = continue_team(
            squad(scripted("leader-model"), scripted("writer-model"), checker),
            paused,
            _resume_request(paused),
            approved={
                ("checker", APPROVED_CALL): approval(APPROVED_CALL, "multiply", {"a": 6, "b": 7}, agent="checker"),
            },
            ctx=run_context("team-approved", SQUAD_APPROVED),
            persist=store,
        )
        async for _ in events:
            pass

    task = asyncio.create_task(consume())
    await asyncio.wait_for(resuming.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert store.statuses == [("checker", "CANCELLED"), ("squad", "CANCELLED")]
    member_row, team_row = store.records
    assert member_row["run_id"] == paused.member_responses[0].run_id
    assert team_row["run_id"] == "team-approved"
    assert team_row["requirements"] == []
