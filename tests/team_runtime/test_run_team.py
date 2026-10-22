"""Team runs on the owned loop store the rows Agno stored and tell the leader what Agno told it.

The C4 goldens are the reference: ``team-completed`` (a member used a tool), ``team-member-error`` (a member's run
failed), ``team-paused`` (a member's call waits for approval), and the streamed collaborate run ``panel-completed``.
"""
# ruff: noqa: D103

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from structlog.testing import capture_logs

from mindroom.llm.errors import ProviderError
from mindroom.llm.events import ContentDelta, RunFinished, ToolCompleted, ToolStarted
from mindroom.llm.messages import Message
from mindroom.llm.provider import ModelRequest, TextDelta, TurnCompleted, UsageReported
from mindroom.llm.runs import RunStatus
from mindroom.teams.delegate_tools import COLLABORATE_MEMBER_PAUSED_RESULT, MEMBER_PAUSED_RESULT, NO_MEMBER_RESPONSE
from mindroom.teams.prompt import TEAM_MEMBER_NOT_FOUND_TEMPLATE, render_members_roster
from mindroom.teams.runtime import MemberEvent, run_team, team_paused_content, team_usage
from mindroom.teams.spec import TeamMode
from tests.agent_loop.helpers import echo, external, gated, toolkit
from tests.fakes.scripted_model import Await, answer, calls, tool_call, usage
from tests.providers.gemini_ollama_support import build_adapter, hide_provider_environment, ollama_ndjson
from tests.team_runtime.squad import (
    PANEL,
    SQUAD,
    Store,
    collect,
    comparable,
    golden_metadata,
    golden_run,
    member_records,
    prompt,
    run_context,
    scripted,
    squad,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.llm.usage import MessageMetrics

pytestmark = pytest.mark.asyncio


def _delegate(call_id: str, member_id: str, task: str, *, metrics: MessageMetrics) -> list[Any]:
    return calls(tool_call("delegate_task_to_member", call_id, member_id=member_id, task=task), metrics=metrics)


# A blocking turn's leader requests do not stream, which is how Agno's team knew to run its non-stream delegation.
_BLOCKING = ModelRequest(messages=(), stream=False)

_OLLAMA_MEMBER = {"provider": "ollama", "id": "qwen3.8:27b", "host": "http://ollama.test:11434"}


async def _overloaded() -> None:
    msg = "Checker overloaded"
    raise ProviderError(msg, provider="Synthetic", status_code=503)


def _silent() -> list[Any]:
    """A turn that ends with no text at all, not even an empty string."""
    return [TurnCompleted(message=Message(role="assistant", content=None), stop_reason="end")]


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


async def test_a_coordinate_run_with_a_member_tool_call_stores_agno_s_rows() -> None:
    team_row = golden_run("team_squad", SQUAD, "team-completed")
    writer_row = golden_run("team_squad", SQUAD, "00000000-0000-4000-8000-00000000004b")
    leader = scripted(
        "leader-model",
        _delegate("call-team-writer", "writer", "Add 2 and 3", metrics=usage(400, 12)),
        answer("The writer found 5.", usage(450, 9, time_to_first_token=0.25)),
    )
    writer = scripted(
        "writer-model",
        calls(tool_call("add", "call-writer-add", a=2, b=3), metrics=usage(80, 4)),
        answer("5", usage(95, 1, time_to_first_token=0.25)),
    )
    checker = scripted("checker-model")
    ctx = run_context("team-completed", SQUAD, golden_metadata(team_row))
    store = Store()

    events, record = await collect(run_team(squad(leader, writer, checker), prompt(team_row), ctx=ctx, persist=store))

    assert comparable(record.to_dict()) == comparable(team_row)
    [member] = member_records(events)
    assert comparable(member.to_dict()) == comparable(writer_row)
    assert (
        member.metadata is record.metadata is ctx.metadata
    )  # One metadata dict, as Agno handed the member the team's.
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
    assert [type(event).__name__ for event in writer_events] == [
        "RequestCompleted",
        "ToolStarted",
        "ToolCompleted",
        "ContentDelta",
        "RequestCompleted",
    ]
    assert all(event.run_id == member.run_id for event in events if isinstance(event, MemberEvent))
    completed = next(event for event in writer_events if isinstance(event, ToolCompleted))
    assert (completed.tool.tool_name, completed.parent_run_id) == ("add", "team-completed")
    leader_tools = [event.tool.tool_name for event in events if isinstance(event, ToolStarted | ToolCompleted)]
    assert leader_tools == ["delegate_task_to_member", "delegate_task_to_member"]
    # The member's terminal record is announced before the leader's delegation completes, and the team ends last.
    kinds = [type(event).__name__ for event in events]
    assert kinds.index("MemberRunEnded") < kinds.index("ToolCompleted")
    assert isinstance(events[-1], RunFinished)


async def test_a_failing_member_reaches_the_leader_as_its_error_text_and_stores_an_error_row() -> None:
    team_row = golden_run("team_squad", SQUAD, "team-member-error")
    checker_row = golden_run("team_squad", SQUAD, "00000000-0000-4000-8000-000000000054")

    leader = scripted(
        "leader-model",
        _delegate("call-team-checker", "checker", "Check that 2 + 3 = 5", metrics=usage(420, 12)),
        answer("The checker could not answer.", usage(470, 8, time_to_first_token=0.25)),
    )
    checker = scripted("checker-model", [Await(_overloaded)])
    ctx = run_context("team-member-error", SQUAD, golden_metadata(team_row))
    store = Store()

    with capture_logs() as logs:
        events, record = await collect(
            run_team(squad(leader, scripted("writer-model"), checker), prompt(team_row), ctx=ctx, persist=store),
        )

    assert comparable(record.to_dict()) == comparable(team_row)
    [member] = member_records(events)
    assert comparable(member.to_dict()) == comparable(checker_row)
    assert (member.status, member.content) == (RunStatus.error, "Checker overloaded")
    assert record.tools is not None
    assert (record.tools[0].result, record.tools[0].tool_call_error) == ("Checker overloaded", False)
    assert store.statuses == [("checker", "ERROR"), ("squad", "COMPLETED")]
    assert [(log["event"], log["member_id"]) for log in logs if log["event"] == "team_member_run_failed"] == [
        ("team_member_run_failed", "checker"),
    ]


async def test_a_member_approval_pauses_the_team_with_agno_s_rows() -> None:
    team_row = golden_run("team_squad", SQUAD, "team-paused")
    checker_row = golden_run("team_squad", SQUAD, "00000000-0000-4000-8000-00000000005a")
    leader = scripted(
        "leader-model",
        _delegate("call-team-multiply", "checker", "Multiply 6 by 7", metrics=usage(430, 12)),
    )
    checker = scripted(
        "checker-model",
        calls(tool_call("multiply", "call-checker-multiply", a=6, b=7), metrics=usage(85, 5)),
    )
    team = squad(
        leader,
        scripted("writer-model"),
        checker,
        system=team_row["messages"][0]["content"],
        checker_system=checker_row["messages"][0]["content"],
    )
    store = Store()

    events, record = await collect(
        run_team(
            team,
            prompt(team_row),
            ctx=run_context("team-paused", SQUAD, golden_metadata(team_row)),
            persist=store,
        ),
    )

    assert record.status is RunStatus.paused
    assert comparable(record.to_dict()) == comparable(team_row)
    [member] = member_records(events)
    assert comparable(member.to_dict()) == comparable(checker_row)
    assert record.member_responses == [member]
    assert record.requirements is not None
    [requirement] = record.requirements
    # The approval card keys the call by member id and call id, both as the member run recorded them.
    assert f"{requirement.member_agent_id}:{requirement.tool_execution.tool_call_id}" == "checker:call-checker-multiply"
    assert (requirement.member_agent_name, requirement.member_run_id) == ("Checker", member.run_id)
    assert len(leader.requests) == 1  # The leader is not asked again while the member waits.
    assert store.statuses == [("checker", "PAUSED"), ("squad", "PAUSED")]
    assert record.content == "Team run paused. The following require input:\n- Checker: multiply requires confirmation"


async def test_a_streamed_collaborate_run_stores_agno_s_rows_and_joins_member_text_in_member_order() -> None:
    team_row = golden_run("team_panel", PANEL, "panel-completed")
    writer_row = golden_run("team_panel", PANEL, "00000000-0000-4000-8000-00000000006d")
    checker_row = golden_run("team_panel", PANEL, "00000000-0000-4000-8000-000000000070")
    leader = scripted(
        "leader-model",
        calls(
            tool_call("delegate_task_to_members", "call-panel", task="Give one fact about the number 5."),
            metrics=usage(380, 10),
        ),
        answer("Five is prime and odd.", usage(410, 7, time_to_first_token=0.25)),
    )
    writer = scripted("writer-model", answer("Five is prime.", usage(60, 3, time_to_first_token=0.25)))
    checker = scripted("checker-model", answer("Five is odd.", usage(61, 3, time_to_first_token=0.25)))
    team = squad(leader, writer, checker, mode=TeamMode.COLLABORATE, team_id="panel")
    store = Store()

    events, record = await collect(
        run_team(
            team,
            prompt(team_row),
            ctx=run_context("panel-completed", PANEL, golden_metadata(team_row)),
            persist=store,
        ),
    )

    assert comparable(record.to_dict()) == comparable(team_row)
    members = member_records(events)
    assert [comparable(member.to_dict()) for member in members] == [comparable(writer_row), comparable(checker_row)]
    assert record.tools is not None
    assert record.tools[0].result == "Five is prime.Five is odd."
    streamed = {
        member_id: "".join(
            event.event.text
            for event in events
            if isinstance(event, MemberEvent) and event.member_id == member_id and isinstance(event.event, ContentDelta)
        )
        for member_id in ("writer", "checker")
    }
    assert streamed == {"writer": "Five is prime.", "checker": "Five is odd."}
    assert store.statuses == [("writer", "COMPLETED"), ("checker", "COMPLETED"), ("panel", "COMPLETED")]
    # The turn's usage is the leader's plus each member's, counted once, within the leader's duration.
    total = team_usage(record, members)
    assert total is not None
    assert (total.input_tokens, total.output_tokens, total.total_tokens) == (911, 23, 934)
    assert total.duration == record.metrics.duration  # type: ignore[union-attr]
    assert total.details is not None
    assert sorted((entry.id, entry.input_tokens) for entry in total.details["model"]) == [
        ("checker-model", 61),
        ("leader-model", 790),
        ("writer-model", 60),
    ]


async def test_an_unknown_member_id_answers_the_leader_with_agno_s_roster_notice() -> None:
    leader = scripted(
        "leader-model",
        _delegate("call-nobody", "nobody", "Do it", metrics=usage(10, 2)),
        answer("There is no such member.", usage(20, 3)),
    )
    writer, checker = scripted("writer-model"), scripted("checker-model")
    team = squad(leader, writer, checker)

    events, record = await collect(
        run_team(team, prompt({"messages": [{"role": "user", "content": "hi"}]}), ctx=run_context("r", SQUAD)),
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
    assert (writer.requests, checker.requests, member_records(events)) == ([], [], [])
    assert record.status is RunStatus.completed


async def test_a_collaborate_member_that_pauses_adds_agno_s_notice_in_its_place_and_pauses_the_team() -> None:
    leader = scripted(
        "leader-model",
        calls(tool_call("delegate_task_to_members", "call-all", task="Multiply 6 by 7."), metrics=usage(10, 2)),
    )
    writer = scripted("writer-model", answer("Six times seven is 42."))
    checker = scripted("checker-model", calls(tool_call("multiply", "call-mul", a=6, b=7), metrics=usage(5, 1)))
    team = squad(leader, writer, checker, mode=TeamMode.COLLABORATE)

    events, record = await collect(
        run_team(team, prompt({"messages": [{"role": "user", "content": "hi"}]}), ctx=run_context("r", SQUAD)),
    )

    assert record.status is RunStatus.paused
    assert record.tools is not None
    assert record.tools[0].result == "Six times seven is 42." + COLLABORATE_MEMBER_PAUSED_RESULT.format(name="Checker")
    writer_record, checker_record = member_records(events)
    assert (writer_record.status, checker_record.status) == (RunStatus.completed, RunStatus.paused)
    assert record.member_responses == [checker_record]  # Only the paused member rides along, as Agno stored it.
    assert record.requirements is not None
    [requirement] = record.requirements
    assert (requirement.member_agent_id, requirement.member_run_id) == ("checker", checker_record.run_id)
    assert record.content == "Team run paused. The following require input:\n- Checker: multiply requires confirmation"


async def test_a_leader_level_confirmation_pauses_the_team_without_member_fields() -> None:
    leader = scripted("leader-model", calls(tool_call("echo", "call-echo", text="hello"), metrics=usage(10, 2)))
    team = squad(
        leader,
        scripted("writer-model"),
        scripted("checker-model"),
        leader_toolkits=(gated(echo, name="job_controls"),),
    )

    events, record = await collect(
        run_team(team, prompt({"messages": [{"role": "user", "content": "hi"}]}), ctx=run_context("r", SQUAD)),
    )

    assert record.status is RunStatus.paused
    assert record.requirements is not None
    [requirement] = record.requirements
    assert (requirement.member_agent_id, requirement.member_run_id) == (None, None)
    assert requirement.tool_execution.tool_call_id == "call-echo"
    assert record.content == "Team run paused. The following require input:\n- team: echo requires confirmation"
    assert (record.member_responses, member_records(events)) == ([], [])


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

    events, record = await collect(
        run_team(team, prompt({"messages": [{"role": "user", "content": "hi"}]}), ctx=run_context("r", SQUAD)),
    )

    assert record.status is RunStatus.paused
    [member] = member_records(events)
    assert record.requirements is not None
    [requirement] = record.requirements
    assert requirement.tool_execution.external_execution_required is True
    # Delegation composes its gate key and its projected approval id from these two ids; the runtime keeps them raw.
    assert f"{requirement.member_agent_id}:{requirement.tool_execution.tool_call_id}" == "checker:call-sub"
    assert requirement.member_run_id == member.run_id
    assert record.tools is not None
    assert record.tools[0].result == MEMBER_PAUSED_RESULT.format(name="Checker")
    # A silent external pause names nothing, so the team's paused text is empty, as Agno's was.
    assert team_paused_content(record) == ""


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
            async for _ in run_team(
                team,
                prompt({"messages": [{"role": "user", "content": "hi"}]}),
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
            "Agent Writer: Five is prime.Agent Checker: Five is odd.",
            id="text",
        ),
        pytest.param(
            [answer("")],
            [_silent()],
            f"Agent Writer: No ResponseAgent Checker: {NO_MEMBER_RESPONSE}",
            id="empty",
        ),
        pytest.param(
            [calls(tool_call("echo", "call-a", text="a"), tool_call("echo", "call-b", text="")), answer("")],
            [calls(tool_call("echo", "call-c", text="c")), _silent()],
            # The broadcast keeps an empty tool result, which a coordinate delegation drops.
            "Agent Writer: a,Agent Checker: null",
            id="tool-only",
        ),
        pytest.param(
            [answer("Five is prime.")],
            [[Await(_overloaded)]],
            # A failed member's record holds its error text, which the broadcast labels like any result.
            "Agent Writer: Five is prime.Agent Checker: Checker overloaded",
            id="error",
        ),
    ],
)
async def test_a_blocking_collaborate_run_gives_the_leader_agno_s_labeled_member_results(
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
        run_team(
            team,
            prompt({"messages": [{"role": "user", "content": "hi"}]}),
            ctx=run_context("r", SQUAD),
            request_template=_BLOCKING,
        ),
    )

    assert record.status is RunStatus.completed
    assert record.tools is not None
    assert record.tools[0].result == expected
    delegated = leader.requests[1].messages[-1]
    assert (delegated.role, delegated.tool_call_id, delegated.content) == ("tool", "call-all", expected)


async def test_a_blocking_collaborate_member_that_pauses_adds_agno_s_labeled_notice_and_pauses_the_team() -> None:
    leader = scripted(
        "leader-model",
        calls(tool_call("delegate_task_to_members", "call-all", task="Multiply 6 by 7."), metrics=usage(10, 2)),
    )
    writer = scripted("writer-model", answer("Six times seven is 42."))
    checker = scripted("checker-model", calls(tool_call("multiply", "call-mul", a=6, b=7), metrics=usage(5, 1)))
    team = squad(leader, writer, checker, mode=TeamMode.COLLABORATE)

    events, record = await collect(
        run_team(
            team,
            prompt({"messages": [{"role": "user", "content": "hi"}]}),
            ctx=run_context("r", SQUAD),
            request_template=_BLOCKING,
        ),
    )

    assert record.status is RunStatus.paused
    assert record.tools is not None
    assert record.tools[0].result == (
        "Agent Writer: Six times seven is 42." + COLLABORATE_MEMBER_PAUSED_RESULT.format(name="Checker")
    )
    _, checker_record = member_records(events)
    assert record.member_responses == [checker_record]
    assert record.requirements is not None
    [requirement] = record.requirements
    assert (requirement.member_agent_id, requirement.member_run_id) == ("checker", checker_record.run_id)


@pytest.mark.parametrize(
    ("stream", "writer_turns", "expected"),
    [
        pytest.param(True, [answer("")], NO_MEMBER_RESPONSE, id="streamed-empty"),
        pytest.param(False, [answer("")], "", id="blocking-empty"),
        pytest.param(True, [calls(tool_call("echo", "call-x", text="x")), _silent()], "x", id="streamed-tool-only"),
        pytest.param(False, [calls(tool_call("echo", "call-x", text="x")), _silent()], "null", id="blocking-tool-only"),
    ],
)
async def test_a_coordinate_member_without_text_reaches_the_leader_as_agno_s_delegation_gave_it_on_each_path(
    stream: bool,
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
        run_team(
            team,
            prompt({"messages": [{"role": "user", "content": "hi"}]}),
            ctx=run_context("r", SQUAD),
            request_template=ModelRequest(messages=(), stream=stream),
        ),
    )

    assert record.tools is not None
    assert record.tools[0].result == expected
    assert leader.requests[1].messages[-1].content == expected


@pytest.mark.parametrize("mode", [TeamMode.COORDINATE, TeamMode.COLLABORATE])
@pytest.mark.parametrize("stream", [False, True], ids=["blocking", "streaming"])
async def test_every_member_request_streams_exactly_when_the_team_turn_does(mode: TeamMode, stream: bool) -> None:
    # Agno ran each member with the team run's own ``stream``, so a blocking turn sends no member a streamed request.
    leader = scripted("leader-model", _delegation_to_both(mode), answer("Both found 5."))
    writer = scripted("writer-model", calls(tool_call("add", "call-add", a=2, b=3)), answer("5"))
    checker = scripted("checker-model", answer("5"))
    team = squad(leader, writer, checker, mode=mode, checker_toolkits=())

    _, record = await collect(
        run_team(
            team,
            prompt({"messages": [{"role": "user", "content": "hi"}]}),
            ctx=run_context("r", SQUAD),
            request_template=ModelRequest(messages=(), stream=stream),
        ),
    )

    assert record.status is RunStatus.completed
    assert (len(writer.requests), len(checker.requests)) == (2, 1)
    assert [request.stream for request in (*leader.requests, *writer.requests, *checker.requests)] == [stream] * 5


async def test_a_blocking_turn_hands_the_leader_an_ollama_member_s_answer_without_its_thinking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A thinking model's ``<think>`` text stays the member's reasoning, as Agno's non-stream member run split it.
    hide_provider_environment(monkeypatch)
    reply = "<think>Five has two divisors.</think>Five is prime."
    sent: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        # Ollama puts a thinking model's tags in the content, whether the reply streams or arrives whole.
        body = json.loads(request.content)
        sent.append(body)
        done = {
            "model": _OLLAMA_MEMBER["id"],
            "created_at": "2026-10-20T00:00:00Z",
            "done": True,
            "done_reason": "stop",
            "message": {"role": "assistant", "content": reply},
            "prompt_eval_count": 7,
            "eval_count": 5,
        }
        if not body["stream"]:
            return httpx.Response(200, json=done)
        text = {key: done[key] for key in ("model", "created_at", "message")} | {"done": False}
        return ollama_ndjson(text, done | {"message": {"role": "assistant", "content": ""}})

    leader = scripted(
        "leader-model",
        _delegate("call-w", "writer", "Is five prime?", metrics=usage(10, 2)),
        answer("Yes."),
    )
    writer = build_adapter(_OLLAMA_MEMBER, tmp_path, respond)
    team = squad(leader, writer, scripted("checker-model"), writer_toolkits=())

    events, record = await collect(
        run_team(
            team,
            prompt({"messages": [{"role": "user", "content": "hi"}]}),
            ctx=run_context("r", SQUAD),
            request_template=_BLOCKING,
        ),
    )

    assert leader.requests[1].messages[-1].content == "Five is prime."
    assert record.tools is not None
    assert record.tools[0].result == "Five is prime."
    [member] = member_records(events)
    assert (member.content, member.messages[-1].reasoning_content) == ("Five is prime.", "Five has two divisors.")
    assert [body["stream"] for body in sent] == [False]
