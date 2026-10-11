"""The compaction summary replays as the first history message, never inside the system prompt."""
# ruff: noqa: D103

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from agno.agent import Agent
from agno.agent import _messages as agent_messages
from agno.db.in_memory import InMemoryDb
from agno.models.message import Message
from agno.run.agent import RunOutput
from agno.run.base import RunContext, RunStatus
from agno.run.team import TeamRunOutput
from agno.session.agent import AgentSession
from agno.session.summary import SessionSummary
from agno.session.team import TeamSession
from agno.team import Team
from agno.team import _messages as team_messages
from agno.team import _run as team_run

from mindroom.agents import create_agent
from mindroom.execution_preparation import _prepared_history_with_scheduled_limit
from mindroom.history import agno_compat_message_builder
from mindroom.history.replay import (
    compaction_summary_message,
    estimate_prompt_visible_history_tokens,
    estimate_request_messages_tokens,
    is_compaction_summary,
    plan_replay_that_fits,
)
from mindroom.history.types import (
    HistoryPolicy,
    HistoryScope,
    PreparedHistoryState,
    ResolvedHistorySettings,
    ResolvedReplayPlan,
)
from tests.test_agno_compat_message_builder import RecordingOpenAIChat, _paused_and_resumed_requests

if TYPE_CHECKING:
    from agno.run.messages import RunMessages

_SUMMARY = "Goal: ship the release.\nProgress: tests pass."


@pytest.fixture(autouse=True)
def _install_message_builder_patch() -> None:
    agno_compat_message_builder.apply_patch()


def _history_runs(kind: str) -> list[Any]:
    run_type = RunOutput if kind == "agent" else TeamRunOutput
    owner = {"agent_id": "helper"} if kind == "agent" else {"team_id": "crew"}
    return [
        run_type(
            run_id=f"run-{index}",
            session_id="session",
            status=RunStatus.completed,
            messages=[
                Message(role="user", content=f"question {index}"),
                Message(role="assistant", content=f"answer {index}"),
            ],
            **owner,
        )
        for index in range(2)
    ]


def _entity(kind: str, *, add_history_to_context: bool = True) -> Agent | Team:
    model = RecordingOpenAIChat(id="gpt-test", api_key="sk-test")
    if kind == "agent":
        return Agent(
            id="helper",
            model=model,
            instructions=["Be precise."],
            add_history_to_context=add_history_to_context,
            add_session_summary_to_context=False,
            telemetry=False,
        )
    return Team(
        id="crew",
        name="crew",
        model=model,
        members=[],
        instructions=["Be precise."],
        add_history_to_context=add_history_to_context,
        add_session_summary_to_context=False,
        telemetry=False,
    )


def _session(kind: str, summary: str | None) -> AgentSession | TeamSession:
    summary_value = SessionSummary(summary=summary) if summary is not None else None
    if kind == "agent":
        return AgentSession(session_id="session", agent_id="helper", runs=_history_runs(kind), summary=summary_value)
    return TeamSession(session_id="session", team_id="crew", runs=_history_runs(kind), summary=summary_value)


async def _build(kind: str, entity: Agent | Team, session: AgentSession | TeamSession) -> RunMessages:
    run_context = RunContext(run_id="current", session_id="session")
    if kind == "agent":
        assert isinstance(entity, Agent)
        assert isinstance(session, AgentSession)
        return await agent_messages.aget_run_messages(
            entity,
            run_response=RunOutput(run_id="current", session_id="session"),
            run_context=run_context,
            input=[Message(role="user", content="current request")],
            session=session,
            add_history_to_context=entity.add_history_to_context,
        )
    assert isinstance(entity, Team)
    assert isinstance(session, TeamSession)
    return await team_messages._aget_run_messages(
        entity,
        run_response=TeamRunOutput(run_id="current", session_id="session"),
        run_context=run_context,
        session=session,
        input_message=[Message(role="user", content="current request")],
        add_history_to_context=entity.add_history_to_context,
    )


def _summaries(messages: list[Message]) -> list[Message]:
    return [message for message in messages if is_compaction_summary(message)]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["agent", "team"])
async def test_summary_is_the_first_history_message_for_new_runs(kind: str) -> None:
    entity = _entity(kind)

    run_messages = await _build(kind, entity, _session(kind, _SUMMARY))

    messages = run_messages.messages
    assert messages[0].role == "system"
    assert _SUMMARY not in str(messages[0].content)
    assert "summary_of_previous_interactions" not in str(messages[0].content)
    summary = messages[1]
    assert is_compaction_summary(summary)
    assert summary.role == "user"
    assert summary.from_history is True
    assert f"<compacted_history>\n{_SUMMARY}\n</compacted_history>" in str(summary.content)
    assert [message.content for message in messages[2:]] == [
        "question 0",
        "answer 0",
        "question 1",
        "answer 1",
        "current request",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["agent", "team"])
async def test_system_prompt_is_identical_before_and_after_compaction(kind: str) -> None:
    entity = _entity(kind)

    before = await _build(kind, entity, _session(kind, None))
    after = await _build(kind, entity, _session(kind, _SUMMARY))

    assert before.messages[0].content == after.messages[0].content
    assert _summaries(before.messages) == []
    assert len(_summaries(after.messages)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["agent", "team"])
async def test_summary_not_inserted_without_history_replay(kind: str) -> None:
    entity = _entity(kind, add_history_to_context=False)

    run_messages = await _build(kind, entity, _session(kind, _SUMMARY))

    assert _summaries(run_messages.messages) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["agent", "team"])
async def test_summary_only_plan_replays_the_summary_without_runs(kind: str) -> None:
    entity = _entity(kind)
    session = _session(kind, _SUMMARY)
    scope = HistoryScope(kind=kind, scope_id="helper" if kind == "agent" else "crew")
    settings = ResolvedHistorySettings(policy=HistoryPolicy(mode="all"), max_tool_calls_from_history=None)
    summary_only = _session(kind, _SUMMARY)
    summary_only.runs = []
    summary_tokens = estimate_prompt_visible_history_tokens(
        session=summary_only,
        scope=scope,
        history_settings=settings,
    )

    plan = plan_replay_that_fits(
        session=session,
        scope=scope,
        history_settings=settings,
        available_history_budget=summary_tokens,
        current_history_tokens=summary_tokens + 1_000,
    )
    entity.add_history_to_context = plan.add_history_to_context
    entity.num_history_runs = plan.num_history_runs
    entity.num_history_messages = plan.num_history_messages
    run_messages = await _build(kind, entity, session)

    assert plan.mode == "disabled"
    assert plan.add_history_to_context is True
    assert plan.num_history_runs == 0
    assert [message.content for message in run_messages.messages[2:]] == ["current request"]
    assert len(_summaries(run_messages.messages)) == 1


def test_replay_estimate_counts_the_rendered_summary_once() -> None:
    scope = HistoryScope(kind="agent", scope_id="helper")
    settings = ResolvedHistorySettings(policy=HistoryPolicy(mode="all"), max_tool_calls_from_history=None)
    without = estimate_prompt_visible_history_tokens(
        session=_session("agent", None),
        scope=scope,
        history_settings=settings,
    )
    with_summary = estimate_prompt_visible_history_tokens(
        session=_session("agent", _SUMMARY),
        scope=scope,
        history_settings=settings,
    )

    rendered = str(compaction_summary_message(_SUMMARY, from_history=True).content)
    assert with_summary - without == len(rendered) // 4


@pytest.mark.asyncio
async def test_stored_run_local_summary_does_not_suppress_the_scope_summary() -> None:
    agent = _entity("agent")
    assert isinstance(agent, Agent)
    stored_local = compaction_summary_message("local work so far", from_history=False)

    run_messages = await agent_messages.aget_continue_run_messages(
        agent,
        input=[stored_local, Message(role="user", content="current request")],
        session=_session("agent", _SUMMARY),
        add_history_to_context=True,
    )

    summaries = _summaries(run_messages.messages)
    assert [summary.from_history for summary in summaries] == [True, False]
    assert _SUMMARY in str(summaries[0].content)


@pytest.mark.parametrize("kind", ["agent", "team"])
def test_summary_is_reinserted_on_synchronous_continuation(kind: str) -> None:
    entity = _entity(kind)
    paused_input = [Message(role="user", content="current request")]
    if kind == "agent":
        assert isinstance(entity, Agent)
        run_messages = agent_messages.get_continue_run_messages(
            entity,
            input=paused_input,
            session=_session(kind, _SUMMARY),
            add_history_to_context=True,
        )
    else:
        assert isinstance(entity, Team)
        run_messages = team_run._get_continue_run_messages(
            entity,
            input=paused_input,
            session=_session(kind, _SUMMARY),
            add_history_to_context=True,
        )

    summaries = _summaries(run_messages.messages)
    assert len(summaries) == 1
    assert run_messages.messages.index(summaries[0]) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["agent", "team"])
async def test_approval_resume_replays_the_summary_once(kind: str) -> None:
    paused_request, resumed_request = await _paused_and_resumed_requests(kind, stream=False, summary=_SUMMARY)

    assert len(_summaries(paused_request)) == 1
    assert len(_summaries(resumed_request)) == 1
    assert _summaries(resumed_request)[0].content == _summaries(paused_request)[0].content
    assert resumed_request[0].content == paused_request[0].content


def test_mindroom_agents_never_render_the_summary_in_the_system_prompt() -> None:
    from tests.conftest import runtime_paths_for  # noqa: PLC0415
    from tests.test_agents import _test_config  # noqa: PLC0415

    config = _test_config()
    agent = create_agent("calculator", config, runtime_paths_for(config), execution_identity=None)

    assert agent.add_history_to_context is True
    assert agent.add_session_summary_to_context is False


@pytest.mark.asyncio
async def test_minimal_agent_request_starts_history_with_the_summary() -> None:
    agent = Agent(
        id="helper",
        model=RecordingOpenAIChat(id="gpt-test", api_key="sk-test"),
        system_message="You are Helper (helper) in minimal mode.",
        add_history_to_context=True,
        db=InMemoryDb(),
        telemetry=False,
    )

    run_messages = await _build("agent", agent, _session("agent", _SUMMARY))

    assert run_messages.messages[0].content == "You are Helper (helper) in minimal mode."
    assert is_compaction_summary(run_messages.messages[1])


_LEGACY_SYSTEM_PROMPT = (
    "Be precise.\n\nHere is a brief summary of your previous interactions:\n\n"
    "<summary_of_previous_interactions>\nOLD\n</summary_of_previous_interactions>\n\n"
)


@pytest.mark.parametrize("kind", ["agent", "team"])
def test_resuming_a_pre_release_pause_keeps_its_single_summary(kind: str) -> None:
    entity = _entity(kind)
    paused_input = [
        Message(role="system", content=_LEGACY_SYSTEM_PROMPT),
        Message(role="user", content="current request"),
    ]
    if kind == "agent":
        assert isinstance(entity, Agent)
        run_messages = agent_messages.get_continue_run_messages(
            entity,
            input=paused_input,
            session=_session(kind, _SUMMARY),
            add_history_to_context=True,
        )
    else:
        assert isinstance(entity, Team)
        run_messages = team_run._get_continue_run_messages(
            entity,
            input=paused_input,
            session=_session(kind, _SUMMARY),
            add_history_to_context=True,
        )

    assert _summaries(run_messages.messages) == []
    assert run_messages.messages[0].content == _LEGACY_SYSTEM_PROMPT


def test_request_estimate_matches_the_replay_estimate_for_the_same_messages() -> None:
    session = _session("agent", None)
    scope = HistoryScope(kind="agent", scope_id="helper")
    settings = ResolvedHistorySettings(policy=HistoryPolicy(mode="all"), max_tool_calls_from_history=None)
    messages = [message for run in session.runs or [] for message in run.messages or []]

    assert estimate_request_messages_tokens(messages, replay_model=None) == estimate_prompt_visible_history_tokens(
        session=session,
        scope=scope,
        history_settings=settings,
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


@pytest.mark.asyncio
async def test_in_memory_continuation_keeps_its_one_summary() -> None:
    agent = _entity("agent")
    assert isinstance(agent, Agent)
    carried = compaction_summary_message("carried summary", from_history=True)

    run_messages = await agent_messages.aget_continue_run_messages(
        agent,
        input=[carried, Message(role="user", content="current request")],
        session=_session("agent", _SUMMARY),
        add_history_to_context=True,
    )

    assert _summaries(run_messages.messages) == [carried]
