"""The compaction summary replays as the first history message, never inside the system prompt.

The agent cases run on the owned request assembly; the team cases run on the team turn's prepared execution and
the leader's resume request, which select the summary and history the replay plan allows.
"""
# ruff: noqa: D103

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

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
from mindroom.history.session_context import ScopeSessionContext, create_scope_session_storage
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
from mindroom.media_inputs import MediaInputs
from mindroom.session_storage.types import ConversationSession
from mindroom.teams.envelope import PreparedTeamExecution, _replayed_team_history, _resume_request, _TeamStorage
from tests.history_helpers import _make_config

if TYPE_CHECKING:
    from pathlib import Path

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


# The team turn selects its summary and history through the replay plan, then builds the same request shape.
_TEAM_SCOPE = HistoryScope(kind="team", scope_id="crew")
_REPLAY_ALL = ResolvedReplayPlan(mode="configured", estimated_tokens=0, add_history_to_context=True)


def _team_session(summary: str | None, *, runs: bool = True) -> ConversationSession:
    stored = [
        TeamRunRecord(
            run_id=f"run-{index}",
            session_id="session",
            team_id="crew",
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
        kind="team",
        owner_id="crew",
        runs=stored if runs else [],
        summary=ScopeSummary(text=summary, updated_at=datetime.now(UTC)) if summary is not None else None,
    )


def _team_storage(tmp_path: Path, session: ConversationSession) -> tuple[_TeamStorage, ScopeSessionContext]:
    config, runtime_paths = _make_config(tmp_path)
    store = create_scope_session_storage(
        agent_name="test_agent",
        scope=_TEAM_SCOPE,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=None,
    )
    return _TeamStorage(store, session, _TEAM_SCOPE, None), ScopeSessionContext(_TEAM_SCOPE, store, session)


def _team_request(tmp_path: Path, session: ConversationSession, plan: ResolvedReplayPlan) -> list[Message]:
    """Build one team turn's request as ``prepare_materialized_team_execution`` and the attempt do."""
    config, _runtime_paths = _make_config(tmp_path)
    _storage, scope_context = _team_storage(tmp_path, session)
    history, summary = _replayed_team_history(
        scope_context,
        PreparedHistoryState(replay_plan=plan),
        config=config,
        configured_team_name=None,
    )
    prepared = PreparedTeamExecution(
        turn=_CURRENT,
        history=tuple(history),
        summary=summary,
        run_metadata=None,
        unseen_event_ids=[],
        prepared_history=PreparedHistoryState(replay_plan=plan),
        runtime_model_name="default",
    )
    return prepared.run_input(MediaInputs())


def test_a_team_turn_replays_the_summary_before_its_history_and_outside_any_system_message(tmp_path: Path) -> None:
    request = _team_request(tmp_path, _team_session(_SUMMARY), _REPLAY_ALL)

    [summary] = _owned_summaries(request)
    assert request[0] is summary
    assert (summary.role, summary.from_history) == ("user", True)
    assert f"<compacted_history>\n{_SUMMARY}\n</compacted_history>" in str(summary.content)
    assert "system" not in {message.role for message in request}
    assert [message.content for message in request[1:]] == [
        "question 0",
        "answer 0",
        "question 1",
        "answer 1",
        "current request",
    ]


def test_a_team_turn_whose_plan_replays_no_history_sends_no_summary(tmp_path: Path) -> None:
    no_replay = ResolvedReplayPlan(mode="disabled", estimated_tokens=0, add_history_to_context=False)

    request = _team_request(tmp_path, _team_session(_SUMMARY), no_replay)

    assert _owned_summaries(request) == []
    assert [message.content for message in request] == ["current request"]


def test_a_team_summary_only_plan_replays_the_summary_without_runs(tmp_path: Path) -> None:
    session = _team_session(_SUMMARY)
    summary_tokens = estimate_prompt_visible_history_tokens(
        session=_team_session(_SUMMARY, runs=False),
        scope=_TEAM_SCOPE,
        history_settings=_ALL_HISTORY,
    )

    plan = plan_replay_that_fits(
        session=session,
        scope=_TEAM_SCOPE,
        history_settings=_ALL_HISTORY,
        available_history_budget=summary_tokens,
        current_history_tokens=summary_tokens + 1_000,
    )
    request = _team_request(tmp_path, session, plan)

    assert plan.mode == "disabled"
    assert plan.add_history_to_context is True
    assert plan.num_history_runs == 0
    assert [message.content for message in request[1:]] == ["current request"]
    assert len(_owned_summaries(request)) == 1


def test_a_team_resume_replays_the_summary_once_under_its_plan(tmp_path: Path) -> None:
    """The leader's resume request keeps the summary its paused request saw, and a plan without replay adds none."""
    session = _team_session(_SUMMARY)
    storage, _scope_context = _team_storage(tmp_path, session)
    paused_request = _team_request(tmp_path, session, _REPLAY_ALL)
    persisted = TeamRunRecord(
        run_id="paused",
        session_id="session",
        team_id="crew",
        status=RunStatus.paused,
        # The stored messages hold no history copies: the system message, then the turn.
        messages=[Message(role="system", content="You lead."), Message(role="user", content="current request")],
    )

    resumed = _resume_request(storage, persisted, settings=_ALL_HISTORY, plan=_REPLAY_ALL)
    without_replay = _resume_request(storage, persisted, settings=_ALL_HISTORY, plan=None)

    [summary] = _owned_summaries(resumed)
    assert summary.content == _owned_summaries(paused_request)[0].content
    assert resumed[0].content == "You lead."
    assert resumed[1] is summary
    assert [message.content for message in resumed[2:]] == [
        *(m.content for m in paused_request[1:-1]),
        "current request",
    ]
    assert _owned_summaries(without_replay) == []
    assert [message.content for message in without_replay] == ["You lead.", "current request"]
