"""Team resumes route each decision to the member that waits for it, then continue the leader.

Most resumes start from the team run Agno stored paused on its checker's ``multiply`` (the ``team-approved``
pre-resume dump), so a run paused before the upgrade resumes after it; the resumed records are asserted as typed values.
"""
# ruff: noqa: D103

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from mindroom.history.request_messages import build_resume_request
from mindroom.history.types import HistoryScope
from mindroom.llm.errors import ProviderError
from mindroom.llm.events import ToolCompleted, ToolStarted
from mindroom.llm.messages import Message
from mindroom.llm.provider import TextDelta, TurnCompleted
from mindroom.llm.runs import RunStatus, TeamRunRecord, ToolExecution
from mindroom.session_storage.agent_storage import create_state_storage
from mindroom.session_storage.schema import TableNames
from mindroom.teams.delegate_tools import MEMBER_PAUSED_RESULT, NO_MEMBER_RESPONSE
from mindroom.teams.runtime import MemberEvent
from mindroom.teams.spec import TeamMode
from mindroom.tool_system.tool_function import ToolGate
from tests.agent_loop.helpers import echo, external, gated, toolkit
from tests.fakes.scripted_model import Await, answer, calls, tool_call, usage
from tests.llm.agno_dict_helpers import stored_requirement, with_requirements
from tests.run_record_helpers import PRODUCT, approved_call, reply, result, resumed, run_metrics, settled, waiting_call
from tests.session_storage.layout_4 import store_rows, upgrade_from_layout, upgrade_from_layout_4
from tests.teams.squad import (
    CHECKER_PAUSED_PIECE,
    PROVIDER,
    SQUAD,
    SQUAD_APPROVED,
    Store,
    approval,
    calculator,
    collect,
    pre_resume_runs,
    prompt,
    resume_team,
    run_context,
    scripted,
    squad,
    start_team,
)

if TYPE_CHECKING:
    from pathlib import Path

    from agno.run import RunContext as AgnoRunContext

    from mindroom.agent_loop.spec import RunContext
    from mindroom.llm.runs import AnyRunRecord

pytestmark = pytest.mark.asyncio

APPROVED_CALL = "call-checker-multiply-approved"
_SQUAD_SCOPE = HistoryScope(kind="team", scope_id="squad")


def _paused_team_approved() -> TeamRunRecord:
    paused = TeamRunRecord.from_dict(pre_resume_runs("team_squad__before_team-approved")["team-approved"])
    assert paused.status is RunStatus.paused
    return paused


def _resume_request(paused: TeamRunRecord) -> list[Message]:
    return build_resume_request(paused=paused.messages, summary=None, history=[])


async def test_an_approved_member_call_resumes_the_member_and_then_the_leader() -> None:
    paused = _paused_team_approved()
    before = paused.to_dict()
    leader_usage, checker_usage = usage(470, 7, time_to_first_token=0.25), usage(95, 1, time_to_first_token=0.25)
    leader = scripted("leader-model", answer("The checker found 42.", leader_usage))
    checker = scripted("checker-model", answer("42", checker_usage))
    store = Store()

    events, record = await collect(
        resume_team(
            squad(leader, scripted("writer-model"), checker),
            paused,
            _resume_request(paused),
            approved={
                ("checker", APPROVED_CALL): approval(APPROVED_CALL, "multiply", {"a": 6, "b": 7}, agent="checker"),
            },
            context=lambda run_id: run_context(run_id, SQUAD_APPROVED),
            persist=store,
        ),
    )

    # Agno stored the paused row without the member run on its delegation, which the conversion names; the call is
    # answered with the member's result where it held the pause notice, and the run keeps the metadata it paused with.
    assert paused.tools is not None
    [delegation] = paused.tools
    [member_run] = paused.member_responses
    assert delegation.child_run_id == member_run.run_id
    assert settled(record) == settled(
        replace(
            paused,
            status=RunStatus.completed,
            content="The checker found 42.",
            messages=[
                *(
                    replace(message, content="42") if message.tool_call_id == delegation.tool_call_id else message
                    for message in paused.messages
                ),
                reply("The checker found 42.", model="leader-model", metrics=leader_usage),
            ],
            tools=[replace(delegation, result="42")],
            member_responses=[],
            metrics=run_metrics(("leader-model", leader_usage), start=paused.metrics),
        ),
    )
    # The member runs the approved call and answers; its record keeps the decision on the call.
    [paused_member] = paused.member_responses
    call, waiting = waiting_call(paused_member)
    [member] = store.members
    assert settled(member) == settled(
        resumed(
            paused_member,
            decided=approved_call(waiting, PRODUCT),
            added=[result(call, PRODUCT)],
            answered=("42", checker_usage),
        ),
    )
    assert paused.to_dict() == before
    assert store.statuses == [("checker", "COMPLETED"), ("squad", "COMPLETED")]
    # The leader reads the member's result where it read the pause notice, and keeps its stored prompt first.
    [request] = leader.requests
    assert [message.role for message in request.messages] == ["system", "user", "assistant", "tool"]
    assert request.messages[-1].content == "42"
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


_MULTIPLIED = '{"operation": "multiplication", "result": 42.0}'


@pytest.mark.parametrize(
    ("before_the_pause", "after_the_call", "handed_over"),
    [
        pytest.param(None, "", _MULTIPLIED, id="stored-notice"),
        pytest.param(None, "Their product is 42.", "Their product is 42.", id="stored-notice-then-text"),
        pytest.param("Six and seven are both small.", "", "Six and seven are both small.", id="own-text"),
        pytest.param(
            "Six and seven are both small.",
            "Their product is 42.",
            "Six and seven are both small.\n\nTheir product is 42.",
            id="own-text-then-more",
        ),
    ],
)
async def test_a_resumed_member_hands_the_leader_everything_it_wrote_and_never_its_paused_notice(
    before_the_pause: str | None,
    after_the_call: str,
    handed_over: str,
) -> None:
    """A member whose resumed run ran its approved call hands over the text it wrote before and after the pause.

    The texts join a blank line apart, as the texts of one run's requests do. The paused notice the stored row held
    reads as no content, so a member that wrote nothing hands over its call's result instead.
    """
    paused = _paused_team_approved()
    if before_the_pause is not None:
        [paused_member] = paused.member_responses
        paused_member.content = before_the_pause
    leader = scripted("leader-model", answer("The checker found 42.", usage(1, 1)))
    checker = scripted("checker-model", answer(after_the_call, usage(1, 1)))

    await collect(
        resume_team(
            squad(leader, scripted("writer-model"), checker),
            paused,
            _resume_request(paused),
            approved={
                ("checker", APPROVED_CALL): approval(APPROVED_CALL, "multiply", {"a": 6, "b": 7}, agent="checker"),
            },
            context=lambda run_id: run_context(run_id, SQUAD_APPROVED),
        ),
    )

    [request] = leader.requests
    assert request.messages[-1].content == handed_over


async def test_a_member_that_pauses_again_keeps_the_team_paused_without_a_leader_request() -> None:
    paused = _paused_team_approved()
    leader = scripted("leader-model")  # Any request would fail: the scripted model has no turn for it.
    checker = scripted("checker-model", calls(tool_call("multiply", "call-again", a=2, b=3), metrics=usage(10, 1)))
    store = Store()

    _, record = await collect(
        resume_team(
            squad(leader, scripted("writer-model"), checker),
            paused,
            _resume_request(paused),
            approved={
                ("checker", APPROVED_CALL): approval(APPROVED_CALL, "multiply", {"a": 6, "b": 7}, agent="checker"),
            },
            context=lambda run_id: run_context(run_id, SQUAD_APPROVED),
            persist=store,
        ),
    )

    assert (record.status, leader.requests) == (RunStatus.paused, [])
    [member] = store.members
    assert member.status is RunStatus.paused
    assert record.member_responses == [member]
    [waiting] = [requirement for requirement in record.requirements if not requirement.is_resolved()]
    assert (waiting.tool_execution.tool_call_id, waiting.member_agent_id, waiting.member_run_id) == (
        "call-again",
        "checker",
        member.run_id,
    )
    assert (record.content, member.content) == (None, None)
    # The delegation still reports the pause: the member results reach the leader once every member is done.
    assert record.tools is not None
    assert record.tools[0].result == MEMBER_PAUSED_RESULT.format(name="Checker")
    assert store.statuses == [("checker", "PAUSED"), ("squad", "PAUSED")]


async def test_a_denied_member_call_hands_the_leader_the_member_s_answer_to_the_denial() -> None:
    paused = _paused_team_approved()
    leader = scripted("leader-model", answer("The checker did not multiply.", usage(1, 1)))
    checker = scripted("checker-model", answer("I did not multiply.", usage(1, 1)))
    store = Store()

    _, record = await collect(
        resume_team(
            squad(leader, scripted("writer-model"), checker),
            paused,
            _resume_request(paused),
            denied={("checker", APPROVED_CALL): "Alice declined."},
            context=lambda run_id: run_context(run_id, SQUAD_APPROVED),
            persist=store,
        ),
    )

    assert record.status is RunStatus.completed
    assert record.tools is not None
    assert record.tools[0].result == "I did not multiply."
    [member] = store.members
    [decided] = [tool for tool in member.tools or () if tool.tool_call_id == APPROVED_CALL]
    assert (decided.confirmed, decided.confirmation_note) == (False, "Alice declined.")
    [checker_request] = checker.requests
    assert checker_request.messages[-1].content == "Alice declined."


async def test_a_resumed_leader_reads_a_new_delegation_by_the_one_result_rule() -> None:
    # A member whose text is blank and that ran no tools hands over the no-response notice (``member_result_text``).
    paused = _paused_team_approved()
    leader = scripted(
        "leader-model",
        calls(tool_call("delegate_task_to_member", "call-w2", member_id="writer", task="Echo x."), metrics=usage(1, 1)),
        answer("Done.", usage(1, 1)),
    )
    writer = scripted(
        "writer-model",
        [TurnCompleted(message=Message(role="assistant", content=" "), stop_reason="end")],
    )
    checker = scripted("checker-model", answer("42", usage(1, 1)))

    _, record = await collect(
        resume_team(
            squad(leader, writer, checker, writer_toolkits=(toolkit(echo),)),
            paused,
            _resume_request(paused),
            approved={
                ("checker", APPROVED_CALL): approval(APPROVED_CALL, "multiply", {"a": 6, "b": 7}, agent="checker"),
            },
            context=lambda run_id: run_context(run_id, SQUAD_APPROVED),
        ),
    )

    assert record.status is RunStatus.completed
    delegated = leader.requests[1].messages[-1]
    assert (delegated.role, delegated.tool_call_id, delegated.content) == ("tool", "call-w2", NO_MEMBER_RESPONSE)


async def test_a_collaborate_resume_keeps_the_text_of_the_member_that_finished_before_the_pause() -> None:
    """The resumed member's result replaces only its own pause notice in the delegation result.

    A collaborate delegation returns every member's text joined; when one member pauses, its piece is its labeled
    pause notice, and the resume answers that piece with the member's result under the same label, so the text of
    a member that had completed earlier (the writer's here) still reaches the leader.
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
        start_team(team, prompt("hi"), ctx=run_context("r", SQUAD)),
    )
    assert paused.tools is not None
    assert paused.tools[0].result == ("Agent Writer: Six times seven is 42.\n\n" + CHECKER_PAUSED_PIECE)

    store = Store()
    _, record = await collect(
        resume_team(
            team,
            paused,
            _resume_request(paused),
            approved={("checker", "call-mul"): approval("call-mul", "multiply", {"a": 6, "b": 7}, agent="checker")},
            context=lambda run_id: run_context(run_id, SQUAD),
            persist=store,
        ),
    )

    assert (record.status, record.content) == (RunStatus.completed, "The panel says 42.")
    # The writer's text stays in the tool entry and in what the leader reads, beside the resumed checker's.
    resolved = "Agent Writer: Six times seven is 42.\n\nAgent Checker: 42"
    assert record.tools is not None
    assert record.tools[0].result == resolved
    assert leader.requests[-1].messages[-1].content == resolved
    # Only the paused member resumes; the writer is neither re-run nor re-read.
    assert [member.agent_id for member in store.members] == ["checker"]
    assert len(writer.requests) == 1


async def test_a_resume_keeps_the_members_that_ended_before_the_pause_with_the_resumed_one_in_its_place() -> None:
    """A team run's ended members carry into its resume, and the resumed member's record replaces its paused one.

    The live team run holds every finished member and continues a paused member's output in place, so a fold of
    the resume stores each member run once. A loaded record holds none of them; the stored row keeps only the paused.
    """
    leader = scripted(
        "leader-model",
        calls(
            tool_call("delegate_task_to_member", "call-w", member_id="writer", task="Draft it."),
            metrics=usage(10, 2),
        ),
        calls(
            tool_call("delegate_task_to_member", "call-c", member_id="checker", task="Multiply."),
            metrics=usage(10, 2),
        ),
        answer("Drafted and checked.", usage(10, 2)),
    )
    writer = scripted("writer-model", answer("Drafted."))
    checker = scripted(
        "checker-model",
        calls(tool_call("multiply", "call-mul", a=6, b=7), metrics=usage(5, 1)),
        answer("42", usage(5, 1)),
    )
    team = squad(leader, writer, checker)
    _, paused = await collect(
        start_team(team, prompt("hi"), ctx=run_context("r", SQUAD)),
    )
    ended = [(run.agent_id, run.run_id, run.status) for run in paused.ended_member_runs]
    assert [(agent, status) for agent, _, status in ended] == [
        ("writer", RunStatus.completed),
        ("checker", RunStatus.paused),
    ]
    assert TeamRunRecord.from_dict(paused.to_dict()).ended_member_runs == []

    _, record = await collect(
        resume_team(
            team,
            paused,
            _resume_request(paused),
            approved={("checker", "call-mul"): approval("call-mul", "multiply", {"a": 6, "b": 7}, agent="checker")},
            context=lambda run_id: run_context(run_id, SQUAD),
        ),
    )

    assert (record.status, record.content) == (RunStatus.completed, "Drafted and checked.")
    assert [(run.agent_id, run.run_id, run.status) for run in record.ended_member_runs] == [
        ended[0],
        ("checker", ended[1][1], RunStatus.completed),
    ]
    # The paused record is left as it was.
    assert [(run.agent_id, run.run_id, run.status) for run in paused.ended_member_runs] == ended


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
        start_team(team, prompt("hi"), ctx=run_context("r", SQUAD)),
    )
    assert paused.status is RunStatus.paused

    store = Store()
    events, record = await collect(
        resume_team(
            team,
            paused,
            _resume_request(paused),
            approved={(None, "call-echo"): approval("call-echo", "echo", {"text": "hello"}, agent="squad")},
            context=lambda run_id: run_context(run_id, SQUAD),
            persist=store,
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
    assert store.members == []
    assert leader.requests[-1].messages[-1].content == "hello"


async def test_a_resume_refuses_an_approved_leader_call_the_team_no_longer_offers() -> None:
    gated_team = squad(
        scripted("leader-model", calls(tool_call("echo", "call-echo", text="hello"))),
        scripted("writer-model"),
        scripted("checker-model"),
        leader_toolkits=(gated(echo, name="job_controls"),),
    )
    _, paused = await collect(
        start_team(gated_team, prompt("hi"), ctx=run_context("r", SQUAD)),
    )
    assert paused.status is RunStatus.paused
    before = paused.to_dict()
    leader = scripted("leader-model")
    ungated_team = squad(leader, scripted("writer-model"), scripted("checker-model"))

    with pytest.raises(ValueError, match="no longer offered"):
        resume_team(
            ungated_team,
            paused,
            _resume_request(paused),
            approved={(None, "call-echo"): approval("call-echo", "echo", {"text": "hello"}, agent="squad")},
            context=lambda run_id: run_context(run_id, SQUAD),
        )

    assert paused.to_dict() == before
    assert leader.requests == []


def calling_run(run_context: AgnoRunContext) -> str:
    """Return the id of the run that makes the call.

    Args:
        run_context: The run's context, which the loop injects.

    """
    return run_context.run_id


async def test_a_resumed_leader_s_calls_run_under_the_paused_team_run_s_id() -> None:
    leader = scripted("leader-model", calls(tool_call("calling_run", "call-run")), answer("Done."))
    team = squad(
        leader,
        scripted("writer-model"),
        scripted("checker-model"),
        leader_toolkits=(gated(calling_run, name="job_controls"),),
    )
    _, paused = await collect(
        start_team(team, prompt("hi"), ctx=run_context("r", SQUAD)),
    )
    assert paused.status is RunStatus.paused

    _, record = await collect(
        resume_team(
            team,
            paused,
            _resume_request(paused),
            approved={(None, "call-run"): approval("call-run", "calling_run", {}, agent="squad")},
            context=lambda run_id: run_context(run_id, SQUAD),
        ),
    )

    assert record.tools is not None
    assert (record.run_id, record.tools[0].result) == ("r", "r")


async def test_a_leader_level_approval_whose_call_stops_the_run_sends_the_leader_no_request() -> None:
    """An approved leader call that stops the run, such as a model switch, ends the resume as a fresh batch does."""
    leader = scripted("leader-model", calls(tool_call("echo", "call-echo", text="hello"), metrics=usage(10, 2)))
    stopping = toolkit(
        echo,
        name="job_controls",
        gate=ToolGate.POLICY,
        stop_after_call=True,
    )
    team = squad(leader, scripted("writer-model"), scripted("checker-model"), leader_toolkits=(stopping,))
    _, paused = await collect(
        start_team(team, prompt("hi"), ctx=run_context("r", SQUAD)),
    )
    assert paused.status is RunStatus.paused

    _, record = await collect(
        resume_team(
            team,
            paused,
            _resume_request(paused),
            approved={(None, "call-echo"): approval("call-echo", "echo", {"text": "hello"}, agent="squad")},
            context=lambda run_id: run_context(run_id, SQUAD),
        ),
    )

    assert record.status is RunStatus.completed
    assert record.tools is not None
    assert (record.tools[0].result, record.tools[0].stop_after_call) == ("hello", True)
    # Only the request that asked for the call; the resume sends none.
    assert len(leader.requests) == 1


@pytest.mark.parametrize(
    ("checker_text", "handed_over"),
    [("42", "42"), ("", "Helper says 42")],
    ids=["text", "no-text"],
)
async def test_an_external_result_resumes_the_member_that_waits_for_it(checker_text: str, handed_over: str) -> None:
    """The checker resumes with its delegation's result, and the leader reads what the checker hands over.

    A resumed member that writes no text hands over its tool results, as a member that never paused does.
    """

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
        answer(checker_text, usage(5, 1)),
    )
    team = squad(leader, scripted("writer-model"), checker, checker_toolkits=(external(run_subagent),))
    _, paused = await collect(
        start_team(team, prompt("hi"), ctx=run_context("r", SQUAD)),
    )

    store = Store()
    _, record = await collect(
        resume_team(
            team,
            paused,
            _resume_request(paused),
            external_results={("checker", "call-sub"): "Helper says 42"},
            context=lambda run_id: run_context(run_id, SQUAD),
            persist=store,
        ),
    )

    assert (record.status, record.content) == (RunStatus.completed, "The helper found 42.")
    assert record.tools is not None
    assert record.tools[0].result == handed_over
    [member] = store.members
    [answered] = [tool for tool in member.tools or () if tool.tool_call_id == "call-sub"]
    assert answered.result == "Helper says 42"
    assert checker.requests[-1].messages[-1].content == "Helper says 42"
    assert record.requirements == []


async def test_refusals_leave_the_paused_run_untouched_and_run_nothing() -> None:
    paused = _paused_team_approved()
    before = paused.to_dict()
    leader, writer, checker = scripted("leader-model"), scripted("writer-model"), scripted("checker-model")
    team = squad(leader, writer, checker)
    decision = approval(APPROVED_CALL, "multiply", {"a": 6, "b": 7}, agent="checker")

    def context(run_id: str) -> RunContext:
        return run_context(run_id, SQUAD_APPROVED)

    with pytest.raises(ValueError, match="Decisions do not cover exactly the calls"):
        resume_team(team, paused, _resume_request(paused), context=context)
    with pytest.raises(ValueError, match="name calls the team does not wait for"):
        resume_team(
            team,
            paused,
            _resume_request(paused),
            approved={
                ("checker", APPROVED_CALL): decision,
                (None, "call-unknown"): approval("call-unknown", "add", {"a": 1, "b": 2}, agent="squad"),
            },
            context=context,
        )
    # A decision on the right call id but the wrong member leaves the checker's call undecided.
    with pytest.raises(ValueError, match="Decisions do not cover exactly the calls"):
        resume_team(
            team,
            paused,
            _resume_request(paused),
            approved={("writer", APPROVED_CALL): decision},
            context=context,
        )
    with pytest.raises(ValueError, match="name calls the team does not wait for"):
        resume_team(
            team,
            paused,
            _resume_request(paused),
            approved={("checker", APPROVED_CALL): decision},
            denied={("writer", "call-elsewhere"): "no"},
            context=context,
        )
    without_checker = replace(team, members=team.members[:1])
    with pytest.raises(ValueError, match="not a member of team 'squad'"):
        resume_team(
            without_checker,
            paused,
            _resume_request(paused),
            approved={("checker", APPROVED_CALL): decision},
            context=context,
        )
    # A team run that no longer holds the member run waits on none of its calls.
    orphaned = replace(paused, member_responses=[])
    with pytest.raises(ValueError, match="name calls the team does not wait for"):
        resume_team(
            team,
            orphaned,
            _resume_request(paused),
            approved={("checker", APPROVED_CALL): decision},
            context=context,
        )
    completed = replace(paused, status=RunStatus.completed)
    with pytest.raises(ValueError, match="Only a paused run can be resumed"):
        resume_team(
            team,
            completed,
            _resume_request(paused),
            approved={("checker", APPROVED_CALL): decision},
            context=context,
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
        start_team(team, prompt("hi"), ctx=run_context("r", SQUAD)),
    )
    assert sorted((r.member_agent_id, r.tool_execution.tool_call_id) for r in paused.requirements) == [
        ("checker", "call-mul"),
        ("writer", "call-mul"),
    ]
    assert [member.agent_id for member in paused.member_responses] == ["writer", "checker"]

    store = Store()
    _, record = await collect(
        resume_team(
            team,
            paused,
            _resume_request(paused),
            approved={
                ("writer", "call-mul"): approval("call-mul", "multiply", {"a": 6, "b": 7}, agent="writer"),
                ("checker", "call-mul"): approval("call-mul", "multiply", {"a": 7, "b": 6}, agent="checker"),
            },
            context=lambda run_id: run_context(run_id, SQUAD),
            persist=store,
        ),
    )

    assert (record.status, record.content) == (RunStatus.completed, "Both say 42.")
    resumed = store.members
    assert [(member.agent_id, member.status) for member in resumed] == [
        ("writer", RunStatus.completed),
        ("checker", RunStatus.completed),
    ]
    # Each member ran its own approved call with its own arguments.
    assert writer.requests[-1].messages[-1].content == '{"operation": "multiplication", "result": 42.0}'
    assert checker.requests[-1].messages[-1].content == '{"operation": "multiplication", "result": 42.0}'
    assert record.tools is not None
    assert record.tools[0].result == "Agent Writer: Writer: 42\n\nAgent Checker: Checker: 42"
    assert leader.requests[-1].messages[-1].content == record.tools[0].result
    assert record.requirements == []


@pytest.mark.parametrize("both_pause", [True, False], ids=["both-paused", "one-paused"])
async def test_a_coordinate_resume_answers_each_waiting_delegation_with_its_member_s_result(
    both_pause: bool,
) -> None:
    """Two delegations of one leader response: each paused member resumes on its own call.

    Each delegation that waited on a member names the member's paused run and reads that member's result; one whose
    member completed before the pause keeps that member's own answer.
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
        start_team(team, prompt("hi"), ctx=run_context("r", SQUAD)),
    )
    assert paused.status is RunStatus.paused
    waiting = sorted((r.member_agent_id, r.tool_execution.tool_call_id) for r in paused.requirements)
    assert waiting == ([("checker", "call-mul"), ("writer", "call-mul")] if both_pause else [("checker", "call-mul")])
    assert paused.tools is not None
    paused_runs = {member.agent_id: member.run_id for member in paused.member_responses}
    assert [(tool.tool_call_id, tool.result, tool.child_run_id) for tool in paused.tools] == [
        (
            "call-w",
            MEMBER_PAUSED_RESULT.format(name="Writer") if both_pause else "Writer: 42",
            paused_runs.get("writer"),
        ),
        ("call-c", MEMBER_PAUSED_RESULT.format(name="Checker"), paused_runs["checker"]),
    ]
    approved = {("checker", "call-mul"): approval("call-mul", "multiply", {"a": 7, "b": 6}, agent="checker")}
    if both_pause:
        approved[("writer", "call-mul")] = approval("call-mul", "multiply", {"a": 6, "b": 7}, agent="writer")

    store = Store()
    _, record = await collect(
        resume_team(
            team,
            paused,
            _resume_request(paused),
            approved=approved,
            context=lambda run_id: run_context(run_id, SQUAD),
            persist=store,
        ),
    )

    assert (record.status, record.content) == (RunStatus.completed, "Both say 42.")
    assert [member.agent_id for member in store.members] == (["writer", "checker"] if both_pause else ["checker"])
    assert (len(writer.requests), len(checker.requests)) == ((2, 2) if both_pause else (1, 2))
    assert record.tools is not None
    assert {tool.tool_call_id: tool.result for tool in record.tools} == {
        "call-w": "Writer: 42",
        "call-c": "Checker: 42",
    }
    assert [message.content for message in leader.requests[-1].messages if message.role == "tool"] == [
        "Writer: 42",
        "Checker: 42",
    ]
    assert record.requirements == []


async def test_two_delegations_to_one_member_each_read_the_result_of_the_run_they_started() -> None:
    """Two coordinate calls each hand the checker a task, and each reads the result of the run it started.

    Both calls read the same pause notice, so only the member run each call names tells them apart: the run of the
    second call pauses first here, so it resumes first too.
    """
    leader = scripted(
        "leader-model",
        calls(
            tool_call("delegate_task_to_member", "call-a", member_id="checker", task="Multiply 6 by 7."),
            tool_call("delegate_task_to_member", "call-b", member_id="checker", task="Multiply 2 by 3."),
            metrics=usage(10, 2),
        ),
        answer("42 and 6.", usage(10, 2)),
    )
    second_paused = asyncio.Event()
    checker = scripted(
        "checker-model",
        [Await(second_paused.wait), *calls(tool_call("multiply", "mul-a", a=6, b=7), metrics=usage(5, 1))],
        calls(tool_call("multiply", "mul-b", a=2, b=3), metrics=usage(5, 1)),
        answer("First resumed.", usage(5, 1)),
        answer("Second resumed.", usage(5, 1)),
    )

    async def persist(record: AnyRunRecord) -> None:
        if record.status is RunStatus.paused:
            second_paused.set()

    team = squad(leader, scripted("writer-model"), checker)
    _, paused = await collect(
        start_team(
            team,
            prompt("hi"),
            ctx=run_context("r", SQUAD),
            persist=persist,
        ),
    )
    assert [r.tool_execution.tool_call_id for r in paused.requirements] == ["mul-b", "mul-a"]
    runs_by_call = {r.tool_execution.tool_call_id: r.member_run_id for r in paused.requirements}
    assert paused.tools is not None
    assert [(tool.tool_call_id, tool.child_run_id) for tool in paused.tools] == [
        ("call-a", runs_by_call["mul-a"]),
        ("call-b", runs_by_call["mul-b"]),
    ]

    _, record = await collect(
        resume_team(
            team,
            paused,
            _resume_request(paused),
            approved={
                ("checker", "mul-a"): approval("mul-a", "multiply", {"a": 6, "b": 7}, agent="checker"),
                ("checker", "mul-b"): approval("mul-b", "multiply", {"a": 2, "b": 3}, agent="checker"),
            },
            context=lambda run_id: run_context(run_id, SQUAD),
        ),
    )

    assert (record.status, record.content) == (RunStatus.completed, "42 and 6.")
    assert record.tools is not None
    assert [(tool.tool_call_id, tool.result) for tool in record.tools] == [
        ("call-a", "Second resumed."),
        ("call-b", "First resumed."),
    ]
    assert [message.content for message in leader.requests[-1].messages if message.role == "tool"] == [
        "Second resumed.",
        "First resumed.",
    ]


async def test_a_waiting_delegation_under_a_call_id_an_earlier_request_used_is_answered_on_its_own_entry() -> None:
    """A provider may reuse a call id in a later request: the waiting delegation is the latest entry under its id.

    The writer answers the first request's delegation; the second request's delegation reuses its id and waits on the
    checker, whose result answers only that delegation.
    """
    leader = scripted(
        "leader-model",
        calls(
            tool_call("delegate_task_to_member", "call-1", member_id="writer", task="Draft it."),
            metrics=usage(10, 2),
        ),
        calls(
            tool_call("delegate_task_to_member", "call-1", member_id="checker", task="Multiply 6 by 7."),
            metrics=usage(10, 2),
        ),
        answer("Drafted and checked.", usage(10, 2)),
    )
    writer = scripted("writer-model", answer("Drafted.", usage(5, 1)))
    checker = scripted(
        "checker-model",
        calls(tool_call("multiply", "call-mul", a=6, b=7), metrics=usage(5, 1)),
        answer("42", usage(5, 1)),
    )
    team = squad(leader, writer, checker)
    _, paused = await collect(
        start_team(team, prompt("hi"), ctx=run_context("r", SQUAD)),
    )
    assert paused.tools is not None
    assert [(tool.result, tool.child_run_id) for tool in paused.tools] == [
        ("Drafted.", None),
        (MEMBER_PAUSED_RESULT.format(name="Checker"), paused.member_responses[0].run_id),
    ]

    _, record = await collect(
        resume_team(
            team,
            paused,
            _resume_request(paused),
            approved={("checker", "call-mul"): approval("call-mul", "multiply", {"a": 6, "b": 7}, agent="checker")},
            context=lambda run_id: run_context(run_id, SQUAD),
        ),
    )

    assert (record.status, record.content) == (RunStatus.completed, "Drafted and checked.")
    assert record.tools is not None
    assert [tool.result for tool in record.tools] == ["Drafted.", "42"]
    assert [message.content for message in leader.requests[-1].messages if message.role == "tool"] == [
        "Drafted.",
        "42",
    ]


async def test_a_team_row_an_earlier_release_paused_with_its_member_call_copied_resumes_after_the_upgrade(
    tmp_path: Path,
) -> None:
    """v2026.10.752 and earlier stored a member's paused call on the member row and again on the team row.

    Layout version 8 keeps the call once, on the member row, which the paused team row waits on and resumes.
    """
    leader = scripted(
        "leader-model",
        calls(
            tool_call("delegate_task_to_member", "call-team", member_id="checker", task="Multiply 6 by 7."),
            metrics=usage(10, 2),
        ),
        answer("The checker found 42.", usage(10, 2)),
    )
    checker = scripted(
        "checker-model",
        calls(tool_call("multiply", "call-mul", a=6, b=7), metrics=usage(5, 1)),
        answer("42", usage(5, 1)),
    )
    team = squad(leader, scripted("writer-model"), checker)
    _, live = await collect(start_team(team, prompt("hi"), ctx=run_context("r", SQUAD)))
    [paused_member] = live.member_responses
    [call] = [tool.to_dict() for tool in paused_member.tools or ()]
    member_row = with_requirements(paused_member, stored_requirement(ToolExecution.from_dict(call), "req-1"))
    team_row = with_requirements(
        live,
        stored_requirement(
            ToolExecution.from_dict(call),
            "req-1",
            member_agent_id="checker",
            member_agent_name="Checker",
            member_run_id=paused_member.run_id,
        ),
    )
    store = create_state_storage(TableNames.of_team(_SQUAD_SCOPE).storage_name, tmp_path, _SQUAD_SCOPE)
    store_rows(store, SQUAD, [team_row, member_row])
    upgrade_from_layout(store, _SQUAD_SCOPE, 7)
    paused = store.get_run(live.run_id)
    assert isinstance(paused, TeamRunRecord)
    assert [(item.member_agent_id, item.tool_execution) for item in paused.requirements] == [
        ("checker", ToolExecution.from_dict(call)),
    ]
    members = Store()

    _, record = await collect(
        resume_team(
            team,
            paused,
            _resume_request(paused),
            approved={("checker", "call-mul"): approval("call-mul", "multiply", {"a": 6, "b": 7}, agent="checker")},
            context=lambda run_id: run_context(run_id, SQUAD),
            persist=members,
        ),
    )

    assert (record.status, record.content) == (RunStatus.completed, "The checker found 42.")
    assert [tool.result for tool in record.tools or ()] == ["42"]
    [member] = members.members
    [resumed_call] = member.tools or ()
    assert (resumed_call.result, resumed_call.confirmed) == (PRODUCT, True)


async def test_a_member_paused_again_on_a_reused_call_id_before_the_upgrade_resumes_after_it(tmp_path: Path) -> None:
    """An earlier release kept the member's resolved requirement, and the team row's copy of it, beside the next one.

    The member's provider reused the call id, so both name ``call-mul``; after layout version 8 the team still waits on
    the member's second call, whose approval resumes the member and then the leader.
    """
    leader = scripted(
        "leader-model",
        calls(
            tool_call("delegate_task_to_member", "call-team", member_id="checker", task="Multiply twice."),
            metrics=usage(10, 2),
        ),
        answer("The checker found 6.", usage(10, 2)),
    )
    checker = scripted(
        "checker-model",
        calls(tool_call("multiply", "call-mul", a=6, b=7), metrics=usage(5, 1)),
        calls(tool_call("multiply", "call-mul", a=2, b=3), metrics=usage(5, 1)),
        answer("6", usage(5, 1)),
    )
    team = squad(leader, scripted("writer-model"), checker)
    _, first = await collect(start_team(team, prompt("hi"), ctx=run_context("r", SQUAD)))
    _, live = await collect(
        resume_team(
            team,
            first,
            _resume_request(first),
            approved={("checker", "call-mul"): approval("call-mul", "multiply", {"a": 6, "b": 7}, agent="checker")},
            context=lambda run_id: run_context(run_id, SQUAD),
        ),
    )
    assert live.status is RunStatus.paused
    [paused_member] = live.member_responses
    approved, waiting = paused_member.tools or ()
    named = {"member_agent_id": "checker", "member_agent_name": "Checker", "member_run_id": paused_member.run_id}
    member_row = with_requirements(
        paused_member,
        stored_requirement(approved, "first-requirement", confirmation=True),
        stored_requirement(waiting, "second-requirement"),
    )
    team_row = with_requirements(
        live,
        stored_requirement(approved, "first-requirement", confirmation=True, **named),
        stored_requirement(waiting, "second-requirement", **named),
    )
    store = create_state_storage(TableNames.of_team(_SQUAD_SCOPE).storage_name, tmp_path, _SQUAD_SCOPE)
    store_rows(store, SQUAD, [team_row, member_row])
    upgrade_from_layout(store, _SQUAD_SCOPE, 7)
    paused = store.get_run(live.run_id)
    assert isinstance(paused, TeamRunRecord)
    [member] = paused.member_responses
    assert member.tools == [approved, waiting]

    _, record = await collect(
        resume_team(
            team,
            paused,
            _resume_request(paused),
            approved={("checker", "call-mul"): approval("call-mul", "multiply", {"a": 2, "b": 3}, agent="checker")},
            context=lambda run_id: run_context(run_id, SQUAD),
        ),
    )

    assert (record.status, record.content) == (RunStatus.completed, "The checker found 6.")
    assert [tool.result for tool in record.tools or ()] == ["6"]


@pytest.mark.parametrize(
    "linked",
    ["paused", "finished"],
    ids=["link-names-the-paused-run", "link-names-the-finished-run"],
)
@pytest.mark.parametrize("first", ["writer", "checker"], ids=["writer-then-checker", "checker-twice"])
async def test_a_row_a_streamed_agno_turn_paused_answers_each_call_with_its_own_member_s_result(
    first: str,
    linked: str,
    tmp_path: Path,
) -> None:
    """A streamed turn on the Agno runtime stored one member run on every coordinate call: the run that ended last.

    The first call's member finished and the checker paused on the second, and both calls name the same run, the
    paused one or the finished one. Once the upgrade rewrote the row and the call is approved, the first call keeps
    its answer and the second reads the checker's result, also when both calls went to the checker.
    """
    leader = scripted(
        "leader-model",
        calls(
            tool_call("delegate_task_to_member", "call-1", member_id=first, task="Draft it."),
            tool_call("delegate_task_to_member", "call-2", member_id="checker", task="Multiply 6 by 7."),
            metrics=usage(10, 2),
        ),
        answer("Drafted and checked.", usage(10, 2)),
    )
    pause = calls(tool_call("multiply", "call-mul", a=6, b=7), metrics=usage(5, 1))
    if first == "writer":
        writer = scripted("writer-model", answer("Drafted.", usage(5, 1)))
        checker = scripted("checker-model", pause, answer("42", usage(5, 1)))
    else:
        writer = scripted("writer-model")
        checker = scripted("checker-model", answer("Drafted.", usage(5, 1)), pause, answer("42", usage(5, 1)))
    team = squad(leader, writer, checker)
    _, live = await collect(
        start_team(team, prompt("hi"), ctx=run_context("r", SQUAD)),
    )
    [paused_run] = live.member_responses
    [finished_run] = [run for run in live.ended_member_runs if run.run_id != paused_run.run_id]
    stored = live.to_dict()
    for tool in stored["tools"]:
        tool["child_run_id"] = (paused_run if linked == "paused" else finished_run).run_id
    store = create_state_storage(TableNames.of_team(_SQUAD_SCOPE).storage_name, tmp_path, _SQUAD_SCOPE)
    store_rows(store, SQUAD, [stored, *(member.to_dict() for member in live.member_responses)])
    upgrade_from_layout_4(store, _SQUAD_SCOPE)
    paused = store.get_run(live.run_id)
    assert isinstance(paused, TeamRunRecord)

    _, record = await collect(
        resume_team(
            team,
            paused,
            _resume_request(paused),
            approved={("checker", "call-mul"): approval("call-mul", "multiply", {"a": 6, "b": 7}, agent="checker")},
            context=lambda run_id: run_context(run_id, SQUAD),
        ),
    )

    assert (record.status, record.content) == (RunStatus.completed, "Drafted and checked.")
    assert record.tools is not None
    assert [(tool.tool_call_id, tool.result) for tool in record.tools] == [("call-1", "Drafted."), ("call-2", "42")]
    assert [message.content for message in leader.requests[-1].messages if message.role == "tool"] == [
        "Drafted.",
        "42",
    ]


@pytest.mark.parametrize("mode", [TeamMode.COORDINATE, TeamMode.COLLABORATE])
async def test_a_member_renamed_while_its_approval_waits_still_answers_the_call_that_waited_on_it(
    mode: TeamMode,
) -> None:
    """The waiting call holds the pause notice under the name the member's run paused with, which a rename keeps.

    The checker is renamed between the pause and the approval; its call is answered all the same, and a collaborate
    piece keeps the label it paused under.
    """
    if mode is TeamMode.COORDINATE:
        delegation = tool_call("delegate_task_to_member", "call-c", member_id="checker", task="Multiply 6 by 7.")
    else:
        delegation = tool_call("delegate_task_to_members", "call-all", task="Multiply 6 by 7.")
    leader = scripted("leader-model", calls(delegation, metrics=usage(10, 2)), answer("Done.", usage(10, 2)))
    checker = scripted(
        "checker-model",
        calls(tool_call("multiply", "call-mul", a=6, b=7), metrics=usage(5, 1)),
        answer("42", usage(5, 1)),
    )
    team = squad(leader, scripted("writer-model", answer("Six times seven is 42.")), checker, mode=mode)
    _, paused = await collect(
        start_team(team, prompt("hi"), ctx=run_context("r", SQUAD)),
    )
    writer_member, checker_member = team.members
    inspector = replace(checker_member, name="Inspector")

    _, record = await collect(
        resume_team(
            replace(team, members=(writer_member, inspector)),
            paused,
            _resume_request(paused),
            approved={("checker", "call-mul"): approval("call-mul", "multiply", {"a": 6, "b": 7}, agent="checker")},
            context=lambda run_id: run_context(run_id, SQUAD),
        ),
    )

    assert record.status is RunStatus.completed
    answered = "42" if mode is TeamMode.COORDINATE else "Agent Writer: Six times seven is 42.\n\nAgent Checker: 42"
    assert leader.requests[-1].messages[-1].content == answered


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
        start_team(team, prompt("hi"), ctx=run_context("r", SQUAD)),
    )
    assert [(r.member_agent_id, r.tool_execution.tool_call_id) for r in paused.requirements] == [("checker", "call-w")]
    assert paused.tools is not None
    assert [(tool.tool_call_id, tool.result) for tool in paused.tools] == [
        ("call-w", "Writer: 42"),
        ("call-c", MEMBER_PAUSED_RESULT.format(name="Checker")),
    ]

    store = Store()
    _, record = await collect(
        resume_team(
            team,
            paused,
            _resume_request(paused),
            approved={("checker", "call-w"): approval("call-w", "multiply", {"a": 7, "b": 6}, agent="checker")},
            context=lambda run_id: run_context(run_id, SQUAD),
            persist=store,
        ),
    )

    assert (record.status, record.content) == (RunStatus.completed, "Both say 42.")
    [member] = store.members
    assert (member.agent_id, member.status) == ("checker", RunStatus.completed)
    assert checker.requests[-1].messages[-1].content == '{"operation": "multiplication", "result": 42.0}'
    assert record.tools is not None
    assert [(tool.tool_call_id, tool.tool_name, tool.result) for tool in record.tools] == [
        ("call-w", "delegate_task_to_member", "Writer: 42"),
        ("call-c", "delegate_task_to_member", "Checker: 42"),
    ]
    assert [message.content for message in leader.requests[-1].messages if message.role == "tool"] == [
        "Writer: 42",
        "Checker: 42",
    ]
    assert record.requirements == []


@pytest.mark.parametrize("mode", [TeamMode.COORDINATE, TeamMode.COLLABORATE])
async def test_a_finished_member_whose_answer_mentions_human_input_keeps_its_answer_on_resume(mode: TeamMode) -> None:
    """Only the delegation that waited on the paused member reads its result; a finished member keeps its own answer.

    The writer finishes with text that mentions human input, the checker pauses on its gated call; once approved,
    a coordinate leader still reads the writer's answer on its own call, and a collaborate call, which waited on the
    checker, keeps the writer's piece beside the checker's result.
    """
    writer_text = "Step 3 requires human input from the operator; steps 1-2 are fine."
    if mode is TeamMode.COORDINATE:
        delegations = calls(
            tool_call("delegate_task_to_member", "call-w", member_id="writer", task="Review the steps."),
            tool_call("delegate_task_to_member", "call-c", member_id="checker", task="Multiply 6 by 7."),
            metrics=usage(10, 2),
        )
    else:
        delegations = calls(tool_call("delegate_task_to_members", "call-all", task="Multiply 6 by 7."))
    leader = scripted("leader-model", delegations, answer("Done.", usage(10, 2)))
    writer = scripted("writer-model", answer(writer_text, usage(5, 1)))
    checker = scripted(
        "checker-model",
        calls(tool_call("multiply", "call-mul", a=6, b=7), metrics=usage(5, 1)),
        answer("Checker: 42", usage(5, 1)),
    )
    team = squad(leader, writer, checker, mode=mode)
    _, paused = await collect(
        start_team(team, prompt("hi"), ctx=run_context("r", SQUAD)),
    )
    assert paused.status is RunStatus.paused

    _, record = await collect(
        resume_team(
            team,
            paused,
            _resume_request(paused),
            approved={("checker", "call-mul"): approval("call-mul", "multiply", {"a": 6, "b": 7}, agent="checker")},
            context=lambda run_id: run_context(run_id, SQUAD),
        ),
    )

    expected = (
        [writer_text, "Checker: 42"]
        if mode is TeamMode.COORDINATE
        else [f"Agent Writer: {writer_text}\n\nAgent Checker: Checker: 42"]
    )
    assert record.status is RunStatus.completed
    assert [message.content for message in leader.requests[-1].messages if message.role == "tool"] == expected


async def test_a_resume_with_member_and_leader_decisions_runs_both_and_hands_the_leader_the_member_s_result() -> None:
    """A mixed resume answers the delegation with the member's result and runs the leader's own approved call.

    The delegation's entry holds the member's result instead of the pause notice, so the leader reads what its member
    did.
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
        start_team(team, prompt("hi"), ctx=run_context("r", SQUAD)),
    )
    assert {(r.member_agent_id, r.tool_execution.tool_call_id) for r in paused.requirements} == {
        (None, "call-echo"),
        ("checker", "call-mul"),
    }

    _, record = await collect(
        resume_team(
            team,
            paused,
            _resume_request(paused),
            approved={
                ("checker", "call-mul"): approval("call-mul", "multiply", {"a": 6, "b": 7}, agent="checker"),
                (None, "call-echo"): approval("call-echo", "echo", {"text": "hello"}, agent="squad"),
            },
            context=lambda run_id: run_context(run_id, SQUAD),
        ),
    )

    assert (record.status, record.content) == (RunStatus.completed, "42 and hello.")
    assert record.tools is not None
    results = {tool.tool_name: tool.result for tool in record.tools}
    assert results == {
        "delegate_task_to_member": "42",
        "echo": "hello",
    }
    assert [message.content for message in leader.requests[-1].messages if message.role == "tool"] == [
        results["delegate_task_to_member"],
        "hello",
    ]


async def test_a_member_whose_resumed_run_fails_reaches_the_leader_as_its_redacted_error() -> None:
    """The leader reads why the resumed member failed, credentials redacted, not the member's paused notice."""
    paused = _paused_team_approved()

    async def lose_the_member_provider() -> None:
        msg = "checker provider down: api_key=example-secret"
        raise ProviderError(msg, provider=PROVIDER)

    leader = scripted("leader-model", answer("The checker could not finish.", usage(1, 1)))
    store = Store()

    await collect(
        resume_team(
            squad(leader, scripted("writer-model"), scripted("checker-model", [Await(lose_the_member_provider)])),
            paused,
            _resume_request(paused),
            approved={
                ("checker", APPROVED_CALL): approval(APPROVED_CALL, "multiply", {"a": 6, "b": 7}, agent="checker"),
            },
            context=lambda run_id: run_context(run_id, SQUAD_APPROVED),
            persist=store,
        ),
    )

    assert store.statuses == [("checker", "ERROR"), ("squad", "COMPLETED")]
    member_result = leader.requests[-1].messages[-1].content
    assert isinstance(member_result, str)
    assert member_result.startswith("checker provider down: api_key=")
    assert "example-secret" not in member_result


async def test_a_failed_resume_stores_its_error_over_the_paused_team_row() -> None:
    """A leader request that fails after the member resumed ends the team run ``ERROR`` over its ``PAUSED`` row.

    The member's resumed row is stored completed first, so the approved call is not offered again.
    """
    paused = _paused_team_approved()

    async def lose_the_provider() -> None:
        msg = "provider connection lost"
        raise ProviderError(msg, provider=PROVIDER)

    store = Store()
    events = resume_team(
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
        context=lambda run_id: run_context(run_id, SQUAD_APPROVED),
        persist=store,
    )

    with pytest.raises(ProviderError, match="provider connection lost"):
        await collect(events)

    assert store.statuses == [("checker", "COMPLETED"), ("squad", "ERROR")]
    member_row, team_row = store.records
    assert member_row["run_id"] == paused.member_responses[0].run_id
    assert team_row["run_id"] == "team-approved"
    assert "provider connection lost" in team_row["error"]
    assert "content" not in team_row
    assert "requirements" not in team_row


async def test_cancelling_a_resume_stores_cancelled_rows_over_the_paused_ones() -> None:
    """A Stop during a member's resume ends the member and the team ``CANCELLED``; the approval is not resumable again.

    The envelope writes its interrupted record over the team row afterwards.
    """
    paused = _paused_team_approved()
    resuming = asyncio.Event()

    async def wait_to_be_cancelled() -> None:
        resuming.set()
        await asyncio.Event().wait()

    checker = scripted("checker-model", [TextDelta("Dra"), Await(wait_to_be_cancelled)])
    store = Store()

    async def consume() -> None:
        events = resume_team(
            squad(scripted("leader-model"), scripted("writer-model"), checker),
            paused,
            _resume_request(paused),
            approved={
                ("checker", APPROVED_CALL): approval(APPROVED_CALL, "multiply", {"a": 6, "b": 7}, agent="checker"),
            },
            context=lambda run_id: run_context(run_id, SQUAD_APPROVED),
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
    assert "requirements" not in team_row
