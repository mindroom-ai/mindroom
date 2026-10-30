"""The team empty-run guard, quiet scheduled replies, and what a failed team turn records for replay.

An empty completed attempt is never stored and is retried once, sharing the turn's continuation budget; a second empty
attempt answers with the shared notice. A collected reply is the streamed document: a tool-only reply shows only its
shown tool calls, and a member's text stays two paragraphs across a hidden call. Providers are scripted at the team
runtime seam (``tests/team_turn_helpers.py``).
"""
# ruff: noqa: D103

from __future__ import annotations

import time
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from mindroom import ai_runtime
from mindroom.agents import create_agent
from mindroom.constants import SILENT_SCHEDULE_NO_REPLY_TOKEN
from mindroom.llm.errors import ProviderError
from mindroom.llm.messages import Message
from mindroom.llm.runs import TeamRunRecord
from mindroom.timing import DispatchPipelineTiming
from mindroom.turn_continuation import TURN_CONTINUATION_LIMIT
from tests.conftest import seed_session
from tests.fakes.scripted_model import Await, ScriptStep, answer, calls, tool_call
from tests.history_helpers import _completed_team_run, _team_session
from tests.scripted_agent_helpers import ProcessCrash, crash_after_first_terminal_store
from tests.team_turn_helpers import (
    EVENT_ID,
    PROBE_TOOL,
    ROOM_ID,
    SESSION_ID,
    ProbeToolkit,
    TeamTurn,
    authored_team_config,
    build_team_turn,
    delegate,
    registered_probe,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from pathlib import Path

    from mindroom.tool_system.events import ToolTraceEntry

HEADER = "🤝 **Team Response** (GeneralAgent):\n\n"


@pytest.fixture(autouse=True)
def probe() -> Iterator[type[ProbeToolkit]]:
    """Register the probe toolkit the member's config names."""
    with registered_probe() as toolkit:
        yield toolkit


def _team(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    leader: Sequence[Sequence[ScriptStep]] = (),
    general: Sequence[Sequence[ScriptStep]] = (),
) -> TeamTurn:
    """Build the ad hoc team of ``general`` on ``general-model``, led on ``default``, which may switch to ``large``."""
    authored = authored_team_config(
        {
            "general": {
                "display_name": "GeneralAgent",
                "role": "Answers.",
                "model": "general-model",
                "tools": ["thread_model", PROBE_TOOL],
            },
        },
        models=("default", "general-model", "large"),
    )
    return build_team_turn(
        tmp_path,
        monkeypatch,
        authored,
        members=("general",),
        scripts={"default": leader, "general-model": general},
    )


def _team_run_ids(team: TeamTurn) -> list[str]:
    session = team.storage.load_session(SESSION_ID)
    assert session is not None
    return [run.run_id for run in session.runs if isinstance(run, TeamRunRecord)]


def _failing(message: str) -> list[ScriptStep]:
    async def fail() -> None:
        raise ProviderError(message, provider="Synthetic")

    return [Await(fail)]


@pytest.mark.asyncio
async def test_an_empty_completed_attempt_is_left_out_of_history_and_retried_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The empty run is never stored; the retry is a fresh model request the pipeline timing marks."""
    team = _team(tmp_path, monkeypatch, leader=[answer(""), answer("Recovered answer")])
    timing = DispatchPipelineTiming(source_event_id=EVENT_ID, room_id=ROOM_ID)

    text = await team.respond(pipeline_timing=timing)

    assert text.endswith("Recovered answer")
    assert len(team.models["default"].requests) == 2
    assert _team_run_ids(team) == ["run-1"]
    assert 0 < timing.marks["first_model_request_sent"] < timing.marks["model_request_sent"]


@pytest.mark.asyncio
async def test_a_crash_after_an_empty_team_attempt_leaves_nothing_the_next_prompt_replays(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The empty team run is never stored as a turn, so a crash right after its store leaves none behind."""
    team = _team(tmp_path, monkeypatch, leader=[answer(""), answer("Next answer.")])
    crash_after_first_terminal_store(monkeypatch)

    with pytest.raises(ProcessCrash):
        await team.respond()
    text = await team.respond(message="Next question", ctx=team.ctx(run_id="run-2"))

    assert text.endswith("Next answer.")
    assert [message for message in team.models["default"].requests[-1].messages if message.from_history] == []
    assert _team_run_ids(team) == ["run-2"]


@pytest.mark.asyncio
async def test_the_empty_retry_builds_only_the_members_it_delegates_to(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The empty attempt delegated to no member, so only the retry, which delegates, builds one."""
    team = _team(
        tmp_path,
        monkeypatch,
        leader=[answer(""), delegate("call-general", "general", "Help"), answer("Recovered answer")],
        general=[answer("Helped.")],
    )

    with patch("mindroom.teams.members.create_agent", wraps=create_agent) as build:
        text = await team.respond()

    assert text.endswith("Recovered answer")
    assert [call.args[0] for call in build.call_args_list] == ["general"]


@pytest.mark.asyncio
@pytest.mark.parametrize("empty", ["", "\n\n  "])
async def test_a_second_empty_attempt_answers_with_the_shared_notice_and_records_no_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    empty: str,
) -> None:
    """Whitespace is no answer either; neither attempt's fallback document leaks ahead of the notice."""
    request_times: list[float] = []

    async def note_request() -> None:
        request_times.append(time.perf_counter())

    team = _team(
        tmp_path,
        monkeypatch,
        leader=[[Await(note_request), *answer(empty)], [Await(note_request), *answer(empty)]],
    )
    timing = DispatchPipelineTiming(source_event_id=EVENT_ID, room_id=ROOM_ID)

    text = await team.respond(pipeline_timing=timing)

    assert text == ai_runtime.EMPTY_RESPONSE_NOTICE
    assert len(team.models["default"].requests) == 2
    # Neither empty run was stored, so no later turn replays an answer that ended immediately.
    assert _team_run_ids(team) == []
    # Each attempt's model request is marked once its context is ready, before the provider received it.
    assert timing.marks["first_model_request_sent"] <= request_times[0]
    assert timing.marks["model_request_sent"] <= request_times[1]
    assert team.recorder.outcome == "completed"
    assert team.recorder.assistant_text == ""


@pytest.mark.asyncio
async def test_stored_history_does_not_count_as_an_attempt_s_answer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An attempt replaying an earlier answer, but writing none, is still empty, and the earlier text is not the reply."""
    team = _team(tmp_path, monkeypatch, leader=[answer(""), answer("Recovered answer")])
    seed_session(
        team.storage,
        _team_session(
            SESSION_ID,
            team_id=team.scope.scope_id,
            runs=[
                _completed_team_run(
                    "run-earlier",
                    team_id=team.scope.scope_id,
                    messages=[
                        Message(role="user", content="Earlier question"),
                        Message(role="assistant", content="Previous turn answer"),
                    ],
                ),
            ],
        ),
    )

    text = await team.respond()

    assert text.endswith("Recovered answer")
    assert "Previous turn answer" not in text
    assert "Previous turn answer" in str(team.models["default"].requests[0].messages[3].content)
    assert len(team.models["default"].requests) == 2


@pytest.mark.asyncio
async def test_the_empty_retry_shares_the_turn_s_budget_with_model_switch_continuations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One empty attempt plus the continuations a member's switches start settle on the limit message."""
    switch = tool_call("switch_thread_model", "call-switch", model_name="large", when="next-turn")
    team = _team(
        tmp_path,
        monkeypatch,
        leader=[
            answer(""),
            *[
                step
                for _ in range(TURN_CONTINUATION_LIMIT)
                for step in (delegate("call-team", "general", "Go"), answer(""))
            ],
        ],
        general=[calls(switch) for _ in range(TURN_CONTINUATION_LIMIT)],
    )

    text = await team.respond(message="Keep switching models.")

    assert len(team.models["default"].requests) == 1 + 2 * TURN_CONTINUATION_LIMIT
    assert "did not produce a final answer" in text


@pytest.mark.asyncio
async def test_a_later_attempt_of_a_team_turn_on_a_new_scope_replays_the_run_an_earlier_attempt_saved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The scope's first session stays with the turn, so the attempt after a member's switch replays the first run.

    The scope has no stored session when the turn starts; the second attempt's leader request carries the first
    attempt's delegation.
    """
    switch = tool_call("switch_thread_model", "call-switch", model_name="large", when="next-turn")
    team = _team(
        tmp_path,
        monkeypatch,
        leader=[delegate("call-team", "general", "Go"), answer("Asked."), answer("Done.")],
        general=[calls(switch)],
    )

    text = await team.respond()

    assert text.endswith("Done.")
    *_, last = team.models["default"].requests
    assert [message.tool_call_id for message in last.messages if message.role == "tool"] == ["call-team"]


@pytest.mark.asyncio
@pytest.mark.parametrize("show_tool_calls", [True, False])
async def test_a_tool_only_team_reply_shows_the_fallback_document_but_records_no_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    show_tool_calls: bool,
) -> None:
    """A reply that shows nothing ends with the fallback line, display chrome rather than an answer to replay.

    The attempt ran tools, so it is not retried; a reply that shows its tool calls shows them instead of the line.
    """
    team = _team(
        tmp_path,
        monkeypatch,
        leader=[delegate("call-team", "general", "Run the tool"), answer("")],
        general=[calls(tool_call("touch", "call-touch", label="noon")), answer("")],
    )
    tool_trace: list[ToolTraceEntry] = []

    text = await team.respond(message="Run the tool.", show_tool_calls=show_tool_calls, tool_trace=tool_trace)

    if show_tool_calls:
        assert text.startswith("🤝 **Team Response** (GeneralAgent):\n\n**GeneralAgent**:\n\n🔧 `touch` [1]")
        assert [entry.tool_name for entry in tool_trace] == ["touch", "delegate_task_to_member"]
    else:
        assert text == HEADER + "No team response generated."
    assert len(team.models["default"].requests) == 2
    assert team.recorder.outcome == "completed"
    assert team.recorder.assistant_text == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("show_tool_calls", [True, False])
async def test_a_quiet_scheduled_team_reply_that_only_ran_tools_reports_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    show_tool_calls: bool,
) -> None:
    """A quiet schedule whose runs called tools but wrote nothing ends with an empty reply, which delivery suppresses.

    The tool calls it showed are not an answer, so the empty final text replaces the document they built; the trace
    stays with the reply.
    """
    team = _team(
        tmp_path,
        monkeypatch,
        leader=[delegate("call-team", "general", "Run the tool"), answer("")],
        general=[calls(tool_call("touch", "call-touch", label="noon")), answer("")],
    )
    tool_trace: list[ToolTraceEntry] = []

    text = await team.respond(
        ctx=team.ctx(allow_no_report_response=True),
        message="Run the tool.",
        show_tool_calls=show_tool_calls,
        tool_trace=tool_trace,
    )

    assert text == ""
    assert [entry.tool_name for entry in tool_trace] == (
        ["touch", "delegate_task_to_member"] if show_tool_calls else []
    )
    assert team.recorder.outcome == "completed"
    assert team.recorder.assistant_text == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("member_finding", [None, "Queue depth is elevated."])
async def test_a_quiet_scheduled_team_reply_is_the_no_reply_token_unless_a_member_found_something(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    member_finding: str | None,
) -> None:
    """The leader's acknowledgment reaches delivery without team chrome, but cannot hide a member's finding."""
    leader = (
        [answer(SILENT_SCHEDULE_NO_REPLY_TOKEN)]
        if member_finding is None
        else [delegate("call-team", "general", "Check"), answer(SILENT_SCHEDULE_NO_REPLY_TOKEN)]
    )
    team = _team(tmp_path, monkeypatch, leader=leader, general=[answer(member_finding or "")])

    text = await team.respond(
        ctx=team.ctx(allow_no_report_response=True),
        message="Check for updates.",
        show_tool_calls=False,
    )

    assert team.recorder.outcome == "completed"
    if member_finding is None:
        assert text == SILENT_SCHEDULE_NO_REPLY_TOKEN
    else:
        assert text == f"{HEADER}**GeneralAgent**: {member_finding}\n\n\n**Team Consensus**:\n\nNO_REPLY"
    # The turn records what it delivered: the bare token, or the document that carries the finding.
    assert team.recorder.assistant_text == text


@pytest.mark.asyncio
async def test_a_member_reply_split_by_a_hidden_tool_call_keeps_two_paragraphs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The member's text before and after its tool call reads as two paragraphs."""
    team = _team(
        tmp_path,
        monkeypatch,
        leader=[delegate("call-team", "general", "Check"), answer("Checked.")],
        general=[calls(tool_call("touch", "call-touch", label="noon"), content="Let me check."), answer("Done.")],
    )

    text = await team.respond(message="Check.", show_tool_calls=False)

    assert text == f"{HEADER}**GeneralAgent**: Let me check.\n\nDone.\n\n\n**Team Consensus**:\n\nChecked."
    assert team.recorder.assistant_text == text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [ProviderError("provider exploded", provider="Synthetic"), RuntimeError("transport died")],
)
async def test_a_team_turn_failing_after_a_member_answered_records_the_answer_interrupted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    error: Exception,
) -> None:
    async def fail() -> None:
        raise error

    team = _team(
        tmp_path,
        monkeypatch,
        leader=[delegate("call-team", "general", "Analyze"), [Await(fail)]],
        general=[answer("Member answer")],
    )

    text = await team.respond()

    assert str(error) in text
    assert team.recorder.outcome == "interrupted"
    assert "Member answer" in team.recorder.assistant_text


@pytest.mark.asyncio
async def test_a_team_turn_failing_before_any_output_still_records_the_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    team = _team(tmp_path, monkeypatch, leader=[_failing("provider exploded")])

    text = await team.respond()

    assert "provider exploded" in text
    assert team.recorder.outcome == "interrupted"
