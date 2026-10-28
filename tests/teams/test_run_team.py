"""Team runs store one record for the team and one for each member run, and tell the leader what each member returned.

The stored records are asserted as typed values (``tests.run_record_helpers``): a coordinate run whose member used a
tool, a member run that failed, a member call that waits for approval, and a streamed collaborate run.
"""
# ruff: noqa: D103

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any, Protocol

import pytest
from structlog.testing import capture_logs

from mindroom.llm.errors import ProviderError
from mindroom.llm.events import RunFinished, ToolCompleted, ToolStarted
from mindroom.llm.messages import Message
from mindroom.llm.provider import TextDelta, TurnCompleted, UsageReported
from mindroom.llm.runs import RunMetrics, RunRecord, RunRequirement, RunStatus, TeamRunRecord
from mindroom.teams.delegate_tools import MEMBER_PAUSED_RESULT, NO_MEMBER_RESPONSE
from mindroom.teams.prompt import TEAM_MEMBER_NOT_FOUND_TEMPLATE, render_members_roster
from mindroom.teams.runtime import MemberEvent, leader_tool_specs, team_usage
from mindroom.teams.spec import TeamMode
from tests.agent_loop.helpers import add, echo, external, gated, toolkit
from tests.fakes.scripted_model import Await, answer, calls, tool_call, usage
from tests.providers.gemini_ollama_support import build_adapter, ollama_ndjson
from tests.providers.golden_support import Wire, hide_provider_environment, streamed_reply
from tests.providers.openai_family_support import openai_family_adapter
from tests.run_record_helpers import PROVIDER, execution, reply, result, run_metrics, settled
from tests.teams.squad import (
    CHECKER_PAUSED_PIECE,
    PANEL,
    REQUESTER,
    SQUAD,
    Store,
    collect,
    prompt,
    run_context,
    scripted,
    squad,
    start_team,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    import httpx

    from mindroom.llm.usage import MessageMetrics
    from mindroom.teams.runtime import TeamRunEvent

pytestmark = pytest.mark.asyncio


def _delegate(call_id: str, member_id: str, task: str, *, metrics: MessageMetrics) -> list[Any]:
    return calls(_delegation(call_id, member_id, task), metrics=metrics)


def _delegation(call_id: str, member_id: str, task: str) -> dict[str, Any]:
    return tool_call("delegate_task_to_member", call_id, member_id=member_id, task=task)


# The run metadata the turn starts with, which the team's record stores and its member records do not.
_METADATA = {"room_id": "!room:example.org", "thread_id": "$squad", "requester_id": REQUESTER}


_OLLAMA_MEMBER = {"provider": "ollama", "id": "qwen3.8:27b", "host": "http://ollama.test:11434"}
_LLAMA_CPP_MEMBER = {"provider": "llama_cpp", "id": "unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_XL"}


async def _overloaded() -> None:
    msg = "Checker overloaded"
    raise ProviderError(msg, provider="Synthetic", status_code=503)


def _silent() -> list[Any]:
    """A turn that ends with no text at all, not even an empty string."""
    return [TurnCompleted(message=Message(role="assistant", content=None), stop_reason="end")]


def _blank() -> list[Any]:
    """A turn whose text is an empty string rather than no text."""
    return [TurnCompleted(message=Message(role="assistant", content=""), stop_reason="end")]


def _member_events(events: list[Any], member_id: str) -> list[Any]:
    return [event.event for event in events if isinstance(event, MemberEvent) and event.member_id == member_id]


def _delegation_to_both(mode: TeamMode) -> list[Any]:
    """Return the leader's first turn: the same sub-task to each member, by the mode's delegation tool."""
    if mode is TeamMode.COLLABORATE:
        return calls(tool_call("delegate_task_to_members", "call-all", task="Add 2 and 3."), metrics=usage(10, 2))
    return calls(
        tool_call("delegate_task_to_member", "call-w", member_id="writer", task="Add 2 and 3."),
        tool_call("delegate_task_to_member", "call-c", member_id="checker", task="Add 2 and 3."),
        metrics=usage(10, 2),
    )


async def test_a_coordinate_run_with_a_member_tool_call_stores_the_team_and_member_records() -> None:
    delegation = _delegation("call-team-writer", "writer", "Add 2 and 3")
    addition = tool_call("add", "call-writer-add", a=2, b=3)
    sum_result = '{"operation": "addition", "result": 5.0}'
    leader_usage = (usage(400, 12), usage(450, 9, time_to_first_token=0.25))
    writer_usage = (usage(80, 4), usage(95, 1, time_to_first_token=0.25))
    leader = scripted(
        "leader-model",
        calls(delegation, metrics=leader_usage[0]),
        answer("The writer found 5.", leader_usage[1]),
    )
    writer = scripted("writer-model", calls(addition, metrics=writer_usage[0]), answer("5", writer_usage[1]))
    checker = scripted("checker-model")
    ctx = run_context("team-completed", SQUAD, _METADATA)
    question = prompt("What is 2 + 3?")
    store = Store()
    stored_when_the_delegation_completed: list[list[tuple[str, str]]] = []

    async def noting_the_store(stream: AsyncIterator[TeamRunEvent]) -> AsyncIterator[TeamRunEvent]:
        async for event in stream:
            if isinstance(event, ToolCompleted):
                stored_when_the_delegation_completed.append(store.statuses)
            yield event

    events, record = await collect(
        noting_the_store(start_team(squad(leader, writer, checker), question, ctx=ctx, persist=store)),
    )

    [member] = store.members
    assert settled(record) == settled(
        TeamRunRecord(
            run_id="team-completed",
            team_id="squad",
            session_id=SQUAD,
            user_id=REQUESTER,
            status=RunStatus.completed,
            content="The writer found 5.",
            model="leader-model",
            model_provider=PROVIDER,
            messages=[
                Message(role="system", content="You lead the squad."),
                *question,
                reply(None, model="leader-model", metrics=leader_usage[0], calls=[delegation]),
                result(delegation, "5"),
                reply("The writer found 5.", model="leader-model", metrics=leader_usage[1]),
            ],
            tools=[execution(delegation, is_error=False, result="5")],
            metrics=run_metrics(*(("leader-model", step) for step in leader_usage)),
            metadata=_METADATA,
        ),
    )
    assert settled(member) == settled(
        RunRecord(
            run_id=member.run_id,
            agent_id="writer",
            agent_display_name="Writer",
            session_id=SQUAD,
            parent_run_id="team-completed",
            user_id=REQUESTER,
            status=RunStatus.completed,
            content="5",
            model="writer-model",
            model_provider=PROVIDER,
            messages=[
                Message(role="system", content="You are Writer."),
                Message(role="user", content="Add 2 and 3"),
                reply(None, model="writer-model", metrics=writer_usage[0], calls=[addition]),
                result(addition, sum_result),
                reply("5", model="writer-model", metrics=writer_usage[1]),
            ],
            tools=[execution(addition, is_error=False, result=sum_result)],
            metrics=run_metrics(*(("writer-model", step) for step in writer_usage)),
        ),
    )
    # The team's metadata stays on the team's record; the member's record stores none of it.
    assert record.metadata is ctx.metadata
    assert member.metadata is None
    assert store.records == [member.to_dict(), record.to_dict()]
    assert checker.requests == []
    # The member's input is the leader's task alone: no history, no conversation.
    [first, *_] = writer.requests
    assert [(message.role, message.content) for message in first.messages] == [
        ("system", "You are Writer."),
        ("user", "Add 2 and 3"),
    ]
    # Member events are tagged with the member and its run; the leader's own tool events are not wrapped.
    writer_events = _member_events(events, "writer")
    assert [type(event).__name__ for event in writer_events] == ["ToolStarted", "ToolCompleted", "TextDelta"]
    assert all(event.run_id == member.run_id for event in events if isinstance(event, MemberEvent))
    completed = next(event for event in writer_events if isinstance(event, ToolCompleted))
    assert completed.call.tool_name == "add"
    leader_tools = [event.call.tool_name for event in events if isinstance(event, ToolStarted | ToolCompleted)]
    assert leader_tools == ["delegate_task_to_member", "delegate_task_to_member"]
    # The member's terminal record is stored before the leader's delegation completes, and the team ends last.
    assert stored_when_the_delegation_completed == [[("writer", "COMPLETED")]]
    assert isinstance(events[-1], RunFinished)


class _RunMetadata(Protocol):
    """The part of the run context a tool receives that ``room_of_the_turn`` reads."""

    metadata: dict[str, Any] | None


def room_of_the_turn(run_context: _RunMetadata) -> str:
    """Return the room the turn's run metadata names.

    Args:
        run_context: The run's context, which the loop injects.

    """
    return str((run_context.metadata or {}).get("room_id"))


async def test_a_member_s_tools_read_the_team_s_metadata_which_only_the_team_s_record_stores() -> None:
    leader = scripted(
        "leader-model",
        _delegate("call-team-writer", "writer", "Name the room", metrics=usage(10, 1)),
        answer("The writer named it.", usage(10, 1)),
    )
    writer = scripted("writer-model", calls(tool_call("room_of_the_turn", "call-room")), answer("Named."))
    team = squad(leader, writer, scripted("checker-model"), writer_toolkits=[toolkit(room_of_the_turn)])
    ctx = run_context("team-run", SQUAD, {"room_id": "!room:example.org"})
    store = Store()

    _, record = await collect(start_team(team, [Message(role="user", content="Which room?")], ctx=ctx, persist=store))

    [member] = store.members
    assert [tool.result for tool in member.tools or ()] == ["!room:example.org"]
    assert member.metadata is None
    assert record.metadata == {"room_id": "!room:example.org"}


async def test_a_failing_member_reaches_the_leader_as_its_error_text_and_stores_an_error_record() -> None:
    delegation = _delegation("call-team-checker", "checker", "Check that 2 + 3 = 5")
    leader_usage = (usage(420, 12), usage(470, 8, time_to_first_token=0.25))
    leader = scripted(
        "leader-model",
        calls(delegation, metrics=leader_usage[0]),
        answer("The checker could not answer.", leader_usage[1]),
    )
    checker = scripted("checker-model", [Await(_overloaded)])
    ctx = run_context("team-member-error", SQUAD, _METADATA)
    question = prompt("Check the answer.")
    store = Store()

    with capture_logs() as logs:
        _, record = await collect(
            start_team(squad(leader, scripted("writer-model"), checker), question, ctx=ctx, persist=store),
        )

    [member] = store.members
    # The member's failure answers the delegation as a result, not as a failed call.
    assert settled(record) == settled(
        TeamRunRecord(
            run_id="team-member-error",
            team_id="squad",
            session_id=SQUAD,
            user_id=REQUESTER,
            status=RunStatus.completed,
            content="The checker could not answer.",
            model="leader-model",
            model_provider=PROVIDER,
            messages=[
                Message(role="system", content="You lead the squad."),
                *question,
                reply(None, model="leader-model", metrics=leader_usage[0], calls=[delegation]),
                result(delegation, "Checker overloaded"),
                reply("The checker could not answer.", model="leader-model", metrics=leader_usage[1]),
            ],
            tools=[execution(delegation, is_error=False, result="Checker overloaded")],
            metrics=run_metrics(*(("leader-model", step) for step in leader_usage)),
            metadata=_METADATA,
        ),
    )
    # The failed member run keeps its task and stores the error's text as its error, with no content or usage.
    assert settled(member) == settled(
        RunRecord(
            run_id=member.run_id,
            agent_id="checker",
            agent_display_name="Checker",
            session_id=SQUAD,
            parent_run_id="team-member-error",
            user_id=REQUESTER,
            status=RunStatus.error,
            error="Checker overloaded",
            model="checker-model",
            model_provider=PROVIDER,
            messages=[
                Message(role="system", content="You are Checker."),
                Message(role="user", content="Check that 2 + 3 = 5"),
            ],
            metrics=RunMetrics(),
        ),
    )
    assert store.statuses == [("checker", "ERROR"), ("squad", "COMPLETED")]
    failures = [log for log in logs if log["event"] == "team_member_run_failed"]
    assert [(log["team_id"], log["member_id"]) for log in failures] == [("squad", "checker")]


async def test_a_member_approval_pauses_the_team_with_the_member_run_embedded() -> None:
    delegation = _delegation("call-team-multiply", "checker", "Multiply 6 by 7")
    multiplication = tool_call("multiply", "call-checker-multiply", a=6, b=7)
    leader = scripted("leader-model", calls(delegation, metrics=usage(430, 12)))
    checker = scripted("checker-model", calls(multiplication, metrics=usage(85, 5)))
    team = squad(leader, scripted("writer-model"), checker)
    question = prompt("Multiply 6 by 7.")
    store = Store()

    _, record = await collect(
        start_team(team, question, ctx=run_context("team-paused", SQUAD, _METADATA), persist=store),
    )

    [member] = store.members
    waiting = execution(multiplication, requires_confirmation=True, approval_type="mindroom_policy")
    paused_member = RunRecord(
        run_id=member.run_id,
        agent_id="checker",
        agent_display_name="Checker",
        session_id=SQUAD,
        parent_run_id="team-paused",
        user_id=REQUESTER,
        status=RunStatus.paused,
        model="checker-model",
        model_provider=PROVIDER,
        messages=[
            Message(role="system", content="You are Checker."),
            Message(role="user", content="Multiply 6 by 7"),
            reply(None, model="checker-model", metrics=usage(85, 5), calls=[multiplication]),
        ],
        tools=[waiting],
        requirements=[RunRequirement(waiting)],
        metrics=run_metrics(("checker-model", usage(85, 5))),
    )
    paused_notice = MEMBER_PAUSED_RESULT.format(name="Checker")
    assert settled(member) == settled(paused_member)
    assert settled(record) == settled(
        TeamRunRecord(
            run_id="team-paused",
            team_id="squad",
            session_id=SQUAD,
            user_id=REQUESTER,
            status=RunStatus.paused,
            model="leader-model",
            model_provider=PROVIDER,
            messages=[
                Message(role="system", content="You lead the squad."),
                *question,
                reply(None, model="leader-model", metrics=usage(430, 12), calls=[delegation]),
                result(delegation, paused_notice),
            ],
            # The delegation names the member run it waits on.
            tools=[execution(delegation, is_error=False, result=paused_notice, child_run_id=member.run_id)],
            requirements=[
                RunRequirement(
                    waiting,
                    member_agent_id="checker",
                    member_display_name="Checker",
                    member_run_id=member.run_id,
                ),
            ],
            metrics=run_metrics(("leader-model", usage(430, 12))),
            metadata=_METADATA,
            member_responses=[paused_member],
        ),
    )
    assert record.member_responses == [member]
    assert record.tools is not None
    assert record.tools[0].child_run_id == member.run_id
    assert record.requirements is not None
    [requirement] = record.requirements
    # The approval card keys the call by member id and call id, both as the member run recorded them, and the team's
    # requirement is the member's own.
    assert f"{requirement.member_agent_id}:{requirement.tool_execution.tool_call_id}" == "checker:call-checker-multiply"
    assert (requirement.member_display_name, requirement.member_run_id) == ("Checker", member.run_id)
    assert [item.id for item in member.requirements or ()] == [requirement.id]
    assert len(leader.requests) == 1  # The leader is not asked again while the member waits.
    assert store.statuses == [("checker", "PAUSED"), ("squad", "PAUSED")]


async def test_a_collaborate_run_stores_labeled_member_results_in_member_order() -> None:
    task = "Give one fact about the number 5."
    delegation = tool_call("delegate_task_to_members", "call-panel", task=task)
    labeled = "Agent Writer: Five is prime.\n\nAgent Checker: Five is odd."
    leader_usage = (usage(380, 10), usage(410, 7, time_to_first_token=0.25))
    answers = {
        "writer": ("Five is prime.", usage(60, 3, time_to_first_token=0.25)),
        "checker": ("Five is odd.", usage(61, 3, time_to_first_token=0.25)),
    }
    leader = scripted(
        "leader-model",
        calls(delegation, metrics=leader_usage[0]),
        answer("Five is prime and odd.", leader_usage[1]),
    )
    writer = scripted("writer-model", answer(*answers["writer"]))
    checker = scripted("checker-model", answer(*answers["checker"]))
    team = squad(leader, writer, checker, mode=TeamMode.COLLABORATE, team_id="panel")
    question = prompt("Tell me about 5.")
    metadata = {**_METADATA, "thread_id": "$panel"}
    store = Store()

    events, record = await collect(
        start_team(team, question, ctx=run_context("panel-completed", PANEL, metadata), persist=store),
    )

    assert settled(record) == settled(
        TeamRunRecord(
            run_id="panel-completed",
            team_id="panel",
            session_id=PANEL,
            user_id=REQUESTER,
            status=RunStatus.completed,
            content="Five is prime and odd.",
            model="leader-model",
            model_provider=PROVIDER,
            messages=[
                Message(role="system", content="You lead the squad."),
                *question,
                reply(None, model="leader-model", metrics=leader_usage[0], calls=[delegation]),
                result(delegation, labeled),
                reply("Five is prime and odd.", model="leader-model", metrics=leader_usage[1]),
            ],
            tools=[execution(delegation, is_error=False, result=labeled)],
            metrics=run_metrics(*(("leader-model", step) for step in leader_usage)),
            metadata=metadata,
        ),
    )
    members = store.members
    assert [settled(member) for member in members] == [
        settled(
            RunRecord(
                run_id=member.run_id,
                agent_id=agent_id,
                agent_display_name=agent_id.title(),
                session_id=PANEL,
                parent_run_id="panel-completed",
                user_id=REQUESTER,
                status=RunStatus.completed,
                content=text,
                model=f"{agent_id}-model",
                model_provider=PROVIDER,
                messages=[
                    Message(role="system", content=f"You are {agent_id.title()}."),
                    Message(role="user", content=task),
                    reply(text, model=f"{agent_id}-model", metrics=metrics),
                ],
                metrics=run_metrics((f"{agent_id}-model", metrics)),
            ),
        )
        for member, (agent_id, (text, metrics)) in zip(members, answers.items(), strict=True)
    ]
    streamed = {
        member_id: "".join(
            event.event.text
            for event in events
            if isinstance(event, MemberEvent) and event.member_id == member_id and isinstance(event.event, TextDelta)
        )
        for member_id in ("writer", "checker")
    }
    assert streamed == {"writer": "Five is prime.", "checker": "Five is odd."}
    assert store.statuses == [("writer", "COMPLETED"), ("checker", "COMPLETED"), ("panel", "COMPLETED")]
    # The turn's usage is the leader's plus each member's, counted once, within the leader's duration.
    total = team_usage(record)
    assert total is not None
    assert (total.tokens.input_tokens, total.tokens.output_tokens, total.tokens.total_tokens) == (911, 23, 934)
    assert total.duration == record.metrics.duration  # type: ignore[union-attr]
    assert total.details is not None
    assert sorted((entry.id, entry.tokens.input_tokens) for entry in total.details["model"]) == [
        ("checker-model", 61),
        ("leader-model", 790),
        ("writer-model", 60),
    ]


async def test_an_unknown_member_id_answers_the_leader_with_the_not_found_notice_and_roster() -> None:
    leader = scripted(
        "leader-model",
        _delegate("call-nobody", "nobody", "Do it", metrics=usage(10, 2)),
        answer("There is no such member.", usage(20, 3)),
    )
    writer, checker = scripted("writer-model"), scripted("checker-model")
    team = squad(leader, writer, checker)
    store = Store()

    _, record = await collect(
        start_team(
            team,
            prompt("hi"),
            ctx=run_context("r", SQUAD),
            persist=store,
        ),
    )

    assert record.tools is not None
    assert record.tools[0].result == TEAM_MEMBER_NOT_FOUND_TEMPLATE.format(
        member_id="nobody",
        roster=render_members_roster(team.members),
    )
    assert render_members_roster(team.members) == (
        '<member id="writer" name="Writer">\n  Role: Writer role.\n</member>\n'
        '<member id="checker" name="Checker">\n  Role: Checker role.\n</member>\n'
    )
    assert (writer.requests, checker.requests, store.members) == ([], [], [])
    assert record.status is RunStatus.completed


async def test_a_collaborate_member_that_pauses_adds_its_labeled_notice_in_its_place_and_pauses_the_team() -> None:
    leader = scripted(
        "leader-model",
        calls(tool_call("delegate_task_to_members", "call-all", task="Multiply 6 by 7."), metrics=usage(10, 2)),
    )
    writer = scripted("writer-model", answer("Six times seven is 42."))
    checker = scripted("checker-model", calls(tool_call("multiply", "call-mul", a=6, b=7), metrics=usage(5, 1)))
    team = squad(leader, writer, checker, mode=TeamMode.COLLABORATE)
    store = Store()

    _, record = await collect(
        start_team(
            team,
            prompt("hi"),
            ctx=run_context("r", SQUAD),
            persist=store,
        ),
    )

    assert record.status is RunStatus.paused
    assert record.tools is not None
    assert record.tools[0].result == ("Agent Writer: Six times seven is 42.\n\n" + CHECKER_PAUSED_PIECE)
    writer_record, checker_record = store.members
    assert (writer_record.status, checker_record.status) == (RunStatus.completed, RunStatus.paused)
    assert record.member_responses == [checker_record]  # Only the paused member rides along.
    assert record.requirements is not None
    [requirement] = record.requirements
    assert (requirement.member_agent_id, requirement.member_run_id) == ("checker", checker_record.run_id)
    assert record.content is None  # The pause is the team run's requirements; no notice stands in for text.


@pytest.mark.parametrize("mode", list(TeamMode))
async def test_the_leader_tool_definitions_are_the_tools_each_leader_request_sends(mode: TeamMode) -> None:
    """``leader_tool_specs`` lists, without a run, the tools a leader request sends, in the order it sends them."""
    leader = scripted("leader-model", answer("Nothing to hand out."))
    team = squad(
        leader,
        scripted("writer-model"),
        scripted("checker-model"),
        mode=mode,
        leader_toolkits=(toolkit(echo, add, name="job_controls"),),
    )

    await collect(
        start_team(team, prompt("hi"), ctx=run_context("r", SQUAD)),
    )

    [request] = leader.requests
    assert tuple(request.tools) == leader_tool_specs(team)
    delegation = "delegate_task_to_members" if mode is TeamMode.COLLABORATE else "delegate_task_to_member"
    assert [definition.name for definition in request.tools] == ["add", delegation, "echo"]


async def test_a_leader_level_confirmation_pauses_the_team_without_member_fields() -> None:
    leader = scripted("leader-model", calls(tool_call("echo", "call-echo", text="hello"), metrics=usage(10, 2)))
    team = squad(
        leader,
        scripted("writer-model"),
        scripted("checker-model"),
        leader_toolkits=(gated(echo, name="job_controls"),),
    )
    store = Store()

    _, record = await collect(
        start_team(
            team,
            prompt("hi"),
            ctx=run_context("r", SQUAD),
            persist=store,
        ),
    )

    assert record.status is RunStatus.paused
    assert record.requirements is not None
    [requirement] = record.requirements
    assert (requirement.member_agent_id, requirement.member_run_id) == (None, None)
    assert requirement.tool_execution.tool_call_id == "call-echo"
    assert record.content is None
    assert (record.member_responses, store.members) == ([], [])


async def test_a_member_s_external_call_pauses_the_team_with_the_member_s_raw_call_id() -> None:
    def run_subagent(agent_name: str, task: str) -> str:
        """Delegate a task to another agent.

        Args:
            agent_name: The agent to run.
            task: What it should do.

        """
        msg = f"{agent_name} runs outside the loop: {task}"
        raise AssertionError(msg)

    leader = scripted("leader-model", _delegate("call-team-sub", "checker", "Ask the helper", metrics=usage(10, 2)))
    checker = scripted(
        "checker-model",
        calls(tool_call("run_subagent", "call-sub", agent_name="helper", task="Multiply"), metrics=usage(5, 1)),
    )
    team = squad(leader, scripted("writer-model"), checker, checker_toolkits=(external(run_subagent),))
    store = Store()

    _, record = await collect(
        start_team(
            team,
            prompt("hi"),
            ctx=run_context("r", SQUAD),
            persist=store,
        ),
    )

    assert record.status is RunStatus.paused
    [member] = store.members
    assert record.requirements is not None
    [requirement] = record.requirements
    assert requirement.tool_execution.external_execution_required is True
    # Delegation composes its gate key and its projected approval id from these two ids; the runtime keeps them raw.
    assert f"{requirement.member_agent_id}:{requirement.tool_execution.tool_call_id}" == "checker:call-sub"
    assert requirement.member_run_id == member.run_id
    assert record.tools is not None
    assert record.tools[0].result == MEMBER_PAUSED_RESULT.format(name="Checker")
    assert record.content is None


async def test_cancelling_the_team_stores_the_member_s_cancelled_row_before_the_team_s() -> None:
    streaming = asyncio.Event()

    async def wait_to_be_cancelled() -> None:
        streaming.set()
        await asyncio.Event().wait()

    leader = scripted("leader-model", _delegate("call-team-writer", "writer", "Write", metrics=usage(400, 12)))
    writer = scripted(
        "writer-model",
        [TextDelta("Dra"), UsageReported(usage(50, 2), final=False), Await(wait_to_be_cancelled)],
    )
    team = squad(leader, writer, scripted("checker-model"))
    store = Store()
    writes: list[str] = []

    async def consume() -> None:
        try:
            async for _ in start_team(
                team,
                prompt("hi"),
                ctx=run_context("r", SQUAD),
                persist=store,
            ):
                pass
        except asyncio.CancelledError:
            writes.append("caller")
            raise

    task = asyncio.create_task(consume())
    await asyncio.wait_for(streaming.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert store.statuses == [("writer", "CANCELLED"), ("squad", "CANCELLED")]
    assert writes == ["caller"]
    member_row, team_row = store.records
    assert member_row["parent_run_id"] == "r"
    assert member_row["messages"][-1]["metrics"] == {"input_tokens": 50, "output_tokens": 2, "total_tokens": 52}
    assert team_row["member_responses"] == []
    assert [tool["result"] for tool in team_row["tools"]] == [None]


@pytest.mark.parametrize(
    ("writer_turns", "checker_turns", "expected"),
    [
        pytest.param(
            [answer("Five is prime.")],
            [answer("Five is odd.")],
            "Agent Writer: Five is prime.\n\nAgent Checker: Five is odd.",
            id="text",
        ),
        pytest.param(
            [_blank()],
            [_silent()],
            f"Agent Writer: {NO_MEMBER_RESPONSE}\n\nAgent Checker: {NO_MEMBER_RESPONSE}",
            id="empty",
        ),
        pytest.param(
            [calls(tool_call("echo", "call-a", text="a"), tool_call("echo", "call-b", text="")), _blank()],
            [calls(tool_call("echo", "call-c", text="c")), _silent()],
            # A member without text gives its tool results, leaving out empty ones, as a coordinate delegation does.
            "Agent Writer: a\n\nAgent Checker: c",
            id="tool-only",
        ),
        pytest.param(
            [answer("Five is prime.")],
            [[Await(_overloaded)]],
            # A failed member hands over its error text, which the broadcast labels like any result.
            "Agent Writer: Five is prime.\n\nAgent Checker: Checker overloaded",
            id="error",
        ),
    ],
)
async def test_a_collaborate_run_gives_the_leader_each_member_s_labeled_result(
    writer_turns: list[list[Any]],
    checker_turns: list[list[Any]],
    expected: str,
) -> None:
    leader = scripted(
        "leader-model",
        calls(tool_call("delegate_task_to_members", "call-all", task="Give one fact about 5."), metrics=usage(10, 2)),
        answer("Done."),
    )
    team = squad(
        leader,
        scripted("writer-model", *writer_turns),
        scripted("checker-model", *checker_turns),
        mode=TeamMode.COLLABORATE,
        writer_toolkits=(toolkit(echo),),
        checker_toolkits=(toolkit(echo),),
    )

    _, record = await collect(
        start_team(
            team,
            prompt("hi"),
            ctx=run_context("r", SQUAD),
        ),
    )

    assert record.status is RunStatus.completed
    assert record.tools is not None
    assert record.tools[0].result == expected
    delegated = leader.requests[1].messages[-1]
    assert (delegated.role, delegated.tool_call_id, delegated.content) == ("tool", "call-all", expected)


_REDACTED_FAILURE = "checker provider down: api_key=***redacted***"


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        pytest.param(TeamMode.COORDINATE, _REDACTED_FAILURE, id="coordinate"),
        pytest.param(
            TeamMode.COLLABORATE,
            f"Agent Writer: Five is prime.\n\nAgent Checker: {_REDACTED_FAILURE}",
            id="collaborate",
        ),
    ],
)
@pytest.mark.parametrize("wrote_text", [True, False], ids=["wrote-text", "silent"])
async def test_a_fresh_member_whose_provider_fails_reaches_the_leader_as_its_redacted_error(
    mode: TeamMode,
    expected: str,
    *,
    wrote_text: bool,
) -> None:
    """The leader reads why the member failed, credentials redacted, not the text the member wrote before failing."""

    async def lose_the_checker_provider() -> None:
        msg = "checker provider down: api_key=example-secret"
        raise ProviderError(msg, provider="Synthetic")

    earlier = [calls(tool_call("echo", "call-echo", text="5"), content="I will check.")] if wrote_text else []

    delegation = (
        calls(tool_call("delegate_task_to_members", "call-all", task="Check 5."), metrics=usage(10, 2))
        if mode is TeamMode.COLLABORATE
        else _delegate("call-all", "checker", "Check 5.", metrics=usage(10, 2))
    )
    leader = scripted("leader-model", delegation, answer("The checker could not finish."))
    checker = scripted("checker-model", *earlier, [Await(lose_the_checker_provider)])
    team = squad(
        leader,
        scripted("writer-model", answer("Five is prime.")),
        checker,
        mode=mode,
        checker_toolkits=(toolkit(echo),),
    )
    store = Store()

    _, record = await collect(
        start_team(
            team,
            prompt("hi"),
            ctx=run_context("r", SQUAD),
            persist=store,
        ),
    )

    assert record.status is RunStatus.completed
    assert record.tools is not None
    assert record.tools[0].result == expected
    delegated = leader.requests[1].messages[-1]
    assert (delegated.role, delegated.tool_call_id, delegated.content) == ("tool", "call-all", expected)
    # The member's stored row keeps the text its model wrote before the failure as its content, and why it failed,
    # credentials redacted, as its error.
    [failed] = [member for member in store.members if member.agent_id == "checker"]
    assert (failed.status, failed.content, failed.error) == (
        RunStatus.error,
        "I will check." if wrote_text else None,
        _REDACTED_FAILURE,
    )


@pytest.mark.parametrize(
    ("writer_turns", "expected"),
    [
        pytest.param([answer("")], NO_MEMBER_RESPONSE, id="empty"),
        pytest.param([_blank()], NO_MEMBER_RESPONSE, id="blank"),
        pytest.param([calls(tool_call("echo", "call-x", text="x")), _silent()], "x", id="tool-only"),
    ],
)
async def test_a_coordinate_member_without_text_reaches_the_leader_by_the_one_result_rule(
    writer_turns: list[list[Any]],
    expected: str,
) -> None:
    leader = scripted("leader-model", _delegate("call-w", "writer", "Do it", metrics=usage(10, 2)), answer("Done."))
    team = squad(
        leader,
        scripted("writer-model", *writer_turns),
        scripted("checker-model"),
        writer_toolkits=(toolkit(echo),),
    )

    _, record = await collect(
        start_team(
            team,
            prompt("hi"),
            ctx=run_context("r", SQUAD),
        ),
    )

    assert record.tools is not None
    assert record.tools[0].result == expected
    assert leader.requests[1].messages[-1].content == expected


def _chat_completion(message: dict[str, Any], finish_reason: str) -> dict[str, Any]:
    """A finished llama.cpp Chat completion with ``message``, which ``streamed_reply`` streams as the server does."""
    return {
        "id": "chatcmpl-member",
        "object": "chat.completion",
        "created": 1,
        "model": _LLAMA_CPP_MEMBER["id"],
        "choices": [{"index": 0, "message": {"role": "assistant", **message}, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
    }


@pytest.mark.parametrize(
    ("stop_after_call", "replies"),
    [
        pytest.param(True, 1, id="stopped-by-its-tool"),
        pytest.param(False, 2, id="silent-after-its-tool"),
    ],
)
async def test_the_leader_reads_a_chat_member_s_tool_results_when_it_writes_no_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    stop_after_call: bool,
    replies: int,
) -> None:
    # The server sends ``content: ""`` beside the tool call and in a reply after it, which the turn reads as no text.
    hide_provider_environment(monkeypatch)
    call = {"id": "call-x", "type": "function", "function": {"name": "echo", "arguments": '{"text": "x"}'}}
    wire = Wire(
        streamed_reply(_chat_completion({"content": "", "tool_calls": [call]}, "tool_calls")),
        streamed_reply(_chat_completion({"content": ""}, "stop")),
    )
    leader = scripted("leader-model", _delegate("call-w", "writer", "Echo x.", metrics=usage(10, 2)), answer("x."))
    writer = openai_family_adapter(_LLAMA_CPP_MEMBER, tmp_path, wire.respond)
    team = squad(
        leader,
        writer,
        scripted("checker-model"),
        writer_toolkits=(toolkit(echo, stop_after_call=stop_after_call),),
    )

    _, record = await collect(
        start_team(
            team,
            prompt("hi"),
            ctx=run_context("r", SQUAD),
        ),
    )

    assert len(wire.requests) == replies
    assert record.tools is not None
    assert record.tools[0].result == "x"
    assert leader.requests[1].messages[-1].content == "x"


async def test_the_leader_reads_an_ollama_member_s_answer_without_its_thinking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A thinking model's ``<think>`` text stays the member's reasoning, so the leader reads only the answer.
    hide_provider_environment(monkeypatch)
    reply = "<think>Five has two divisors.</think>Five is prime."
    sent: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        # Ollama puts a thinking model's tags in the content.
        sent.append(json.loads(request.content))
        done = {
            "model": _OLLAMA_MEMBER["id"],
            "created_at": "2026-10-20T00:00:00Z",
            "done": True,
            "done_reason": "stop",
            "message": {"role": "assistant", "content": ""},
            "prompt_eval_count": 7,
            "eval_count": 5,
        }
        text = {key: done[key] for key in ("model", "created_at")} | {
            "done": False,
            "message": {"role": "assistant", "content": reply},
        }
        return ollama_ndjson(text, done)

    leader = scripted(
        "leader-model",
        _delegate("call-w", "writer", "Is five prime?", metrics=usage(10, 2)),
        answer("Yes."),
    )
    writer = build_adapter(_OLLAMA_MEMBER, tmp_path, respond)
    team = squad(leader, writer, scripted("checker-model"), writer_toolkits=())
    store = Store()

    _, record = await collect(
        start_team(
            team,
            prompt("hi"),
            ctx=run_context("r", SQUAD),
            persist=store,
        ),
    )

    assert leader.requests[1].messages[-1].content == "Five is prime."
    assert record.tools is not None
    assert record.tools[0].result == "Five is prime."
    [member] = store.members
    assert (member.content, member.messages[-1].reasoning_content) == ("Five is prime.", "Five has two divisors.")
    assert [body["stream"] for body in sent] == [True]
