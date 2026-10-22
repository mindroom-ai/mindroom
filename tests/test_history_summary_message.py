"""The compaction summary replays as the first history message, never inside the system prompt.

The agent cases run on the owned request assembly; the team cases drive the ``squad`` team through the team envelope
with a summary seeded into its scope and read the leader's requests.
"""
# ruff: noqa: D103

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from mindroom.config.approval import ApprovalRuleConfig
from mindroom.dispatch_source import ScheduledHistoryBudget
from mindroom.execution_preparation import _prepared_history_with_scheduled_limit
from mindroom.history.replay import (
    compaction_summary_message,
    estimate_prompt_visible_history_tokens,
    estimate_request_messages_tokens,
    plan_replay_that_fits,
)
from mindroom.history.request_messages import (
    TurnMessages,
    build_request,
    build_resume_request,
    scope_history_messages,
)
from mindroom.history.summary_message import is_compaction_summary
from mindroom.history.types import (
    HistoryPolicy,
    HistoryScope,
    PreparedHistoryState,
    ResolvedHistorySettings,
    ResolvedReplayPlan,
    ScopeSummary,
)
from mindroom.llm.messages import Message
from mindroom.llm.runs import RunRecord, RunStatus, TeamRunRecord
from mindroom.response_turn import ResponsePausedForApproval
from mindroom.session_storage.types import ConversationSession
from tests.conftest import seed_session
from tests.fakes.scripted_model import ScriptedModel, answer, calls, tool_call, usage
from tests.history_helpers import _completed_team_run, _team_session, seed_summary
from tests.team_turn_helpers import (
    EVENT_ID,
    SESSION_ID,
    TEAM_SCOPE,
    TeamTurn,
    delegate,
    member_approval,
    squad_turn,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from mindroom.response_turn import PausedAttempt

_SUMMARY = "Goal: ship the release.\nProgress: tests pass."

_AGENT_SCOPE = HistoryScope(kind="agent", scope_id="helper")
_ALL_HISTORY = ResolvedHistorySettings(policy=HistoryPolicy(mode="all"), max_tool_calls_from_history=None)
_CURRENT = TurnMessages(prompt=Message(role="user", content="current request"))


def _owned_session(summary: str | None, *, runs: bool = True) -> ConversationSession:
    stored = [
        RunRecord(
            run_id=f"run-{index}",
            session_id="session",
            agent_id="helper",
            status=RunStatus.completed,
            messages=[
                Message(role="user", content=f"question {index}"),
                Message(role="assistant", content=f"answer {index}"),
            ],
        )
        for index in range(2)
    ]
    return ConversationSession(
        session_id="session",
        kind="agent",
        owner_id="helper",
        runs=stored if runs else [],
        summary=ScopeSummary(text=summary, updated_at=datetime.now(UTC)) if summary is not None else None,
    )


def _owned_summaries(messages: list[Message]) -> list[Message]:
    return [message for message in messages if is_compaction_summary(message)]


def test_a_summary_only_plan_replays_the_summary_without_runs() -> None:
    session = _owned_session(_SUMMARY)
    summary_tokens = estimate_prompt_visible_history_tokens(
        session=_owned_session(_SUMMARY, runs=False),
        scope=_AGENT_SCOPE,
        history_settings=_ALL_HISTORY,
    )

    plan = plan_replay_that_fits(
        session=session,
        scope=_AGENT_SCOPE,
        history_settings=_ALL_HISTORY,
        available_history_budget=summary_tokens,
        current_history_tokens=summary_tokens + 1_000,
    )
    history = scope_history_messages(
        session.runs,
        scope=_AGENT_SCOPE,
        num_history_runs=plan.num_history_runs,
        num_history_messages=plan.num_history_messages,
        max_tool_calls_from_history=None,
    )
    request = build_request(system=(), summary=session.summary, history=history, turn=_CURRENT)

    assert plan.mode == "disabled"
    assert plan.add_history_to_context is True
    assert plan.num_history_runs == 0
    assert [message.content for message in request[1:]] == ["current request"]
    assert len(_owned_summaries(request)) == 1


def test_replay_estimate_counts_the_rendered_summary_once() -> None:
    without = estimate_prompt_visible_history_tokens(
        session=_owned_session(None),
        scope=_AGENT_SCOPE,
        history_settings=_ALL_HISTORY,
    )
    with_summary = estimate_prompt_visible_history_tokens(
        session=_owned_session(_SUMMARY),
        scope=_AGENT_SCOPE,
        history_settings=_ALL_HISTORY,
    )

    rendered = str(compaction_summary_message(_SUMMARY, run_local=False).content)
    assert with_summary - without == len(rendered) // 4


def test_a_stored_run_local_summary_does_not_suppress_the_scope_summary_on_resume() -> None:
    stored_local = compaction_summary_message("local work so far", run_local=True)

    resumed = build_resume_request(
        paused=[stored_local, Message(role="user", content="current request")],
        summary=_owned_session(_SUMMARY).summary,
        history=[],
    )

    summaries = _owned_summaries(resumed)
    assert [summary.from_history for summary in summaries] == [True, False]
    assert _SUMMARY in str(summaries[0].content)


def test_request_estimate_matches_the_replay_estimate_for_the_same_messages() -> None:
    session = _owned_session(None)
    messages = [message for run in session.runs for message in run.messages]

    assert estimate_request_messages_tokens(messages, replay_model=None) == estimate_prompt_visible_history_tokens(
        session=session,
        scope=_AGENT_SCOPE,
        history_settings=_ALL_HISTORY,
    )


def test_scheduled_history_limits_keep_or_drop_the_summary() -> None:
    summary_only = PreparedHistoryState(
        replay_plan=ResolvedReplayPlan(
            mode="disabled",
            estimated_tokens=10,
            add_history_to_context=True,
            num_history_runs=0,
        ),
        replays_persisted_history=True,
    )

    assert _prepared_history_with_scheduled_limit(summary_only, 3) is summary_only
    no_history = _prepared_history_with_scheduled_limit(summary_only, 0).replay_plan
    assert no_history is not None
    assert no_history.add_history_to_context is False


# The block Agno rendered into a system message while it replayed the summary there.
_LEGACY_SYSTEM_SUFFIX = (
    "\n\nHere is a brief summary of your previous interactions:\n\n"
    "<summary_of_previous_interactions>\nOLD\n</summary_of_previous_interactions>\n\n"
)


def _seeded(team: TeamTurn) -> TeamTurn:
    """Seed two earlier runs and a summary into ``team``'s scope and return the team."""
    session = seed_session(
        team.storage,
        _team_session(
            SESSION_ID,
            team_id="squad",
            runs=[_completed_team_run("past-1", team_id="squad"), _completed_team_run("past-2", team_id="squad")],
        ),
    )
    seed_summary(team.storage, session, TEAM_SCOPE, _SUMMARY)
    return team


def _contents(messages: Sequence[Message]) -> list[str]:
    return [str(message.content) for message in messages]


async def _paused_on_the_checker(team: TeamTurn) -> PausedAttempt:
    with pytest.raises(ResponsePausedForApproval) as raised:
        await team.stream(show_tool_calls=False)
    return raised.value.paused


_PAST = ["past-1 team question", "past-1 team answer", "past-2 team question", "past-2 team answer"]


def _checker_pauses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TeamTurn:
    """Return the squad whose leader hands the checker a ``touch`` call that waits for approval."""
    return squad_turn(
        tmp_path,
        monkeypatch,
        leader=[delegate("call-team", "checker", "Touch it")],
        checker=[calls(tool_call("touch", "call-touch", label="dangerous"), metrics=usage(10, 1))],
        approval_rules=[ApprovalRuleConfig(match="touch", action="require_approval")],
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("squad_probe")
async def test_a_team_turn_replays_the_summary_before_its_history_and_outside_any_system_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    team = _seeded(squad_turn(tmp_path, monkeypatch, leader=[answer("Done.", usage(50, 5))]))

    await team.stream()

    request = team.models["default"].requests[0].messages
    [summary] = _owned_summaries(request)
    assert [message.role for message in request].count("system") == 1
    assert request[0].role == "system"
    assert _SUMMARY not in str(request[0].content)
    assert request[1] is summary
    assert (summary.role, summary.from_history) == ("user", True)
    assert f"<compacted_history>\n{_SUMMARY}\n</compacted_history>" in str(summary.content)
    assert _contents(request[2:6]) == _PAST
    assert "Hello" in str(request[-1].content)


@pytest.mark.asyncio
@pytest.mark.usefixtures("squad_probe")
async def test_a_team_turn_whose_plan_replays_no_history_sends_no_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A scheduled turn whose history budget is spent replays neither the summary nor the stored runs."""
    team = _seeded(squad_turn(tmp_path, monkeypatch, leader=[answer("Done.", usage(50, 5))]))
    spent = ScheduledHistoryBudget(limit=0, source_event_id=EVENT_ID)

    await team.stream(ctx=team.ctx(scheduled_history_budget=spent))

    request = team.models["default"].requests[0].messages
    assert _owned_summaries(request) == []
    assert not set(_PAST) & set(_contents(request))
    assert "Hello" in str(request[-1].content)


@pytest.mark.asyncio
@pytest.mark.usefixtures("squad_probe")
async def test_a_team_summary_only_plan_replays_the_summary_without_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When no stored run fits the leader's window, the turn still replays the summary, which does."""
    team = squad_turn(tmp_path, monkeypatch, leader=[answer("Done.", usage(50, 5))])
    long_run = _completed_team_run(
        "past-long",
        team_id="squad",
        messages=[Message(role="user", content="long " * 20_000), Message(role="assistant", content="ok")],
    )
    session = seed_session(team.storage, _team_session(SESSION_ID, team_id="squad", runs=[long_run]))
    seed_summary(team.storage, session, TEAM_SCOPE, _SUMMARY)
    team.config.models["default"].context_window = 8_000

    await team.stream()

    request = team.models["default"].requests[0].messages
    assert len(_owned_summaries(request)) == 1
    assert not any("long long" in content for content in _contents(request))
    assert "Hello" in str(request[-1].content)


@pytest.mark.asyncio
@pytest.mark.usefixtures("squad_probe")
async def test_a_team_resume_replays_the_summary_once_behind_the_paused_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The leader's resumed request keeps the summary and history its paused request saw, once, as its prefix."""
    team = _seeded(_checker_pauses(tmp_path, monkeypatch))
    paused = await _paused_on_the_checker(team)
    [paused_request] = team.models["default"].requests
    team.models["default"] = ScriptedModel([answer("The checker touched it.", usage(50, 5))])
    team.models["checker-model"] = ScriptedModel([answer("Touched.")])

    await team.resume(paused, approved=True, approvals=(member_approval("call-touch", "dangerous"),))

    [resumed] = team.models["default"].requests
    [summary] = _owned_summaries(resumed.messages)
    assert summary.content == _owned_summaries(paused_request.messages)[0].content
    assert _contents(resumed.messages)[: len(paused_request.messages)] == _contents(paused_request.messages)


@pytest.mark.asyncio
@pytest.mark.usefixtures("squad_probe")
@pytest.mark.parametrize("replays", [True, False])
async def test_a_team_continued_within_its_turn_replays_the_summary_only_under_a_plan_that_replays(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    replays: bool,
) -> None:
    """The leader continues after a member's child finished, with the summary only when the turn replays history."""
    team = _seeded(
        squad_turn(
            tmp_path,
            monkeypatch,
            leader=[delegate("call-team", "writer", "Research it"), answer("Consensus.", usage(50, 5))],
            writer=[
                calls(tool_call("run_subagent", "call-sub", agent_name="helper", task="Dig"), metrics=usage(10, 1)),
                answer("Writer: found it."),
            ],
            helper=[answer("Helper found it.")],
        ),
    )
    ctx = team.ctx()
    if not replays:
        ctx = replace(ctx, scheduled_history_budget=ScheduledHistoryBudget(limit=0, source_event_id=EVENT_ID))

    await team.stream(ctx=ctx)

    first, continued = team.models["default"].requests
    assert len(_owned_summaries(first.messages)) == int(replays)
    assert len(_owned_summaries(continued.messages)) == int(replays)
    assert (set(_PAST) <= set(_contents(continued.messages))) is replays


@pytest.mark.asyncio
@pytest.mark.usefixtures("squad_probe")
async def test_resuming_a_pre_release_team_pause_keeps_its_single_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A team paused while Agno rendered the summary into the leader's system message resumes with only that copy."""
    team = _seeded(_checker_pauses(tmp_path, monkeypatch))
    paused = await _paused_on_the_checker(team)
    session = team.storage.load_session(SESSION_ID, "team")
    assert session is not None
    stored = team.storage.get_run(paused.run_id)
    assert isinstance(stored, TeamRunRecord)
    system = stored.messages[0]
    assert system.role == "system"
    stored.messages[0] = replace(system, content=f"{system.content}{_LEGACY_SYSTEM_SUFFIX}")
    team.storage.save_runs(session, [stored])
    team.models["default"] = ScriptedModel([answer("The checker touched it.", usage(50, 5))])
    team.models["checker-model"] = ScriptedModel([answer("Touched.")])

    await team.resume(paused, approved=True, approvals=(member_approval("call-touch", "dangerous"),))

    [resumed] = team.models["default"].requests
    assert _owned_summaries(resumed.messages) == []
    assert resumed.messages[0].content == f"{system.content}{_LEGACY_SYSTEM_SUFFIX}"
