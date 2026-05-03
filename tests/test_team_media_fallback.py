"""Tests for team inline-media fallback behavior."""

from __future__ import annotations

import asyncio
import inspect
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from agno.agent import Agent as AgnoAgent
from agno.models.message import Message
from agno.models.response import ToolExecution
from agno.run.agent import RunContentEvent as AgentRunContentEvent
from agno.run.agent import RunOutput
from agno.run.agent import ToolCallCompletedEvent as AgentToolCallCompletedEvent
from agno.run.agent import ToolCallStartedEvent as AgentToolCallStartedEvent
from agno.run.base import RunStatus
from agno.run.team import RunCancelledEvent as TeamRunCancelledEvent
from agno.run.team import RunContentEvent as TeamRunContentEvent
from agno.run.team import RunErrorEvent as TeamRunErrorEvent
from agno.run.team import TeamRunOutput
from agno.team import Team as AgnoTeam
from agno.team._run import _cleanup_and_store
from agno.utils.message import get_text_from_message

from mindroom.agents import create_agent
from mindroom.ai_runtime import QUEUED_MESSAGE_NOTICE_TEXT
from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.constants import AI_RUN_METADATA_KEY, ROUTER_AGENT_NAME
from mindroom.execution_preparation import (
    PreparedExecutionContext,
    ThreadHistoryRenderLimits,
    prepare_bound_team_execution_context,
)
from mindroom.history.interrupted_replay import render_interrupted_replay_content
from mindroom.history.runtime import open_bound_scope_session_context
from mindroom.history.storage import read_scope_seen_event_ids, update_scope_seen_event_ids
from mindroom.history.turn_recorder import TurnRecorder
from mindroom.history.types import CompactionDecision, CompactionReplyOutcome
from mindroom.hooks import EnrichmentItem
from mindroom.knowledge import KnowledgeResolution
from mindroom.matrix.identity import MatrixID
from mindroom.media_inputs import MediaInputs
from mindroom.team_exact_members import (
    ResolvedExactTeamMembers,
    materialize_exact_requested_team_members,
    resolve_live_shared_agent_names,
)
from mindroom.teams import (
    TeamMode,
    _materialize_team_members,
    _prepare_materialized_team_execution,
    _team_response_stream_raw,
    materialize_exact_team_members,
    team_response,
    team_response_stream,
)
from mindroom.timing import DispatchPipelineTiming
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.conftest import bind_runtime_paths, make_visible_message, runtime_paths_for, test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


_TEST_MODEL = "openai:gpt-5.4"


def _make_test_agent(name: str) -> AgnoAgent:
    agent_id = name.removesuffix("Agent").replace(" ", "_").lower() or name.lower()
    return AgnoAgent(name=name, id=agent_id, model=_TEST_MODEL)


def _make_test_team(
    *,
    name: str = "Test Team",
    team_id: str = "test-team",
) -> AgnoTeam:
    return AgnoTeam(name=name, id=team_id, model=_TEST_MODEL, members=[], tools=[])


def _build_test_config() -> Config:
    runtime_paths = test_runtime_paths(Path(tempfile.mkdtemp()))
    return bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(display_name="GeneralAgent", rooms=["#test:example.org"]),
            },
        ),
        runtime_paths,
    )


def _prepared_team_execution_context(
    *,
    final_prompt: str,
    replays_persisted_history: bool = False,
    unseen_event_ids: list[str] | None = None,
    context_messages: tuple[Message, ...] = (),
    compaction_decision: CompactionDecision | None = None,
    compaction_reply_outcome: CompactionReplyOutcome = "none",
    prepared_context_tokens: int | None = None,
) -> PreparedExecutionContext:
    return PreparedExecutionContext(
        messages=(*context_messages, Message(role="user", content=final_prompt)),
        replay_plan=None,
        unseen_event_ids=unseen_event_ids or [],
        replays_persisted_history=replays_persisted_history,
        compaction_outcomes=[],
        compaction_decision=compaction_decision,
        compaction_reply_outcome=compaction_reply_outcome,
        prepared_context_tokens=prepared_context_tokens,
    )


def _queued_notice_message() -> Message:
    return Message(
        role="user",
        content=QUEUED_MESSAGE_NOTICE_TEXT,
        provider_data={"mindroom_queued_message_notice": True},
    )


def _has_queued_notice(messages: list[Message] | None) -> bool:
    return any(
        (
            isinstance(message.provider_data, dict)
            and message.provider_data.get("mindroom_queued_message_notice") is True
        )
        or message.content == QUEUED_MESSAGE_NOTICE_TEXT
        for message in messages or []
    )


def _team_turn_recorder(message: str) -> TurnRecorder:
    return TurnRecorder(user_message=message)


def test_team_response_requires_turn_recorder() -> None:
    """Direct team helper calls should explicitly opt into lifecycle recording."""
    turn_recorder = inspect.signature(team_response).parameters["turn_recorder"]
    assert turn_recorder.default is inspect.Signature.empty


def test_team_response_stream_requires_turn_recorder() -> None:
    """Direct team stream helper calls should explicitly opt into lifecycle recording."""
    turn_recorder = inspect.signature(team_response_stream).parameters["turn_recorder"]
    assert turn_recorder.default is inspect.Signature.empty


def test_resolve_live_shared_agent_names_returns_none_when_runtime_availability_is_unknown() -> None:
    """Missing shared runtime state must remain unknown, not become an empty live set."""
    config = _build_test_config()
    orchestrator = MagicMock()
    orchestrator.config = config
    orchestrator.agent_bots = object()

    assert resolve_live_shared_agent_names(orchestrator) is None


def test_resolve_live_shared_agent_names_filters_to_running_shared_agents() -> None:
    """Only running configured shared agents should be treated as live."""
    runtime_paths = test_runtime_paths(Path(tempfile.mkdtemp()))
    config = bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(display_name="GeneralAgent", rooms=["#test:example.org"]),
                "research": AgentConfig(display_name="ResearchAgent", rooms=["#test:example.org"]),
            },
        ),
        runtime_paths,
    )
    orchestrator = MagicMock()
    orchestrator.config = config
    orchestrator.agent_bots = {
        "router": MagicMock(running=True),
        "general": MagicMock(running=True),
        "research": MagicMock(running=False),
        "ghost": MagicMock(running=True),
    }

    assert resolve_live_shared_agent_names(orchestrator) == {"general"}


def test_materialize_exact_requested_team_members_short_circuits_missing_live_members() -> None:
    """Known-missing live members should fail before any builder callback runs."""
    build_member = MagicMock()

    team_members = materialize_exact_requested_team_members(
        ["general", "research"],
        materializable_agent_names={"general"},
        build_member=build_member,
    )

    assert team_members.requested_agent_names == ["general", "research"]
    assert team_members.materialized_agent_names == set()
    assert team_members.failed_agent_names == ["research"]
    build_member.assert_not_called()


def test_materialize_exact_team_members_closes_partial_agents_on_failure() -> None:
    """Partial exact-member materialization should close any runtime-owned DBs before raising."""
    runtime_paths = test_runtime_paths(Path(tempfile.mkdtemp()))
    config = bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(display_name="GeneralAgent", rooms=[]),
                "research": AgentConfig(display_name="ResearchAgent", rooms=[]),
            },
        ),
        runtime_paths,
    )
    built_agent = _make_test_agent("GeneralAgent")

    with (
        patch("mindroom.teams.create_agent", side_effect=[built_agent, RuntimeError("boom")]),
        patch("mindroom.teams.resolve_agent_knowledge_access", return_value=KnowledgeResolution(knowledge=None)),
        patch("mindroom.teams.close_team_runtime_state_dbs") as mock_close,
        pytest.raises(ValueError, match="research"),
    ):
        materialize_exact_team_members(
            ["general", "research"],
            config=config,
            runtime_paths=runtime_paths,
            execution_identity=None,
        )

    mock_close.assert_called_once_with(
        agents=[built_agent],
        team_db=None,
        shared_scope_storage=None,
    )


@pytest.mark.asyncio
async def test_team_response_retries_without_inline_media_on_validation_error() -> None:
    """Non-streaming team response should retry once without inline media."""
    config = _build_test_config()
    orchestrator = MagicMock()
    orchestrator.config = config
    orchestrator.runtime_paths = runtime_paths_for(config)
    orchestrator.knowledge_managers = {}
    orchestrator.agent_bots = {"general": MagicMock()}

    media_validation_error = "Error code: 500 - audio input is not supported"
    mock_team = _make_test_team()
    mock_team.arun = AsyncMock(
        side_effect=[
            Exception(media_validation_error),
            TeamRunOutput(content="Recovered team response"),
        ],
    )
    audio_input = MagicMock(name="audio_input")

    fake_agent = _make_test_agent("GeneralAgent")
    with (
        patch("mindroom.teams.create_agent", return_value=fake_agent),
        patch("mindroom.teams.resolve_agent_knowledge_access", return_value=KnowledgeResolution(knowledge=None)),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
    ):
        response = await team_response(
            agent_names=["general"],
            mode=TeamMode.COORDINATE,
            message="Analyze this.",
            turn_recorder=_team_turn_recorder("Analyze this."),
            orchestrator=orchestrator,
            execution_identity=None,
            media=MediaInputs(audio=[audio_input]),
        )

    assert "Recovered team response" in response
    assert mock_team.arun.await_count == 2
    first_call = mock_team.arun.await_args_list[0]
    second_call = mock_team.arun.await_args_list[1]
    first_prompt = first_call.args[0]
    second_prompt = second_call.args[0]
    assert isinstance(first_prompt, list)
    assert isinstance(second_prompt, str)
    assert first_prompt[-1].audio == [audio_input]
    assert "Inline media unavailable for this model" in second_prompt


@pytest.mark.asyncio
async def test_team_response_retries_errored_plain_run_output_with_fresh_run_id() -> None:
    """Inline-media team retries must also cover plain-string errored RunOutput fallbacks."""
    config = _build_test_config()
    orchestrator = MagicMock()
    orchestrator.config = config
    orchestrator.runtime_paths = runtime_paths_for(config)
    orchestrator.knowledge_managers = {}
    orchestrator.agent_bots = {"general": MagicMock()}

    mock_team = _make_test_team()
    mock_team.arun = AsyncMock(
        side_effect=[
            RunOutput(content="Error code: 500 - audio input is not supported", status="error"),
            TeamRunOutput(content="Recovered team response", status=RunStatus.completed),
        ],
    )

    fake_agent = _make_test_agent("GeneralAgent")
    callback_run_ids: list[str] = []
    with (
        patch("mindroom.teams.create_agent", return_value=fake_agent),
        patch("mindroom.teams.resolve_agent_knowledge_access", return_value=KnowledgeResolution(knowledge=None)),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
    ):
        response = await team_response(
            agent_names=["general"],
            mode=TeamMode.COORDINATE,
            message="Analyze this.",
            turn_recorder=_team_turn_recorder("Analyze this."),
            orchestrator=orchestrator,
            execution_identity=None,
            media=MediaInputs(audio=[MagicMock(name="audio_input")]),
            run_id="run-123",
            run_id_callback=callback_run_ids.append,
        )

    assert "Recovered team response" in response
    first_call = mock_team.arun.await_args_list[0]
    second_call = mock_team.arun.await_args_list[1]
    assert first_call.kwargs["run_id"] == "run-123"
    assert second_call.kwargs["run_id"] is not None
    assert second_call.kwargs["run_id"] != "run-123"
    assert callback_run_ids == [first_call.kwargs["run_id"], second_call.kwargs["run_id"]]


@pytest.mark.asyncio
async def test_team_response_retry_scrubs_queued_notice_before_second_attempt() -> None:
    """Non-stream retries should scrub queued notices from the loaded team session before retrying."""
    config = _build_test_config()
    runtime_paths = runtime_paths_for(config)
    orchestrator = MagicMock()
    orchestrator.config = config
    orchestrator.runtime_paths = runtime_paths
    orchestrator.knowledge_managers = {}
    orchestrator.agent_bots = {"general": MagicMock()}

    fake_agent = _make_test_agent("GeneralAgent")
    with open_bound_scope_session_context(
        agents=[fake_agent],
        session_id="session-retry-clean",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=None,
        create_session_if_missing=True,
    ) as scope_context:
        assert scope_context is not None
        assert scope_context.session is not None
        scope_context.storage.upsert_session(scope_context.session)
    mock_team = _make_test_team(name="General Team")
    prepared_scope_context = None
    attempts = 0

    async def fake_prepare_bound_team_execution_context(**kwargs: object) -> PreparedExecutionContext:
        nonlocal prepared_scope_context
        scope_context = kwargs["scope_context"]
        assert scope_context is not None
        assert scope_context.session is not None
        prepared_scope_context = scope_context
        return _prepared_team_execution_context(final_prompt="Analyze this.")

    async def fake_arun(*_args: object, **_kwargs: object) -> TeamRunOutput:
        nonlocal attempts
        attempts += 1
        assert prepared_scope_context is not None
        assert prepared_scope_context.session is not None
        mock_team.db = prepared_scope_context.storage
        team_id = prepared_scope_context.session.team_id
        assert team_id is not None
        if attempts == 1:
            errored_output = TeamRunOutput(
                run_id="run-1",
                team_id=team_id,
                team_name="General Team",
                session_id="session-retry-clean",
                content="Error code: 500 - audio input is not supported",
                messages=[_queued_notice_message()],
                status=RunStatus.error,
            )
            _cleanup_and_store(mock_team, errored_output, prepared_scope_context.session)
            return errored_output
        assert not any(_has_queued_notice(run.messages) for run in prepared_scope_context.session.runs or [])
        return TeamRunOutput(
            run_id="run-2",
            team_id=team_id,
            team_name="General Team",
            session_id="session-retry-clean",
            content="Recovered team response",
            status=RunStatus.completed,
        )

    mock_team.arun = AsyncMock(side_effect=fake_arun)

    with (
        patch("mindroom.teams.create_agent", return_value=fake_agent),
        patch("mindroom.teams.resolve_agent_knowledge_access", return_value=KnowledgeResolution(knowledge=None)),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
        patch(
            "mindroom.teams.prepare_bound_team_run_context",
            new=AsyncMock(side_effect=fake_prepare_bound_team_execution_context),
        ),
    ):
        response = await team_response(
            agent_names=["general"],
            mode=TeamMode.COORDINATE,
            message="Analyze this.",
            turn_recorder=_team_turn_recorder("Analyze this."),
            orchestrator=orchestrator,
            execution_identity=None,
            media=MediaInputs(audio=[MagicMock(name="audio_input")]),
            session_id="session-retry-clean",
        )

    assert attempts == 2
    assert "Recovered team response" in response


@pytest.mark.asyncio
async def test_team_response_fallback_run_output_cleans_queued_notice_before_formatting() -> None:
    """Fallback RunOutput values should be cleaned and formatted like normal agent results."""
    config = _build_test_config()
    orchestrator = MagicMock()
    orchestrator.config = config
    orchestrator.runtime_paths = runtime_paths_for(config)
    orchestrator.knowledge_managers = {}
    orchestrator.agent_bots = {"general": MagicMock()}

    mock_team = _make_test_team()
    fallback_result = RunOutput(
        run_id="run-123",
        session_id="session-123",
        agent_name="general",
        content=None,
        messages=[
            Message(role="assistant", content="Recovered team response"),
            _queued_notice_message(),
        ],
        status=RunStatus.completed,
    )
    mock_team.arun = AsyncMock(return_value=fallback_result)

    fake_agent = _make_test_agent("GeneralAgent")
    with (
        patch("mindroom.teams.create_agent", return_value=fake_agent),
        patch("mindroom.teams.resolve_agent_knowledge_access", return_value=KnowledgeResolution(knowledge=None)),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
    ):
        response = await team_response(
            agent_names=["general"],
            mode=TeamMode.COORDINATE,
            message="Analyze this.",
            turn_recorder=_team_turn_recorder("Analyze this."),
            orchestrator=orchestrator,
            execution_identity=None,
            session_id="session-123",
        )

    assert "Recovered team response" in response
    assert "RunOutput(" not in response
    assert QUEUED_MESSAGE_NOTICE_TEXT not in response


@pytest.mark.asyncio
async def test_team_response_fallback_run_output_error_uses_friendly_error() -> None:
    """Errored RunOutput fallbacks should use the normal team error path."""
    config = _build_test_config()
    orchestrator = MagicMock()
    orchestrator.config = config
    orchestrator.runtime_paths = runtime_paths_for(config)
    orchestrator.knowledge_managers = {}
    orchestrator.agent_bots = {"general": MagicMock()}

    mock_team = _make_test_team()
    fallback_result = RunOutput(
        run_id="run-123",
        session_id="session-123",
        agent_name="general",
        content="validation failed in team",
        status=RunStatus.error,
    )
    mock_team.arun = AsyncMock(return_value=fallback_result)

    fake_agent = _make_test_agent("GeneralAgent")
    with (
        patch("mindroom.teams.create_agent", return_value=fake_agent),
        patch("mindroom.teams.resolve_agent_knowledge_access", return_value=KnowledgeResolution(knowledge=None)),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
        patch("mindroom.teams.get_user_friendly_error_message", return_value="friendly-team-error"),
    ):
        response = await team_response(
            agent_names=["general"],
            mode=TeamMode.COORDINATE,
            message="Analyze this.",
            turn_recorder=_team_turn_recorder("Analyze this."),
            orchestrator=orchestrator,
            execution_identity=None,
            session_id="session-123",
        )

    assert response == "friendly-team-error"


@pytest.mark.asyncio
async def test_team_response_uses_compaction_aware_member_execution() -> None:
    """Direct team execution should prepare member history and apply queued compactions."""
    config = _build_test_config()
    orchestrator = MagicMock()
    orchestrator.config = config
    orchestrator.runtime_paths = runtime_paths_for(config)
    orchestrator.knowledge_managers = {}
    orchestrator.agent_bots = {"general": MagicMock()}
    mock_team = _make_test_team()
    mock_team.arun = AsyncMock(return_value=TeamRunOutput(content="Recovered team response"))
    fake_agent = _make_test_agent("GeneralAgent")
    collector: list[object] = []

    with (
        patch("mindroom.teams.create_agent", return_value=fake_agent),
        patch("mindroom.teams.resolve_agent_knowledge_access", return_value=KnowledgeResolution(knowledge=None)),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
        patch("mindroom.teams.prepare_bound_team_run_context", new_callable=AsyncMock) as mock_prepare,
    ):
        mock_prepare.return_value = _prepared_team_execution_context(final_prompt="Analyze this.")
        response = await team_response(
            agent_names=["general"],
            mode=TeamMode.COORDINATE,
            message="Analyze this.",
            turn_recorder=_team_turn_recorder("Analyze this."),
            orchestrator=orchestrator,
            execution_identity=None,
            session_id="session-123",
            compaction_outcomes_collector=collector,
        )

    assert "Recovered team response" in response
    assert mock_prepare.await_count == 1
    assert mock_prepare.await_args.kwargs["agents"] == [fake_agent]
    assert mock_prepare.await_args.kwargs["team"] is mock_team
    assert mock_prepare.await_args.kwargs["prompt"] == "Analyze this."
    scope_context = mock_prepare.await_args.kwargs["scope_context"]
    assert scope_context is not None
    assert scope_context.scope.kind == "team"
    assert mock_prepare.await_args.kwargs["compaction_outcomes_collector"] is collector


@pytest.mark.asyncio
async def test_team_response_prefers_persisted_history_over_thread_context_fallback() -> None:
    """Persisted team history should let Agno replay natively and skip thread stuffing."""
    config = _build_test_config()
    orchestrator = MagicMock()
    orchestrator.config = config
    orchestrator.runtime_paths = runtime_paths_for(config)
    orchestrator.knowledge_managers = {}
    orchestrator.agent_bots = {"general": MagicMock()}

    mock_team = _make_test_team()
    mock_team.arun = AsyncMock(return_value=TeamRunOutput(content="Recovered team response"))
    fake_agent = _make_test_agent("GeneralAgent")

    with (
        patch("mindroom.teams.create_agent", return_value=fake_agent),
        patch("mindroom.teams.resolve_agent_knowledge_access", return_value=KnowledgeResolution(knowledge=None)),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
        patch("mindroom.teams.prepare_bound_team_run_context", new_callable=AsyncMock) as mock_prepare,
    ):
        mock_prepare.return_value = _prepared_team_execution_context(
            final_prompt="Analyze this.",
            replays_persisted_history=True,
        )
        response = await team_response(
            agent_names=["general"],
            mode=TeamMode.COORDINATE,
            message="Analyze this.",
            turn_recorder=_team_turn_recorder("Analyze this."),
            thread_history=[make_visible_message(sender="user", body="Old thread context")],
            orchestrator=orchestrator,
            execution_identity=None,
            session_id="session-123",
        )

    assert "Recovered team response" in response
    assert mock_prepare.await_args.kwargs["team"] is mock_team
    assert mock_prepare.await_args.kwargs["prompt"] == "Analyze this."
    assert [message.body for message in mock_prepare.await_args.kwargs["thread_history"]] == [
        "Old thread context",
    ]
    prompt = mock_team.arun.await_args.args[0]
    assert prompt == "Analyze this."


@pytest.mark.asyncio
async def test_team_response_preserves_unseen_matrix_thread_context_with_persisted_history() -> None:
    """Matrix team runs should include unseen live thread messages with native replay."""
    config = _build_test_config()
    runtime_paths = runtime_paths_for(config)
    orchestrator = MagicMock()
    orchestrator.config = config
    orchestrator.runtime_paths = runtime_paths
    orchestrator.knowledge_managers = {}
    orchestrator.agent_bots = {"general": MagicMock()}

    mock_team = _make_test_team()
    mock_team.arun = AsyncMock(return_value=TeamRunOutput(content="Recovered team response"))
    fake_agent = _make_test_agent("GeneralAgent")
    with open_bound_scope_session_context(
        agents=[fake_agent],
        session_id="session-123",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=None,
        create_session_if_missing=True,
    ) as scope_context:
        assert scope_context is not None
        assert scope_context.session is not None
        update_scope_seen_event_ids(scope_context.session, scope_context.scope, ["event-1"])
        scope_context.storage.upsert_session(scope_context.session)

    thread_history = [
        make_visible_message(event_id="event-1", sender="user", body="Already seen"),
        make_visible_message(event_id="event-2", sender="user", body="Fresh follow-up"),
        make_visible_message(event_id="event-3", sender="user", body="Current message body"),
    ]

    with (
        patch("mindroom.teams.create_agent", return_value=fake_agent),
        patch("mindroom.teams.resolve_agent_knowledge_access", return_value=KnowledgeResolution(knowledge=None)),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
        patch("mindroom.teams.prepare_bound_team_run_context", new_callable=AsyncMock) as mock_prepare,
    ):
        mock_prepare.return_value = _prepared_team_execution_context(
            final_prompt="Analyze this.",
            replays_persisted_history=True,
            unseen_event_ids=["event-2"],
            context_messages=(Message(role="user", content="user: Fresh follow-up"),),
        )
        response = await team_response(
            agent_names=["general"],
            mode=TeamMode.COORDINATE,
            message="Analyze this.",
            turn_recorder=_team_turn_recorder("Analyze this."),
            thread_history=thread_history,
            orchestrator=orchestrator,
            execution_identity=None,
            session_id="session-123",
            reply_to_event_id="event-3",
            response_sender_id="@mindroom_team:example.org",
        )

    assert "Recovered team response" in response
    assert mock_prepare.await_args.kwargs["team"] is mock_team
    prompt = mock_team.arun.await_args.args[0]
    assert prompt == "user: Fresh follow-up\n\nAnalyze this."


@pytest.mark.asyncio
async def test_team_response_scrubs_queued_notices_before_prepare_and_after_run() -> None:
    """Team runs should not replay or persist hidden queued-message notices."""
    config = _build_test_config()
    runtime_paths = runtime_paths_for(config)
    orchestrator = MagicMock()
    orchestrator.config = config
    orchestrator.runtime_paths = runtime_paths
    orchestrator.knowledge_managers = {}
    orchestrator.agent_bots = {"general": MagicMock()}

    fake_agent = _make_test_agent("GeneralAgent")
    with open_bound_scope_session_context(
        agents=[fake_agent],
        session_id="session-queued",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=None,
        create_session_if_missing=True,
    ) as scope_context:
        assert scope_context is not None
        assert scope_context.session is not None
        persisted_team = _make_test_team(
            name="General Team",
            team_id=scope_context.session.team_id,
        )
        persisted_team.db = scope_context.storage
        _cleanup_and_store(
            persisted_team,
            TeamRunOutput(
                run_id="run-1",
                team_id=scope_context.session.team_id,
                team_name="General Team",
                session_id="session-queued",
                messages=[_queued_notice_message()],
                status=RunStatus.completed,
            ),
            scope_context.session,
        )

    prepared_scope_context = None
    team_id = "team_general"
    mock_team = _make_test_team(name="General Team", team_id=team_id)

    async def fake_arun(*_args: object, **_kwargs: object) -> TeamRunOutput:
        assert prepared_scope_context is not None
        mock_team.db = prepared_scope_context.storage
        assert prepared_scope_context.session is not None
        _cleanup_and_store(
            mock_team,
            TeamRunOutput(
                run_id="run-2",
                team_id=team_id,
                team_name="General Team",
                session_id="session-queued",
                content="Recovered team response",
                messages=[_queued_notice_message()],
                status=RunStatus.completed,
            ),
            prepared_scope_context.session,
        )
        return TeamRunOutput(
            run_id="run-2",
            team_id=team_id,
            team_name="General Team",
            session_id="session-queued",
            content="Recovered team response",
            messages=[_queued_notice_message()],
            status=RunStatus.completed,
        )

    mock_team.arun = AsyncMock(side_effect=fake_arun)

    async def fake_prepare_bound_team_execution_context(**kwargs: object) -> PreparedExecutionContext:
        nonlocal prepared_scope_context
        scope_context = kwargs["scope_context"]
        assert scope_context is not None
        assert scope_context.session is not None
        prepared_scope_context = scope_context
        assert not any(_has_queued_notice(run.messages) for run in scope_context.session.runs or [])
        return _prepared_team_execution_context(final_prompt="Analyze this.")

    with (
        patch("mindroom.teams.create_agent", return_value=fake_agent),
        patch("mindroom.teams.resolve_agent_knowledge_access", return_value=KnowledgeResolution(knowledge=None)),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
        patch(
            "mindroom.teams.prepare_bound_team_run_context",
            new=AsyncMock(side_effect=fake_prepare_bound_team_execution_context),
        ),
    ):
        response = await team_response(
            agent_names=["general"],
            mode=TeamMode.COORDINATE,
            message="Analyze this.",
            turn_recorder=_team_turn_recorder("Analyze this."),
            orchestrator=orchestrator,
            execution_identity=None,
            session_id="session-queued",
        )

    assert "Recovered team response" in response
    with open_bound_scope_session_context(
        agents=[fake_agent],
        session_id="session-queued",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=None,
    ) as scope_context:
        assert scope_context is not None
        assert scope_context.session is not None
        assert not any(_has_queued_notice(run.messages) for run in scope_context.session.runs or [])


@pytest.mark.asyncio
async def test_prepare_materialized_team_execution_scrubs_queued_notices_when_called_directly() -> None:
    """Shared team preparation should scrub loaded queued notices even outside team_response helpers."""
    config = _build_test_config()
    runtime_paths = runtime_paths_for(config)
    fake_agent = _make_test_agent("GeneralAgent")

    with open_bound_scope_session_context(
        agents=[fake_agent],
        session_id="session-helper-scrub",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=None,
        create_session_if_missing=True,
    ) as scope_context:
        assert scope_context is not None
        assert scope_context.session is not None
        persisted_team = _make_test_team(name="General Team", team_id=scope_context.session.team_id)
        persisted_team.db = scope_context.storage
        _cleanup_and_store(
            persisted_team,
            TeamRunOutput(
                run_id="run-1",
                team_id=scope_context.session.team_id,
                team_name="General Team",
                session_id="session-helper-scrub",
                messages=[_queued_notice_message()],
                status=RunStatus.completed,
            ),
            scope_context.session,
        )

    with open_bound_scope_session_context(
        agents=[fake_agent],
        session_id="session-helper-scrub",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=None,
    ) as scope_context:
        assert scope_context is not None
        assert scope_context.session is not None
        mock_team = _make_test_team(name="General Team", team_id=scope_context.session.team_id)

        async def fake_prepare_bound_team_execution_context(**kwargs: object) -> PreparedExecutionContext:
            prepared_scope_context = kwargs["scope_context"]
            assert prepared_scope_context is not None
            assert prepared_scope_context.session is not None
            assert not any(_has_queued_notice(run.messages) for run in prepared_scope_context.session.runs or [])
            return _prepared_team_execution_context(final_prompt="Analyze this.")

        with patch(
            "mindroom.execution_preparation.prepare_bound_team_execution_context",
            new=AsyncMock(side_effect=fake_prepare_bound_team_execution_context),
        ):
            await _prepare_materialized_team_execution(
                scope_context=scope_context,
                agents=[fake_agent],
                team=mock_team,
                message="Analyze this.",
                thread_history=[],
                config=config,
                runtime_paths=runtime_paths,
                active_model_name=None,
                room_id=None,
                thread_id=None,
                requester_id=None,
                correlation_id=None,
                reply_to_event_id=None,
                active_event_ids=frozenset(),
                response_sender_id=None,
                current_sender_id=None,
                compaction_outcomes_collector=None,
                configured_team_name=None,
            )


@pytest.mark.asyncio
async def test_prepare_materialized_team_execution_forwards_explicit_thread_history_render_limits() -> None:
    """Shared team preparation should forward caller-provided fallback-history caps."""
    config = _build_test_config()
    runtime_paths = runtime_paths_for(config)
    fake_agent = _make_test_agent("GeneralAgent")
    mock_team = _make_test_team(name="General Team")

    async def fake_prepare_bound_team_execution_context(**kwargs: object) -> PreparedExecutionContext:
        assert kwargs["thread_history_render_limits"] == ThreadHistoryRenderLimits(
            max_messages=30,
            max_message_length=200,
            missing_sender_label="Unknown",
        )
        return _prepared_team_execution_context(final_prompt="Analyze this.")

    with patch(
        "mindroom.teams.prepare_bound_team_run_context",
        new=AsyncMock(side_effect=fake_prepare_bound_team_execution_context),
    ):
        await _prepare_materialized_team_execution(
            scope_context=None,
            agents=[fake_agent],
            team=mock_team,
            message="Analyze this.",
            thread_history=[],
            config=config,
            runtime_paths=runtime_paths,
            active_model_name=None,
            room_id=None,
            thread_id=None,
            requester_id=None,
            correlation_id=None,
            reply_to_event_id=None,
            active_event_ids=frozenset(),
            response_sender_id=None,
            current_sender_id=None,
            compaction_outcomes_collector=None,
            configured_team_name=None,
            thread_history_render_limits=ThreadHistoryRenderLimits(
                max_messages=30,
                max_message_length=200,
                missing_sender_label="Unknown",
            ),
        )


@pytest.mark.asyncio
async def test_prepare_materialized_team_execution_appends_system_enrichment_context() -> None:
    """Transient team system context should not replace existing configured context."""
    config = _build_test_config()
    runtime_paths = runtime_paths_for(config)
    fake_agent = _make_test_agent("GeneralAgent")
    fake_agent.additional_context = "member configured context"
    mock_team = _make_test_team(name="General Team")
    mock_team.additional_context = "team configured context"

    async def fake_prepare_bound_team_execution_context(**_kwargs: object) -> PreparedExecutionContext:
        return _prepared_team_execution_context(final_prompt="Analyze this.")

    with patch(
        "mindroom.teams.prepare_bound_team_run_context",
        new=AsyncMock(side_effect=fake_prepare_bound_team_execution_context),
    ):
        await _prepare_materialized_team_execution(
            scope_context=None,
            agents=[fake_agent],
            team=mock_team,
            message="Analyze this.",
            thread_history=[],
            config=config,
            runtime_paths=runtime_paths,
            active_model_name=None,
            reply_to_event_id=None,
            active_event_ids=frozenset(),
            response_sender_id=None,
            current_sender_id=None,
            room_id=None,
            thread_id=None,
            requester_id=None,
            correlation_id=None,
            compaction_outcomes_collector=None,
            configured_team_name=None,
            system_enrichment_items=(EnrichmentItem(key="weather", text="72F and sunny"),),
        )

    assert mock_team.additional_context.startswith("team configured context\n\n")
    assert "weather" in mock_team.additional_context
    assert fake_agent.additional_context.startswith("member configured context\n\n")
    assert "weather" in fake_agent.additional_context


@pytest.mark.asyncio
async def test_prepare_materialized_team_execution_carries_compaction_metadata_and_timing() -> None:
    """Team preparation should preserve prepared-context metadata and timing diagnostics."""
    config = _build_test_config()
    runtime_paths = runtime_paths_for(config)
    fake_agent = _make_test_agent("GeneralAgent")
    mock_team = _make_test_team(name="General Team")
    timing = DispatchPipelineTiming(source_event_id="$event", room_id="!room:localhost")
    decision = CompactionDecision(
        mode="none",
        reason="within_hard_budget",
        current_history_tokens=12_001,
        trigger_budget_tokens=10_000,
        hard_budget_tokens=20_000,
        fitted_replay_tokens=9_000,
    )

    async def fake_prepare_bound_team_run_context(**kwargs: object) -> PreparedExecutionContext:
        assert kwargs["pipeline_timing"] is timing
        return _prepared_team_execution_context(
            final_prompt="Analyze this.",
            compaction_decision=decision,
            compaction_reply_outcome="none",
            prepared_context_tokens=12_345,
        )

    with patch(
        "mindroom.teams.prepare_bound_team_run_context",
        new=AsyncMock(side_effect=fake_prepare_bound_team_run_context),
    ):
        prepared = await _prepare_materialized_team_execution(
            scope_context=None,
            agents=[fake_agent],
            team=mock_team,
            message="Analyze this.",
            thread_history=[],
            config=config,
            runtime_paths=runtime_paths,
            active_model_name=None,
            reply_to_event_id="$event",
            active_event_ids=frozenset(),
            response_sender_id=None,
            current_sender_id=None,
            room_id="!room:localhost",
            thread_id=None,
            requester_id=None,
            correlation_id=None,
            compaction_outcomes_collector=None,
            configured_team_name=None,
            pipeline_timing=timing,
        )

    assert prepared.run_metadata is not None
    ai_metadata = prepared.run_metadata[AI_RUN_METADATA_KEY]
    assert ai_metadata["prepared_context"] == {"tokens": 12_345}
    assert ai_metadata["compaction"]["decision"] == "none"
    assert ai_metadata["compaction"]["outcome"] == "none"
    assert timing.metadata["compaction_decision"] == "none"
    assert timing.metadata["prepared_context_tokens"] == 12_345


@pytest.mark.asyncio
async def test_prepare_bound_team_execution_context_uses_team_renderer_for_trimmed_fallback_prompt() -> None:
    """Fallback planning should use the team renderer after applying thread-history caps."""
    config = _build_test_config()
    runtime_paths = runtime_paths_for(config)
    fake_agent = _make_test_agent("GeneralAgent")
    mock_team = _make_test_team(name="General Team")
    captured_prompts: list[tuple[str, str | None]] = []

    def fake_estimate_static_tokens(
        team: AgnoTeam,
        *,
        full_prompt: str,
    ) -> int:
        assert team is mock_team
        captured_prompts.append((full_prompt, None))
        return 0

    with patch(
        "mindroom.execution_preparation.estimate_preparation_static_tokens_for_team",
        side_effect=fake_estimate_static_tokens,
    ):
        prepared = await prepare_bound_team_execution_context(
            scope_context=None,
            agents=[fake_agent],
            team=mock_team,
            prompt="Analyze this.",
            thread_history=[
                make_visible_message(event_id="event-1", sender="user", body="Older user message"),
                make_visible_message(
                    event_id="event-2",
                    sender="@mindroom_team:example.org",
                    body="Previous team reply",
                ),
            ],
            runtime_paths=runtime_paths,
            config=config,
            team_name=None,
            active_model_name=None,
            active_context_window=None,
            response_sender_id="@mindroom_team:example.org",
            thread_history_render_limits=ThreadHistoryRenderLimits(
                max_messages=1,
                max_message_length=200,
                missing_sender_label="Unknown",
            ),
        )

    assert tuple((message.role, message.content) for message in prepared.messages) == (
        ("assistant", "Previous team reply"),
        ("user", "Analyze this."),
    )
    assert captured_prompts == [
        ("Analyze this.", None),
        ("assistant: Previous team reply\n\nAnalyze this.", None),
    ]


@pytest.mark.asyncio
async def test_team_response_scrubs_queued_notices_after_run_exception() -> None:
    """Failed team runs should still remove hidden queued-message notices from history."""
    config = _build_test_config()
    runtime_paths = runtime_paths_for(config)
    orchestrator = MagicMock()
    orchestrator.config = config
    orchestrator.runtime_paths = runtime_paths
    orchestrator.knowledge_managers = {}
    orchestrator.agent_bots = {"general": MagicMock()}

    fake_agent = _make_test_agent("GeneralAgent")
    with open_bound_scope_session_context(
        agents=[fake_agent],
        session_id="session-queued-error",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=None,
        create_session_if_missing=True,
    ) as scope_context:
        assert scope_context is not None
        assert scope_context.session is not None
        scope_context.storage.upsert_session(scope_context.session)

    prepared_scope_context = None
    team_id = "team_general"
    mock_team = _make_test_team(name="General Team", team_id=team_id)
    boom_error = "boom"

    async def fake_arun(*_args: object, **_kwargs: object) -> TeamRunOutput:
        assert prepared_scope_context is not None
        mock_team.db = prepared_scope_context.storage
        assert prepared_scope_context.session is not None
        _cleanup_and_store(
            mock_team,
            TeamRunOutput(
                run_id="run-error",
                team_id=team_id,
                team_name="General Team",
                session_id="session-queued-error",
                content="intermediate response",
                messages=[_queued_notice_message()],
                status=RunStatus.completed,
            ),
            prepared_scope_context.session,
        )
        raise RuntimeError(boom_error)

    mock_team.arun = AsyncMock(side_effect=fake_arun)

    async def fake_prepare_bound_team_execution_context(**kwargs: object) -> PreparedExecutionContext:
        nonlocal prepared_scope_context
        scope_context = kwargs["scope_context"]
        assert scope_context is not None
        assert scope_context.session is not None
        prepared_scope_context = scope_context
        assert not any(_has_queued_notice(run.messages) for run in scope_context.session.runs or [])
        return _prepared_team_execution_context(final_prompt="Analyze this.")

    with (
        patch("mindroom.teams.create_agent", return_value=fake_agent),
        patch("mindroom.teams.resolve_agent_knowledge_access", return_value=KnowledgeResolution(knowledge=None)),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
        patch(
            "mindroom.teams.prepare_bound_team_run_context",
            new=AsyncMock(side_effect=fake_prepare_bound_team_execution_context),
        ),
    ):
        response = await team_response(
            agent_names=["general"],
            mode=TeamMode.COORDINATE,
            message="Analyze this.",
            orchestrator=orchestrator,
            execution_identity=None,
            session_id="session-queued-error",
            turn_recorder=_team_turn_recorder("Analyze this."),
        )

    assert "boom" in response
    with open_bound_scope_session_context(
        agents=[fake_agent],
        session_id="session-queued-error",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=None,
    ) as scope_context:
        assert scope_context is not None
        assert scope_context.session is not None
        assert not any(_has_queued_notice(run.messages) for run in scope_context.session.runs or [])


@pytest.mark.asyncio
async def test_team_response_stream_scrubs_queued_notices_after_stream_exception() -> None:
    """Streaming team failures should still scrub hidden queued-message notices."""
    config = _build_test_config()
    runtime_paths = runtime_paths_for(config)
    orchestrator = MagicMock()
    orchestrator.config = config
    orchestrator.runtime_paths = runtime_paths
    orchestrator.knowledge_managers = {}
    orchestrator.agent_bots = {"general": MagicMock(running=True)}

    fake_agent = _make_test_agent("GeneralAgent")
    with open_bound_scope_session_context(
        agents=[fake_agent],
        session_id="session-stream-queued-error",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=None,
        create_session_if_missing=True,
    ) as scope_context:
        assert scope_context is not None
        assert scope_context.session is not None
        scope_context.storage.upsert_session(scope_context.session)

    prepared_scope_context = None
    team_id = "team_general"
    mock_team = _make_test_team(name="General Team", team_id=team_id)
    boom_error = "boom"

    async def failing_raw_stream() -> AsyncIterator[object]:
        if False:
            yield None
        assert prepared_scope_context is not None
        mock_team.db = prepared_scope_context.storage
        assert prepared_scope_context.session is not None
        _cleanup_and_store(
            mock_team,
            TeamRunOutput(
                run_id="run-stream-error",
                team_id=team_id,
                team_name="General Team",
                session_id="session-stream-queued-error",
                content="intermediate response",
                messages=[_queued_notice_message()],
                status=RunStatus.completed,
            ),
            prepared_scope_context.session,
        )
        raise RuntimeError(boom_error)

    async def fake_prepare_bound_team_execution_context(**kwargs: object) -> PreparedExecutionContext:
        nonlocal prepared_scope_context
        scope_context = kwargs["scope_context"]
        assert scope_context is not None
        assert scope_context.session is not None
        prepared_scope_context = scope_context
        assert not any(_has_queued_notice(run.messages) for run in scope_context.session.runs or [])
        return _prepared_team_execution_context(final_prompt="Analyze this.")

    async def fake_team_response_stream_raw(**_kwargs: object) -> AsyncIterator[object]:
        return failing_raw_stream()

    with (
        patch("mindroom.teams.create_agent", return_value=fake_agent),
        patch("mindroom.teams.resolve_agent_knowledge_access", return_value=KnowledgeResolution(knowledge=None)),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
        patch(
            "mindroom.teams.prepare_bound_team_run_context",
            new=AsyncMock(side_effect=fake_prepare_bound_team_execution_context),
        ),
        patch(
            "mindroom.teams._team_response_stream_raw",
            new=AsyncMock(side_effect=fake_team_response_stream_raw),
        ),
    ):
        chunks = [
            chunk
            async for chunk in team_response_stream(
                agent_ids=[config.get_ids(runtime_paths)["general"]],
                mode=TeamMode.COORDINATE,
                message="Analyze this.",
                orchestrator=orchestrator,
                execution_identity=None,
                session_id="session-stream-queued-error",
                turn_recorder=_team_turn_recorder("Analyze this."),
            )
        ]

    assert "boom" in "".join(chunk.content if hasattr(chunk, "content") else str(chunk) for chunk in chunks)
    with open_bound_scope_session_context(
        agents=[fake_agent],
        session_id="session-stream-queued-error",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=None,
    ) as scope_context:
        assert scope_context is not None
        assert scope_context.session is not None
        assert not any(_has_queued_notice(run.messages) for run in scope_context.session.runs or [])


@pytest.mark.asyncio
async def test_team_response_persists_seen_event_ids_for_matrix_runs() -> None:
    """Successful Matrix team runs should mark the triggering and unseen events as consumed."""
    config = _build_test_config()
    runtime_paths = runtime_paths_for(config)
    orchestrator = MagicMock()
    orchestrator.config = config
    orchestrator.runtime_paths = runtime_paths
    orchestrator.knowledge_managers = {}
    orchestrator.agent_bots = {"general": MagicMock()}

    mock_team = _make_test_team()
    mock_team.arun = AsyncMock(return_value=TeamRunOutput(content="Recovered team response"))
    fake_agent = _make_test_agent("GeneralAgent")
    with open_bound_scope_session_context(
        agents=[fake_agent],
        session_id="session-456",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=None,
        create_session_if_missing=True,
    ) as scope_context:
        assert scope_context is not None
        assert scope_context.session is not None
        scope_context.storage.upsert_session(scope_context.session)

    with (
        patch("mindroom.teams.create_agent", return_value=fake_agent),
        patch("mindroom.teams.resolve_agent_knowledge_access", return_value=KnowledgeResolution(knowledge=None)),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
        patch("mindroom.teams.prepare_bound_team_run_context", new_callable=AsyncMock) as mock_prepare,
    ):
        mock_prepare.return_value = _prepared_team_execution_context(
            final_prompt="Analyze this.",
            replays_persisted_history=True,
            unseen_event_ids=["event-1"],
        )
        await team_response(
            agent_names=["general"],
            mode=TeamMode.COORDINATE,
            message="Analyze this.",
            turn_recorder=_team_turn_recorder("Analyze this."),
            thread_history=[
                make_visible_message(event_id="event-1", sender="user", body="Fresh follow-up"),
                make_visible_message(event_id="event-2", sender="user", body="Current message body"),
            ],
            orchestrator=orchestrator,
            execution_identity=None,
            session_id="session-456",
            reply_to_event_id="event-2",
            response_sender_id="@mindroom_team:example.org",
        )

    with open_bound_scope_session_context(
        agents=[fake_agent],
        session_id="session-456",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=None,
    ) as scope_context:
        assert scope_context is not None
        assert scope_context.session is not None
        assert read_scope_seen_event_ids(scope_context.session, scope_context.scope) == {
            "event-1",
            "event-2",
        }


@pytest.mark.asyncio
async def test_team_response_passes_run_id_to_team_arun() -> None:
    """Non-streaming team responses should pass an explicit run_id to Agno."""
    config = _build_test_config()
    orchestrator = MagicMock()
    orchestrator.config = config
    orchestrator.runtime_paths = runtime_paths_for(config)
    orchestrator.knowledge_managers = {}
    orchestrator.agent_bots = {"general": MagicMock()}

    mock_team = _make_test_team()
    mock_team.arun = AsyncMock(
        return_value=TeamRunOutput(content="Recovered team response", status=RunStatus.completed),
    )

    fake_agent = _make_test_agent("GeneralAgent")
    with (
        patch("mindroom.teams.create_agent", return_value=fake_agent),
        patch("mindroom.teams.resolve_agent_knowledge_access", return_value=KnowledgeResolution(knowledge=None)),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
    ):
        response = await team_response(
            agent_names=["general"],
            mode=TeamMode.COORDINATE,
            message="Analyze this.",
            turn_recorder=_team_turn_recorder("Analyze this."),
            orchestrator=orchestrator,
            execution_identity=None,
            run_id="run-123",
        )

    assert "Recovered team response" in response
    assert mock_team.arun.await_args.kwargs["run_id"] == "run-123"


@pytest.mark.asyncio
async def test_team_response_raises_cancelled_error_for_cancelled_runs() -> None:
    """Gracefully cancelled team runs should surface as CancelledError."""
    config = _build_test_config()
    orchestrator = MagicMock()
    orchestrator.config = config
    orchestrator.runtime_paths = runtime_paths_for(config)
    orchestrator.knowledge_managers = {}
    orchestrator.agent_bots = {"general": MagicMock()}

    mock_team = _make_test_team()
    mock_team.arun = AsyncMock(
        return_value=TeamRunOutput(
            content="Run run-123 was cancelled",
            status=RunStatus.cancelled,
        ),
    )

    fake_agent = _make_test_agent("GeneralAgent")
    with (
        patch("mindroom.teams.create_agent", return_value=fake_agent),
        patch("mindroom.teams.resolve_agent_knowledge_access", return_value=KnowledgeResolution(knowledge=None)),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
        pytest.raises(asyncio.CancelledError),
    ):
        await team_response(
            agent_names=["general"],
            mode=TeamMode.COORDINATE,
            message="Analyze this.",
            turn_recorder=_team_turn_recorder("Analyze this."),
            orchestrator=orchestrator,
            execution_identity=None,
            run_id="run-123",
        )


@pytest.mark.asyncio
async def test_team_response_records_interrupted_snapshot_for_cancelled_runs() -> None:
    """Cancelled team runs should capture canonical replay state in the lifecycle recorder."""
    config = _build_test_config()
    runtime_paths = runtime_paths_for(config)
    orchestrator = MagicMock()
    orchestrator.config = config
    orchestrator.runtime_paths = runtime_paths
    orchestrator.knowledge_managers = {}
    orchestrator.agent_bots = {"general": MagicMock()}

    fake_agent = _make_test_agent("GeneralAgent")
    mock_team = _make_test_team()
    mock_team.arun = AsyncMock(
        return_value=TeamRunOutput(
            run_id="run-123",
            session_id="session-team",
            content="Run run-123 was cancelled",
            member_responses=[
                RunOutput(
                    agent_name="GeneralAgent",
                    content="Half done",
                    tools=[
                        ToolExecution(
                            tool_name="run_shell_command",
                            tool_args={"cmd": "pwd"},
                            result="/app",
                        ),
                    ],
                    status=RunStatus.completed,
                ),
            ],
            status=RunStatus.cancelled,
        ),
    )

    recorder = TurnRecorder(user_message="Analyze this.")

    with (
        patch("mindroom.teams.create_agent", return_value=fake_agent),
        patch("mindroom.teams.resolve_agent_knowledge_access", return_value=KnowledgeResolution(knowledge=None)),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
        pytest.raises(asyncio.CancelledError),
    ):
        await team_response(
            agent_names=["general"],
            mode=TeamMode.COORDINATE,
            message="Analyze this.",
            turn_recorder=recorder,
            orchestrator=orchestrator,
            execution_identity=None,
            session_id="session-team",
            run_id="run-123",
            reply_to_event_id="e1",
        )

    snapshot = recorder.interrupted_snapshot()
    assert snapshot.user_message == "Analyze this."
    assert render_interrupted_replay_content(snapshot) == (
        "**GeneralAgent**: Half done\n\n\n"
        "*No team consensus - showing individual responses only*\n\n"
        "[tool:run_shell_command completed]\n"
        "  args: cmd=pwd\n"
        "  result: /app\n\n"
        "[interrupted]"
    )


@pytest.mark.asyncio
async def test_team_response_records_incomplete_cancelled_tools_as_interrupted() -> None:
    """Cancelled non-streaming team runs must keep unfinished tools in the recorder snapshot."""
    config = _build_test_config()
    runtime_paths = runtime_paths_for(config)
    orchestrator = MagicMock()
    orchestrator.config = config
    orchestrator.runtime_paths = runtime_paths
    orchestrator.knowledge_managers = {}
    orchestrator.agent_bots = {"general": MagicMock()}

    fake_agent = _make_test_agent("GeneralAgent")
    mock_team = _make_test_team()
    mock_team.arun = AsyncMock(
        return_value=TeamRunOutput(
            run_id="run-123",
            session_id="session-team",
            content="Run run-123 was cancelled",
            member_responses=[
                RunOutput(
                    agent_name="GeneralAgent",
                    content="Half done",
                    tools=[
                        ToolExecution(
                            tool_name="run_shell_command",
                            tool_args={"cmd": "pwd"},
                            result=None,
                        ),
                    ],
                    status=RunStatus.completed,
                ),
            ],
            status=RunStatus.cancelled,
        ),
    )

    recorder = TurnRecorder(user_message="Analyze this.")

    with (
        patch("mindroom.teams.create_agent", return_value=fake_agent),
        patch("mindroom.teams.resolve_agent_knowledge_access", return_value=KnowledgeResolution(knowledge=None)),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
        pytest.raises(asyncio.CancelledError),
    ):
        await team_response(
            agent_names=["general"],
            mode=TeamMode.COORDINATE,
            message="Analyze this.",
            turn_recorder=recorder,
            orchestrator=orchestrator,
            execution_identity=None,
            session_id="session-team",
            run_id="run-123",
            reply_to_event_id="e1",
        )

    snapshot = recorder.interrupted_snapshot()
    assert snapshot.user_message == "Analyze this."
    assert render_interrupted_replay_content(snapshot) == (
        "**GeneralAgent**: Half done\n\n\n"
        "*No team consensus - showing individual responses only*\n\n"
        "[tool:run_shell_command interrupted]\n"
        "  args: cmd=pwd\n"
        "  result: <interrupted before completion>\n\n"
        "[interrupted]"
    )


@pytest.mark.asyncio
async def test_team_response_returns_friendly_error_for_error_status() -> None:
    """Errored TeamRunOutput values must not be formatted as successful team replies."""
    config = _build_test_config()
    orchestrator = MagicMock()
    orchestrator.config = config
    orchestrator.runtime_paths = runtime_paths_for(config)
    orchestrator.knowledge_managers = {}
    orchestrator.agent_bots = {"general": MagicMock()}

    mock_team = _make_test_team()
    mock_team.arun = AsyncMock(
        return_value=TeamRunOutput(content="validation failed in team", status=RunStatus.error),
    )

    fake_agent = _make_test_agent("GeneralAgent")
    with (
        patch("mindroom.teams.create_agent", return_value=fake_agent),
        patch("mindroom.teams.resolve_agent_knowledge_access", return_value=KnowledgeResolution(knowledge=None)),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
        patch(
            "mindroom.teams.get_user_friendly_error_message",
            return_value="friendly-team-error",
        ) as mock_friendly,
    ):
        response = await team_response(
            agent_names=["general"],
            mode=TeamMode.COORDINATE,
            message="Analyze this.",
            turn_recorder=_team_turn_recorder("Analyze this."),
            orchestrator=orchestrator,
            execution_identity=None,
        )

    assert response == "friendly-team-error"
    mock_friendly.assert_called_once()


@pytest.mark.asyncio
async def test_team_response_with_turn_recorder_defers_interrupted_persistence_to_runner() -> None:
    """Lifecycle-owned team calls should record interrupted state without persisting directly."""
    config = _build_test_config()
    runtime_paths = runtime_paths_for(config)
    orchestrator = MagicMock()
    orchestrator.config = config
    orchestrator.runtime_paths = runtime_paths
    orchestrator.knowledge_managers = {}
    orchestrator.agent_bots = {"general": MagicMock(running=True)}

    mock_team = _make_test_team()
    mock_team.arun = AsyncMock(
        return_value=TeamRunOutput(
            content="Run cancelled",
            messages=[Message(role="assistant", content="Half done")],
            tools=[
                ToolExecution(
                    tool_name="run_shell_command",
                    tool_args={"cmd": "pwd"},
                    result="/app",
                ),
            ],
            status=RunStatus.cancelled,
            session_id="session-team",
            run_id="run-123",
            member_responses=[RunOutput(content="Half done", agent_id="general")],
        ),
    )
    recorder = TurnRecorder(user_message="Analyze this.")

    fake_agent = _make_test_agent("GeneralAgent")
    with (
        patch("mindroom.teams.create_agent", return_value=fake_agent),
        patch("mindroom.teams.resolve_agent_knowledge_access", return_value=KnowledgeResolution(knowledge=None)),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
        pytest.raises(asyncio.CancelledError),
    ):
        await team_response(
            agent_names=["general"],
            mode=TeamMode.COORDINATE,
            message="Analyze this.",
            orchestrator=orchestrator,
            execution_identity=None,
            session_id="session-team",
            run_id="run-123",
            reply_to_event_id="e1",
            turn_recorder=recorder,
        )

    with open_bound_scope_session_context(
        agents=[fake_agent],
        session_id="session-team",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=None,
    ) as scope_context:
        assert scope_context is not None
        assert scope_context.session is None

    snapshot = recorder.interrupted_snapshot()
    assert snapshot.user_message == "Analyze this."
    assert [tool.tool_name for tool in snapshot.completed_tools] == ["run_shell_command"]
    assert snapshot.seen_event_ids == ("e1",)


@pytest.mark.asyncio
async def test_team_response_with_turn_recorder_preserves_unseen_event_ids_on_cancellation() -> None:
    """Lifecycle-owned team cancellations must keep unseen-event metadata in the recorder snapshot."""
    config = _build_test_config()
    runtime_paths = runtime_paths_for(config)
    orchestrator = MagicMock()
    orchestrator.config = config
    orchestrator.runtime_paths = runtime_paths
    orchestrator.knowledge_managers = {}
    orchestrator.agent_bots = {"general": MagicMock(running=True)}

    mock_team = _make_test_team()
    mock_team.arun = AsyncMock(
        return_value=TeamRunOutput(
            content="Run cancelled",
            messages=[Message(role="assistant", content="Half done")],
            status=RunStatus.cancelled,
            session_id="session-team",
            run_id="run-123",
            member_responses=[RunOutput(content="Half done", agent_id="general")],
        ),
    )
    recorder = TurnRecorder(user_message="Analyze this.")

    fake_agent = _make_test_agent("GeneralAgent")
    with (
        patch("mindroom.teams.create_agent", return_value=fake_agent),
        patch("mindroom.teams.resolve_agent_knowledge_access", return_value=KnowledgeResolution(knowledge=None)),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
        patch("mindroom.teams.prepare_bound_team_run_context", new_callable=AsyncMock) as mock_prepare,
    ):
        mock_prepare.return_value = _prepared_team_execution_context(
            final_prompt="Analyze this.",
            unseen_event_ids=["e2"],
        )
        with pytest.raises(asyncio.CancelledError):
            await team_response(
                agent_names=["general"],
                mode=TeamMode.COORDINATE,
                message="Analyze this.",
                orchestrator=orchestrator,
                execution_identity=None,
                session_id="session-team",
                run_id="run-123",
                reply_to_event_id="e1",
                turn_recorder=recorder,
            )

    snapshot = recorder.interrupted_snapshot()
    assert snapshot.user_message == "Analyze this."
    assert snapshot.seen_event_ids == ("e1", "e2")


@pytest.mark.asyncio
async def test_team_response_retries_errored_run_output_with_fresh_run_id() -> None:
    """Inline-media team retries must use a fresh Agno run_id after errored output."""
    config = _build_test_config()
    orchestrator = MagicMock()
    orchestrator.config = config
    orchestrator.runtime_paths = runtime_paths_for(config)
    orchestrator.knowledge_managers = {}
    orchestrator.agent_bots = {"general": MagicMock()}

    mock_team = _make_test_team()
    mock_team.arun = AsyncMock(
        side_effect=[
            TeamRunOutput(content="Error code: 500 - audio input is not supported", status=RunStatus.error),
            TeamRunOutput(content="Recovered team response", status=RunStatus.completed),
        ],
    )

    fake_agent = _make_test_agent("GeneralAgent")
    recorder = TurnRecorder(user_message="Analyze this.")
    callback_run_ids: list[str] = []
    with (
        patch("mindroom.teams.create_agent", return_value=fake_agent),
        patch("mindroom.teams.resolve_agent_knowledge_access", return_value=KnowledgeResolution(knowledge=None)),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
    ):
        response = await team_response(
            agent_names=["general"],
            mode=TeamMode.COORDINATE,
            message="Analyze this.",
            turn_recorder=recorder,
            orchestrator=orchestrator,
            execution_identity=None,
            media=MediaInputs(audio=[MagicMock(name="audio_input")]),
            run_id="run-123",
            run_id_callback=lambda current_run_id: (
                callback_run_ids.append(current_run_id),
                recorder.set_run_id(current_run_id),
            ),
        )

    assert "Recovered team response" in response
    first_call = mock_team.arun.await_args_list[0]
    second_call = mock_team.arun.await_args_list[1]
    assert first_call.kwargs["run_id"] == "run-123"
    assert second_call.kwargs["run_id"] is not None
    assert second_call.kwargs["run_id"] != "run-123"
    assert callback_run_ids == [first_call.kwargs["run_id"], second_call.kwargs["run_id"]]


@pytest.mark.asyncio
async def test_team_response_tracks_retry_run_id_after_hard_cancellation() -> None:
    """Lifecycle-owned team cancellation should keep the last retry attempt id on the recorder."""
    config = _build_test_config()
    runtime_paths = runtime_paths_for(config)
    orchestrator = MagicMock()
    orchestrator.config = config
    orchestrator.runtime_paths = runtime_paths
    orchestrator.knowledge_managers = {}
    orchestrator.agent_bots = {"general": MagicMock()}

    fake_agent = _make_test_agent("GeneralAgent")
    recorder = TurnRecorder(user_message="Analyze this.")
    mock_team = _make_test_team()
    callback_run_ids: list[str] = []
    mock_team.arun = AsyncMock(
        side_effect=[
            TeamRunOutput(content="Error code: 500 - audio input is not supported", status=RunStatus.error),
            asyncio.CancelledError(),
        ],
    )

    with (
        patch("mindroom.teams.create_agent", return_value=fake_agent),
        patch("mindroom.teams.resolve_agent_knowledge_access", return_value=KnowledgeResolution(knowledge=None)),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
        patch(
            "mindroom.teams.prepare_bound_team_run_context",
            new=AsyncMock(
                return_value=_prepared_team_execution_context(
                    final_prompt="Analyze this.",
                    prepared_context_tokens=44_000,
                ),
            ),
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await team_response(
            agent_names=["general"],
            mode=TeamMode.COORDINATE,
            message="Analyze this.",
            turn_recorder=recorder,
            orchestrator=orchestrator,
            execution_identity=None,
            session_id="session-team",
            media=MediaInputs(audio=[MagicMock(name="audio_input")]),
            run_id="run-123",
            run_id_callback=lambda current_run_id: (
                callback_run_ids.append(current_run_id),
                recorder.set_run_id(current_run_id),
            ),
        )

    first_call = mock_team.arun.await_args_list[0]
    second_call = mock_team.arun.await_args_list[1]
    assert first_call.kwargs["run_id"] == "run-123"
    assert second_call.kwargs["run_id"] is not None
    assert second_call.kwargs["run_id"] != "run-123"
    assert callback_run_ids == [first_call.kwargs["run_id"], second_call.kwargs["run_id"]]
    assert recorder.run_id == second_call.kwargs["run_id"]
    assert recorder.run_metadata is not None
    assert recorder.run_metadata[AI_RUN_METADATA_KEY]["prepared_context"] == {"tokens": 44_000}


@pytest.mark.asyncio
async def test_team_response_stream_raises_cancelled_error_for_team_run_cancelled_event() -> None:
    """Streaming team cancellation should propagate as CancelledError."""
    config = _build_test_config()
    runtime_paths = runtime_paths_for(config)
    orchestrator = MagicMock()
    orchestrator.config = config
    orchestrator.runtime_paths = runtime_paths
    orchestrator.knowledge_managers = {}
    orchestrator.agent_bots = {"general": MagicMock(running=True)}

    team_members = ResolvedExactTeamMembers(
        requested_agent_names=["general"],
        agents=[],
        display_names=["GeneralAgent"],
        materialized_agent_names={"general"},
        failed_agent_names=[],
    )

    async def fake_stream_raw(*_args: object, **_kwargs: object) -> AsyncIterator[object]:
        assert _kwargs["run_id"] == "run-456"
        yield TeamRunContentEvent(content="partial consensus")
        yield TeamRunCancelledEvent(run_id="run-456", reason="Run run-456 was cancelled")

    team_agent_ids = [
        MatrixID.from_agent(
            "general",
            config.get_domain(runtime_paths),
            runtime_paths,
        ),
    ]
    recorder = TurnRecorder(user_message="Analyze this.")

    with (
        patch(
            "mindroom.teams.resolve_agent_knowledge_access",
            new=MagicMock(return_value=KnowledgeResolution(knowledge=None)),
        ),
        patch("mindroom.teams._materialize_team_members", return_value=team_members),
        patch("mindroom.teams._create_team_instance", return_value=_make_test_team()),
        patch("mindroom.teams._team_response_stream_raw", new=AsyncMock(side_effect=fake_stream_raw)),
    ):

        async def collect_chunks_until_cancelled() -> list[str]:
            return [
                str(chunk)
                async for chunk in team_response_stream(
                    agent_ids=team_agent_ids,
                    message="Analyze this.",
                    turn_recorder=recorder,
                    orchestrator=orchestrator,
                    execution_identity=None,
                    mode=TeamMode.COORDINATE,
                    run_id="run-456",
                )
            ]

        with pytest.raises(asyncio.CancelledError):
            await collect_chunks_until_cancelled()

    streamed_text = [
        str(chunk.content)
        async for chunk in fake_stream_raw(run_id="run-456")
        if isinstance(chunk, TeamRunContentEvent)
    ]
    assert any("partial consensus" in chunk for chunk in streamed_text)


@pytest.mark.asyncio
async def test_team_response_stream_records_hidden_interrupted_tool_state() -> None:
    """Streaming team cancellation should capture hidden completed tools in the lifecycle recorder."""
    config = _build_test_config()
    runtime_paths = runtime_paths_for(config)
    orchestrator = MagicMock()
    orchestrator.config = config
    orchestrator.runtime_paths = runtime_paths
    orchestrator.knowledge_managers = {}
    orchestrator.agent_bots = {"general": MagicMock(running=True)}

    fake_agent = _make_test_agent("GeneralAgent")
    team_members = ResolvedExactTeamMembers(
        requested_agent_names=["general"],
        agents=[fake_agent],
        display_names=["GeneralAgent"],
        materialized_agent_names={"general"},
        failed_agent_names=[],
    )

    async def fake_stream_raw(*_args: object, **_kwargs: object) -> AsyncIterator[object]:
        yield AgentRunContentEvent(agent_name="GeneralAgent", content="Half done")
        yield AgentToolCallStartedEvent(
            agent_name="GeneralAgent",
            tool=ToolExecution(tool_name="run_shell_command", tool_args={"cmd": "pwd"}),
        )
        yield AgentToolCallCompletedEvent(
            agent_name="GeneralAgent",
            tool=ToolExecution(
                tool_name="run_shell_command",
                tool_args={"cmd": "pwd"},
                result="/app",
            ),
        )
        yield TeamRunCancelledEvent(run_id="run-456", session_id="session-team-stream", reason="Run cancelled")

    team_agent_ids = [
        MatrixID.from_agent(
            "general",
            config.get_domain(runtime_paths),
            runtime_paths,
        ),
    ]
    recorder = TurnRecorder(user_message="Analyze this.")

    with (
        patch(
            "mindroom.teams.resolve_agent_knowledge_access",
            new=MagicMock(return_value=KnowledgeResolution(knowledge=None)),
        ),
        patch("mindroom.teams._materialize_team_members", return_value=team_members),
        patch("mindroom.teams._create_team_instance", return_value=_make_test_team()),
        patch("mindroom.teams._team_response_stream_raw", new=AsyncMock(side_effect=fake_stream_raw)),
        pytest.raises(asyncio.CancelledError),
    ):
        async for _chunk in team_response_stream(
            agent_ids=team_agent_ids,
            message="Analyze this.",
            turn_recorder=recorder,
            orchestrator=orchestrator,
            execution_identity=None,
            mode=TeamMode.COORDINATE,
            session_id="session-team-stream",
            run_id="run-456",
            reply_to_event_id="e1",
            show_tool_calls=False,
        ):
            pass

    snapshot = recorder.interrupted_snapshot()
    assert snapshot.user_message == "Analyze this."
    assert render_interrupted_replay_content(snapshot) == (
        "**GeneralAgent**: Half done\n\n\n"
        "*No team consensus - showing individual responses only*\n\n"
        "[tool:run_shell_command completed]\n"
        "  args: cmd=pwd\n"
        "  result: /app\n\n"
        "[interrupted]"
    )


@pytest.mark.asyncio
async def test_team_response_stream_records_interrupted_snapshot_after_external_task_cancel() -> None:
    """External task cancellation should still capture interrupted team replay state."""
    config = _build_test_config()
    runtime_paths = runtime_paths_for(config)
    orchestrator = MagicMock()
    orchestrator.config = config
    orchestrator.runtime_paths = runtime_paths
    orchestrator.knowledge_managers = {}
    orchestrator.agent_bots = {"general": MagicMock(running=True)}

    fake_agent = _make_test_agent("GeneralAgent")
    team_members = ResolvedExactTeamMembers(
        requested_agent_names=["general"],
        agents=[fake_agent],
        display_names=["GeneralAgent"],
        materialized_agent_names={"general"},
        failed_agent_names=[],
    )
    first_chunk_seen = asyncio.Event()
    recorder = TurnRecorder(user_message="Analyze this.")

    async def fake_stream_raw(*_args: object, **_kwargs: object) -> AsyncIterator[object]:
        yield AgentRunContentEvent(agent_name="GeneralAgent", content="Half done")
        await asyncio.sleep(60)

    async def consume_stream() -> None:
        team_agent_ids = [
            MatrixID.from_agent(
                "general",
                config.get_domain(runtime_paths),
                runtime_paths,
            ),
        ]
        async for _chunk in team_response_stream(
            agent_ids=team_agent_ids,
            message="Analyze this.",
            turn_recorder=recorder,
            orchestrator=orchestrator,
            execution_identity=None,
            mode=TeamMode.COORDINATE,
            session_id="session-team-stream",
            run_id="run-999",
            reply_to_event_id="e1",
            show_tool_calls=False,
        ):
            first_chunk_seen.set()

    with (
        patch(
            "mindroom.teams.resolve_agent_knowledge_access",
            new=MagicMock(return_value=KnowledgeResolution(knowledge=None)),
        ),
        patch("mindroom.teams._materialize_team_members", return_value=team_members),
        patch("mindroom.teams._create_team_instance", return_value=_make_test_team()),
        patch("mindroom.teams._team_response_stream_raw", new=AsyncMock(side_effect=fake_stream_raw)),
    ):
        task = asyncio.create_task(consume_stream())
        await first_chunk_seen.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    snapshot = recorder.interrupted_snapshot()
    assert snapshot.user_message == "Analyze this."
    assert render_interrupted_replay_content(snapshot) == (
        "**GeneralAgent**: Half done\n\n\n*No team consensus - showing individual responses only*\n\n[interrupted]"
    )


@pytest.mark.asyncio
async def test_team_response_stream_preserves_pending_tool_scope_for_same_named_tools() -> None:
    """Cancelled team replay must not confuse two members using the same tool name concurrently."""
    runtime_paths = test_runtime_paths(Path(tempfile.mkdtemp()))
    config = bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(display_name="GeneralAgent", rooms=["#test:example.org"]),
                "research": AgentConfig(display_name="ResearchAgent", rooms=["#test:example.org"]),
            },
        ),
        runtime_paths,
    )
    orchestrator = MagicMock()
    orchestrator.config = config
    orchestrator.runtime_paths = runtime_paths
    orchestrator.knowledge_managers = {}
    orchestrator.agent_bots = {
        "general": MagicMock(running=True),
        "research": MagicMock(running=True),
    }

    general_agent = _make_test_agent("GeneralAgent")
    research_agent = _make_test_agent("ResearchAgent")
    team_members = ResolvedExactTeamMembers(
        requested_agent_names=["general", "research"],
        agents=[general_agent, research_agent],
        display_names=["GeneralAgent", "ResearchAgent"],
        materialized_agent_names={"general", "research"},
        failed_agent_names=[],
    )

    async def fake_stream_raw(*_args: object, **_kwargs: object) -> AsyncIterator[object]:
        yield AgentRunContentEvent(agent_name="GeneralAgent", content="General started")
        yield AgentRunContentEvent(agent_name="ResearchAgent", content="Research started")
        yield AgentToolCallStartedEvent(
            agent_name="GeneralAgent",
            tool=ToolExecution(tool_name="run_shell_command", tool_args={"cmd": "pwd"}),
        )
        yield AgentToolCallStartedEvent(
            agent_name="ResearchAgent",
            tool=ToolExecution(tool_name="run_shell_command", tool_args={"cmd": "ls"}),
        )
        yield AgentToolCallCompletedEvent(
            agent_name="GeneralAgent",
            tool=ToolExecution(
                tool_name="run_shell_command",
                tool_args={"cmd": "pwd"},
                result="/app",
            ),
        )
        yield TeamRunCancelledEvent(run_id="run-789", session_id="session-team-stream", reason="Run cancelled")

    team_agent_ids = [
        MatrixID.from_agent(
            "general",
            config.get_domain(runtime_paths),
            runtime_paths,
        ),
        MatrixID.from_agent(
            "research",
            config.get_domain(runtime_paths),
            runtime_paths,
        ),
    ]
    recorder = TurnRecorder(user_message="Analyze this.")

    with (
        patch(
            "mindroom.teams.resolve_agent_knowledge_access",
            new=MagicMock(return_value=KnowledgeResolution(knowledge=None)),
        ),
        patch("mindroom.teams._materialize_team_members", return_value=team_members),
        patch("mindroom.teams._create_team_instance", return_value=_make_test_team()),
        patch("mindroom.teams._team_response_stream_raw", new=AsyncMock(side_effect=fake_stream_raw)),
        pytest.raises(asyncio.CancelledError),
    ):
        async for _chunk in team_response_stream(
            agent_ids=team_agent_ids,
            message="Analyze this.",
            turn_recorder=recorder,
            orchestrator=orchestrator,
            execution_identity=None,
            mode=TeamMode.COORDINATE,
            session_id="session-team-stream",
            run_id="run-789",
            reply_to_event_id="e1",
            show_tool_calls=False,
        ):
            pass

    snapshot = recorder.interrupted_snapshot()
    assert snapshot.user_message == "Analyze this."
    content = render_interrupted_replay_content(snapshot)
    assert "[tool:run_shell_command completed]" in content
    assert "args: cmd=pwd" in content
    assert "[tool:run_shell_command interrupted]" in content
    assert "args: cmd=ls" in content
    assert "args: cmd=pwd\n  result: <interrupted before completion>" not in content


@pytest.mark.asyncio
async def test_team_response_stream_preserves_pending_tool_identity_within_member_scope() -> None:
    """Cancelled team replay must match same-named tools by call identity within one member scope."""
    config = _build_test_config()
    runtime_paths = runtime_paths_for(config)
    orchestrator = MagicMock()
    orchestrator.config = config
    orchestrator.runtime_paths = runtime_paths
    orchestrator.knowledge_managers = {}
    orchestrator.agent_bots = {"general": MagicMock(running=True)}

    general_agent = _make_test_agent("GeneralAgent")
    team_members = ResolvedExactTeamMembers(
        requested_agent_names=["general"],
        agents=[general_agent],
        display_names=["GeneralAgent"],
        materialized_agent_names={"general"},
        failed_agent_names=[],
    )

    async def fake_stream_raw(*_args: object, **_kwargs: object) -> AsyncIterator[object]:
        yield AgentRunContentEvent(agent_name="GeneralAgent", content="General started")
        yield AgentToolCallStartedEvent(
            agent_name="GeneralAgent",
            tool=ToolExecution(tool_call_id="call-1", tool_name="run_shell_command", tool_args={"cmd": "pwd"}),
        )
        yield AgentToolCallStartedEvent(
            agent_name="GeneralAgent",
            tool=ToolExecution(tool_call_id="call-2", tool_name="run_shell_command", tool_args={"cmd": "ls"}),
        )
        yield AgentToolCallCompletedEvent(
            agent_name="GeneralAgent",
            tool=ToolExecution(
                tool_call_id="call-1",
                tool_name="run_shell_command",
                tool_args={"cmd": "pwd"},
                result="/app",
            ),
        )
        yield TeamRunCancelledEvent(run_id="run-789", session_id="session-team-stream", reason="Run cancelled")

    team_agent_ids = [
        MatrixID.from_agent(
            "general",
            config.get_domain(runtime_paths),
            runtime_paths,
        ),
    ]
    recorder = TurnRecorder(user_message="Analyze this.")

    with (
        patch(
            "mindroom.teams.resolve_agent_knowledge_access",
            new=MagicMock(return_value=KnowledgeResolution(knowledge=None)),
        ),
        patch("mindroom.teams._materialize_team_members", return_value=team_members),
        patch("mindroom.teams._create_team_instance", return_value=_make_test_team()),
        patch("mindroom.teams._team_response_stream_raw", new=AsyncMock(side_effect=fake_stream_raw)),
        pytest.raises(asyncio.CancelledError),
    ):
        async for _chunk in team_response_stream(
            agent_ids=team_agent_ids,
            message="Analyze this.",
            turn_recorder=recorder,
            orchestrator=orchestrator,
            execution_identity=None,
            mode=TeamMode.COORDINATE,
            session_id="session-team-stream",
            run_id="run-789",
            reply_to_event_id="e1",
            show_tool_calls=False,
        ):
            pass

    snapshot = recorder.interrupted_snapshot()
    assert snapshot.user_message == "Analyze this."
    assert render_interrupted_replay_content(snapshot) == (
        "**GeneralAgent**: General started\n\n\n"
        "*No team consensus - showing individual responses only*\n\n"
        "[tool:run_shell_command completed]\n"
        "  args: cmd=pwd\n"
        "  result: /app\n"
        "[tool:run_shell_command interrupted]\n"
        "  args: cmd=ls\n"
        "  result: <interrupted before completion>\n\n"
        "[interrupted]"
    )


@pytest.mark.asyncio
async def test_team_response_stream_does_not_retry_after_hidden_tool_progress_on_errored_run_output() -> None:
    """Inline-media retry must stop once hidden tool activity has already started."""
    config = _build_test_config()
    runtime_paths = runtime_paths_for(config)
    orchestrator = MagicMock()
    orchestrator.config = config
    orchestrator.runtime_paths = runtime_paths
    orchestrator.knowledge_managers = {}
    orchestrator.agent_bots = {"general": MagicMock(running=True)}

    general_agent = _make_test_agent("GeneralAgent")
    team_members = ResolvedExactTeamMembers(
        requested_agent_names=["general"],
        agents=[general_agent],
        display_names=["GeneralAgent"],
        materialized_agent_names={"general"},
        failed_agent_names=[],
    )

    attempts = 0

    async def fake_stream_raw(*_args: object, **_kwargs: object) -> AsyncIterator[object]:
        nonlocal attempts
        attempts += 1
        yield AgentToolCallStartedEvent(
            agent_name="GeneralAgent",
            tool=ToolExecution(tool_name="run_shell_command", tool_args={"cmd": "pwd"}),
        )
        yield RunOutput(content="image input is not supported", status=RunStatus.error)

    team_agent_ids = [
        MatrixID.from_agent(
            "general",
            config.get_domain(runtime_paths),
            runtime_paths,
        ),
    ]

    with (
        patch(
            "mindroom.teams.resolve_agent_knowledge_access",
            new=MagicMock(return_value=KnowledgeResolution(knowledge=None)),
        ),
        patch("mindroom.teams._materialize_team_members", return_value=team_members),
        patch("mindroom.teams._create_team_instance", return_value=_make_test_team()),
        patch("mindroom.teams._team_response_stream_raw", new=AsyncMock(side_effect=fake_stream_raw)),
        patch("mindroom.teams.get_user_friendly_error_message", return_value="friendly team error"),
    ):
        chunks = [
            chunk
            async for chunk in team_response_stream(
                agent_ids=team_agent_ids,
                message="Analyze this.",
                turn_recorder=_team_turn_recorder("Analyze this."),
                orchestrator=orchestrator,
                execution_identity=None,
                mode=TeamMode.COORDINATE,
                session_id="session-team-stream",
                run_id="run-789",
                show_tool_calls=False,
                media=MediaInputs(images=(object(),)),
            )
        ]

    assert attempts == 1
    assert chunks == ["friendly team error"]


@pytest.mark.asyncio
async def test_team_response_stream_does_not_retry_after_hidden_tool_progress_on_team_error_event() -> None:
    """Hidden tool progress should block retry on TeamRunErrorEvent too."""
    config = _build_test_config()
    runtime_paths = runtime_paths_for(config)
    orchestrator = MagicMock()
    orchestrator.config = config
    orchestrator.runtime_paths = runtime_paths
    orchestrator.knowledge_managers = {}
    orchestrator.agent_bots = {"general": MagicMock(running=True)}

    general_agent = _make_test_agent("GeneralAgent")
    team_members = ResolvedExactTeamMembers(
        requested_agent_names=["general"],
        agents=[general_agent],
        display_names=["GeneralAgent"],
        materialized_agent_names={"general"},
        failed_agent_names=[],
    )

    attempts = 0

    async def fake_stream_raw(*_args: object, **_kwargs: object) -> AsyncIterator[object]:
        nonlocal attempts
        attempts += 1
        yield AgentToolCallStartedEvent(
            agent_name="GeneralAgent",
            tool=ToolExecution(tool_name="run_shell_command", tool_args={"cmd": "pwd"}),
        )
        yield TeamRunErrorEvent(content="image input is not supported")

    team_agent_ids = [
        MatrixID.from_agent(
            "general",
            config.get_domain(runtime_paths),
            runtime_paths,
        ),
    ]

    with (
        patch(
            "mindroom.teams.resolve_agent_knowledge_access",
            new=MagicMock(return_value=KnowledgeResolution(knowledge=None)),
        ),
        patch("mindroom.teams._materialize_team_members", return_value=team_members),
        patch("mindroom.teams._create_team_instance", return_value=_make_test_team()),
        patch("mindroom.teams._team_response_stream_raw", new=AsyncMock(side_effect=fake_stream_raw)),
        patch("mindroom.teams.get_user_friendly_error_message", return_value="friendly team error"),
    ):
        chunks = [
            chunk
            async for chunk in team_response_stream(
                agent_ids=team_agent_ids,
                message="Analyze this.",
                turn_recorder=_team_turn_recorder("Analyze this."),
                orchestrator=orchestrator,
                execution_identity=None,
                mode=TeamMode.COORDINATE,
                session_id="session-team-stream",
                run_id="run-789",
                show_tool_calls=False,
                media=MediaInputs(images=(object(),)),
            )
        ]

    assert attempts == 1
    assert chunks == ["friendly team error"]


@pytest.mark.asyncio
async def test_team_response_stream_emits_team_run_output_fallback() -> None:
    """A non-streaming provider fallback should still emit one final team response chunk."""
    config = _build_test_config()
    runtime_paths = runtime_paths_for(config)
    orchestrator = MagicMock()
    orchestrator.config = config
    orchestrator.runtime_paths = runtime_paths
    orchestrator.knowledge_managers = {}
    orchestrator.agent_bots = {"general": MagicMock(running=True)}

    team_members = ResolvedExactTeamMembers(
        requested_agent_names=["general"],
        agents=[],
        display_names=["GeneralAgent"],
        materialized_agent_names={"general"},
        failed_agent_names=[],
    )

    async def fake_stream_raw(*_args: object, **_kwargs: object) -> AsyncIterator[object]:
        assert _kwargs["run_id"] == "run-789"
        yield TeamRunOutput(content="Fallback consensus", status=RunStatus.completed)

    team_agent_ids = [
        MatrixID.from_agent(
            "general",
            config.get_domain(runtime_paths),
            runtime_paths,
        ),
    ]

    with (
        patch(
            "mindroom.teams.resolve_agent_knowledge_access",
            new=MagicMock(return_value=KnowledgeResolution(knowledge=None)),
        ),
        patch("mindroom.teams._materialize_team_members", return_value=team_members),
        patch("mindroom.teams._create_team_instance", return_value=_make_test_team()),
        patch("mindroom.teams._team_response_stream_raw", new=AsyncMock(side_effect=fake_stream_raw)),
    ):
        chunks = [
            chunk
            async for chunk in team_response_stream(
                agent_ids=team_agent_ids,
                message="Analyze this.",
                turn_recorder=_team_turn_recorder("Analyze this."),
                orchestrator=orchestrator,
                execution_identity=None,
                mode=TeamMode.COORDINATE,
                run_id="run-789",
            )
        ]

    assert len(chunks) == 1
    assert isinstance(chunks[0], str)
    assert chunks[0].startswith("🤝 **Team Response** (GeneralAgent):")
    assert "Fallback consensus" in chunks[0]


@pytest.mark.asyncio
async def test_team_response_stream_marks_successful_event_stream_completed() -> None:
    """A successful event stream without final TeamRunOutput should complete the turn recorder."""
    config = _build_test_config()
    runtime_paths = runtime_paths_for(config)
    orchestrator = MagicMock()
    orchestrator.config = config
    orchestrator.runtime_paths = runtime_paths
    orchestrator.knowledge_managers = {}
    orchestrator.agent_bots = {"general": MagicMock(running=True)}

    fake_agent = _make_test_agent("GeneralAgent")
    team_members = ResolvedExactTeamMembers(
        requested_agent_names=["general"],
        agents=[fake_agent],
        display_names=["GeneralAgent"],
        materialized_agent_names={"general"},
        failed_agent_names=[],
    )

    async def fake_stream_raw(*_args: object, **_kwargs: object) -> AsyncIterator[object]:
        yield AgentRunContentEvent(agent_name="GeneralAgent", content="Member answer.")
        yield TeamRunContentEvent(content="Consensus answer.")

    team_agent_ids = [
        MatrixID.from_agent(
            "general",
            config.get_domain(runtime_paths),
            runtime_paths,
        ),
    ]
    recorder = TurnRecorder(user_message="Analyze this.")

    with (
        patch("mindroom.teams._materialize_team_members", return_value=team_members),
        patch("mindroom.teams._create_team_instance", return_value=_make_test_team()),
        patch(
            "mindroom.teams.prepare_bound_team_run_context",
            new=AsyncMock(return_value=_prepared_team_execution_context(final_prompt="Analyze this.")),
        ),
        patch("mindroom.teams._team_response_stream_raw", new=AsyncMock(side_effect=fake_stream_raw)),
    ):
        chunks = [
            chunk
            async for chunk in team_response_stream(
                agent_ids=team_agent_ids,
                message="Analyze this.",
                turn_recorder=recorder,
                orchestrator=orchestrator,
                execution_identity=None,
                mode=TeamMode.COORDINATE,
                session_id="session-team-stream",
                run_id="run-456",
                show_tool_calls=False,
            )
        ]

    rendered_chunks = [chunk.content if hasattr(chunk, "content") else str(chunk) for chunk in chunks]
    assert recorder.outcome == "completed"
    assert recorder.assistant_text.startswith("🤝 **Team Response** (GeneralAgent):")
    assert "**GeneralAgent**: Member answer." in recorder.assistant_text
    assert "**Team Consensus**" in recorder.assistant_text
    assert "Consensus answer." in recorder.assistant_text
    assert recorder.completed_tools == []
    assert any("Consensus answer." in chunk for chunk in rendered_chunks)


@pytest.mark.asyncio
async def test_team_response_stream_emits_plain_run_output_fallback_with_team_formatting() -> None:
    """A completed plain RunOutput fallback should still use the normal team response shape."""
    config = _build_test_config()
    runtime_paths = runtime_paths_for(config)
    orchestrator = MagicMock()
    orchestrator.config = config
    orchestrator.runtime_paths = runtime_paths
    orchestrator.knowledge_managers = {}
    orchestrator.agent_bots = {"general": MagicMock(running=True)}

    team_members = ResolvedExactTeamMembers(
        requested_agent_names=["general"],
        agents=[],
        display_names=["GeneralAgent"],
        materialized_agent_names={"general"},
        failed_agent_names=[],
    )

    async def fake_stream_raw(*_args: object, **_kwargs: object) -> AsyncIterator[object]:
        yield RunOutput(
            content=None,
            messages=[
                Message(role="assistant", content="Recovered team response"),
                _queued_notice_message(),
            ],
            status=RunStatus.completed,
        )

    team_agent_ids = [
        MatrixID.from_agent(
            "general",
            config.get_domain(runtime_paths),
            runtime_paths,
        ),
    ]

    with (
        patch(
            "mindroom.teams.resolve_agent_knowledge_access",
            new=MagicMock(return_value=KnowledgeResolution(knowledge=None)),
        ),
        patch("mindroom.teams._materialize_team_members", return_value=team_members),
        patch("mindroom.teams._create_team_instance", return_value=_make_test_team()),
        patch("mindroom.teams._team_response_stream_raw", new=AsyncMock(side_effect=fake_stream_raw)),
    ):
        chunks = [
            chunk
            async for chunk in team_response_stream(
                agent_ids=team_agent_ids,
                message="Analyze this.",
                turn_recorder=_team_turn_recorder("Analyze this."),
                orchestrator=orchestrator,
                execution_identity=None,
                mode=TeamMode.COORDINATE,
            )
        ]

    assert len(chunks) == 1
    assert isinstance(chunks[0], str)
    assert chunks[0].startswith("🤝 **Team Response** (GeneralAgent):")
    assert "Recovered team response" in chunks[0]


@pytest.mark.asyncio
async def test_team_response_stream_raises_cancelled_error_for_team_run_output_fallback() -> None:
    """A cancelled TeamRunOutput fallback should propagate as CancelledError."""
    config = _build_test_config()
    runtime_paths = runtime_paths_for(config)
    orchestrator = MagicMock()
    orchestrator.config = config
    orchestrator.runtime_paths = runtime_paths
    orchestrator.knowledge_managers = {}
    orchestrator.agent_bots = {"general": MagicMock(running=True)}

    team_members = ResolvedExactTeamMembers(
        requested_agent_names=["general"],
        agents=[],
        display_names=["GeneralAgent"],
        materialized_agent_names={"general"},
        failed_agent_names=[],
    )

    async def fake_stream_raw(*_args: object, **_kwargs: object) -> AsyncIterator[object]:
        assert _kwargs["run_id"] == "run-789"
        yield TeamRunOutput(content="Run run-789 was cancelled", status=RunStatus.cancelled)

    team_agent_ids = [
        MatrixID.from_agent(
            "general",
            config.get_domain(runtime_paths),
            runtime_paths,
        ),
    ]

    with (
        patch(
            "mindroom.teams.resolve_agent_knowledge_access",
            new=MagicMock(return_value=KnowledgeResolution(knowledge=None)),
        ),
        patch("mindroom.teams._materialize_team_members", return_value=team_members),
        patch("mindroom.teams._create_team_instance", return_value=_make_test_team()),
        patch("mindroom.teams._team_response_stream_raw", new=AsyncMock(side_effect=fake_stream_raw)),
        pytest.raises(asyncio.CancelledError),
    ):
        async for _chunk in team_response_stream(
            agent_ids=team_agent_ids,
            message="Analyze this.",
            turn_recorder=_team_turn_recorder("Analyze this."),
            orchestrator=orchestrator,
            execution_identity=None,
            mode=TeamMode.COORDINATE,
            run_id="run-789",
        ):
            pass


@pytest.mark.asyncio
async def test_team_response_stream_returns_friendly_error_for_errored_run_output() -> None:
    """Errored TeamRunOutput fallbacks should use the normal team error path."""
    config = _build_test_config()
    runtime_paths = runtime_paths_for(config)
    orchestrator = MagicMock()
    orchestrator.config = config
    orchestrator.runtime_paths = runtime_paths
    orchestrator.knowledge_managers = {}
    orchestrator.agent_bots = {"general": MagicMock(running=True)}

    team_members = ResolvedExactTeamMembers(
        requested_agent_names=["general"],
        agents=[],
        display_names=["GeneralAgent"],
        materialized_agent_names={"general"},
        failed_agent_names=[],
    )

    async def fake_stream_raw(*_args: object, **_kwargs: object) -> AsyncIterator[object]:
        yield TeamRunOutput(content="validation failed in team", status=RunStatus.error)

    team_agent_ids = [
        MatrixID.from_agent(
            "general",
            config.get_domain(runtime_paths),
            runtime_paths,
        ),
    ]

    with (
        patch(
            "mindroom.teams.resolve_agent_knowledge_access",
            new=MagicMock(return_value=KnowledgeResolution(knowledge=None)),
        ),
        patch("mindroom.teams._materialize_team_members", return_value=team_members),
        patch("mindroom.teams._create_team_instance", return_value=_make_test_team()),
        patch("mindroom.teams._team_response_stream_raw", new=AsyncMock(side_effect=fake_stream_raw)),
        patch("mindroom.teams.get_user_friendly_error_message", return_value="friendly-team-error"),
    ):
        chunks = [
            chunk
            async for chunk in team_response_stream(
                agent_ids=team_agent_ids,
                message="Analyze this.",
                turn_recorder=_team_turn_recorder("Analyze this."),
                orchestrator=orchestrator,
                execution_identity=None,
                mode=TeamMode.COORDINATE,
            )
        ]

    assert chunks == ["friendly-team-error"]


@pytest.mark.asyncio
async def test_team_response_stream_returns_friendly_error_for_errored_plain_run_output() -> None:
    """Errored RunOutput fallbacks should use the normal team error path."""
    config = _build_test_config()
    runtime_paths = runtime_paths_for(config)
    orchestrator = MagicMock()
    orchestrator.config = config
    orchestrator.runtime_paths = runtime_paths
    orchestrator.knowledge_managers = {}
    orchestrator.agent_bots = {"general": MagicMock(running=True)}

    team_members = ResolvedExactTeamMembers(
        requested_agent_names=["general"],
        agents=[],
        display_names=["GeneralAgent"],
        materialized_agent_names={"general"},
        failed_agent_names=[],
    )

    async def fake_stream_raw(*_args: object, **_kwargs: object) -> AsyncIterator[object]:
        yield RunOutput(content="validation failed in team", status=RunStatus.error)

    team_agent_ids = [
        MatrixID.from_agent(
            "general",
            config.get_domain(runtime_paths),
            runtime_paths,
        ),
    ]

    with (
        patch(
            "mindroom.teams.resolve_agent_knowledge_access",
            new=MagicMock(return_value=KnowledgeResolution(knowledge=None)),
        ),
        patch("mindroom.teams._materialize_team_members", return_value=team_members),
        patch("mindroom.teams._create_team_instance", return_value=_make_test_team()),
        patch("mindroom.teams._team_response_stream_raw", new=AsyncMock(side_effect=fake_stream_raw)),
        patch("mindroom.teams.get_user_friendly_error_message", return_value="friendly-team-error"),
    ):
        chunks = [
            chunk
            async for chunk in team_response_stream(
                agent_ids=team_agent_ids,
                message="Analyze this.",
                turn_recorder=_team_turn_recorder("Analyze this."),
                orchestrator=orchestrator,
                execution_identity=None,
                mode=TeamMode.COORDINATE,
            )
        ]

    assert chunks == ["friendly-team-error"]


@pytest.mark.asyncio
async def test_team_response_stream_retries_errored_output_with_fresh_run_id() -> None:
    """Streaming inline-media retries must rotate the team run_id after errored fallback output."""
    config = _build_test_config()
    runtime_paths = runtime_paths_for(config)
    orchestrator = MagicMock()
    orchestrator.config = config
    orchestrator.runtime_paths = runtime_paths
    orchestrator.knowledge_managers = {}
    orchestrator.agent_bots = {"general": MagicMock(running=True)}

    team_members = ResolvedExactTeamMembers(
        requested_agent_names=["general"],
        agents=[],
        display_names=["GeneralAgent"],
        materialized_agent_names={"general"},
        failed_agent_names=[],
    )

    call_run_ids: list[str | None] = []
    callback_run_ids: list[str] = []

    async def fake_stream_raw(*_args: object, **_kwargs: object) -> AsyncIterator[object]:
        call_run_ids.append(_kwargs["run_id"])
        if len(call_run_ids) == 1:
            yield TeamRunOutput(content="Error code: 500 - audio input is not supported", status=RunStatus.error)
            return
        yield TeamRunOutput(content="Recovered consensus", status=RunStatus.completed)

    team_agent_ids = [
        MatrixID.from_agent(
            "general",
            config.get_domain(runtime_paths),
            runtime_paths,
        ),
    ]

    with (
        patch(
            "mindroom.teams.resolve_agent_knowledge_access",
            new=MagicMock(return_value=KnowledgeResolution(knowledge=None)),
        ),
        patch("mindroom.teams._materialize_team_members", return_value=team_members),
        patch("mindroom.teams._create_team_instance", return_value=_make_test_team()),
        patch("mindroom.teams._team_response_stream_raw", new=AsyncMock(side_effect=fake_stream_raw)),
    ):
        chunks = [
            chunk
            async for chunk in team_response_stream(
                agent_ids=team_agent_ids,
                message="Analyze this.",
                turn_recorder=_team_turn_recorder("Analyze this."),
                orchestrator=orchestrator,
                execution_identity=None,
                mode=TeamMode.COORDINATE,
                media=MediaInputs(audio=[MagicMock(name="audio_input")]),
                run_id="run-789",
                run_id_callback=callback_run_ids.append,
            )
        ]

    assert len(chunks) == 1
    assert "Recovered consensus" in str(chunks[0])
    assert call_run_ids[0] == "run-789"
    assert call_run_ids[1] is not None
    assert call_run_ids[1] != "run-789"
    assert callback_run_ids == [run_id for run_id in call_run_ids if run_id is not None]


@pytest.mark.asyncio
async def test_team_response_stream_tracks_retry_run_id_after_hard_cancellation() -> None:
    """Streaming team cleanup should keep the final retry attempt id after hard cancellation."""
    config = _build_test_config()
    runtime_paths = runtime_paths_for(config)
    orchestrator = MagicMock()
    orchestrator.config = config
    orchestrator.runtime_paths = runtime_paths
    orchestrator.knowledge_managers = {}
    orchestrator.agent_bots = {"general": MagicMock(running=True)}

    fake_agent = _make_test_agent("GeneralAgent")
    team_members = ResolvedExactTeamMembers(
        requested_agent_names=["general"],
        agents=[fake_agent],
        display_names=["GeneralAgent"],
        materialized_agent_names={"general"},
        failed_agent_names=[],
    )

    call_run_ids: list[str | None] = []
    callback_run_ids: list[str] = []
    recorder = TurnRecorder(user_message="Analyze this.")

    async def fake_stream_raw(*_args: object, **_kwargs: object) -> AsyncIterator[object]:
        call_run_ids.append(_kwargs["run_id"])
        if len(call_run_ids) == 1:
            yield TeamRunErrorEvent(content="Error code: 500 - audio input is not supported")
            return
        raise asyncio.CancelledError
        yield ""  # pragma: no cover

    team_agent_ids = [
        MatrixID.from_agent(
            "general",
            config.get_domain(runtime_paths),
            runtime_paths,
        ),
    ]

    with (
        patch(
            "mindroom.teams.resolve_agent_knowledge_access",
            new=MagicMock(return_value=KnowledgeResolution(knowledge=None)),
        ),
        patch("mindroom.teams._materialize_team_members", return_value=team_members),
        patch("mindroom.teams._create_team_instance", return_value=_make_test_team()),
        patch("mindroom.teams._team_response_stream_raw", new=AsyncMock(side_effect=fake_stream_raw)),
        patch(
            "mindroom.teams.prepare_bound_team_run_context",
            new=AsyncMock(
                return_value=_prepared_team_execution_context(
                    final_prompt="Analyze this.",
                    prepared_context_tokens=55_000,
                ),
            ),
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        _chunks = [
            chunk
            async for chunk in team_response_stream(
                agent_ids=team_agent_ids,
                message="Analyze this.",
                turn_recorder=recorder,
                orchestrator=orchestrator,
                execution_identity=None,
                mode=TeamMode.COORDINATE,
                session_id="session-team-stream",
                media=MediaInputs(audio=[MagicMock(name="audio_input")]),
                run_id="run-789",
                run_id_callback=lambda current_run_id: (
                    callback_run_ids.append(current_run_id),
                    recorder.set_run_id(current_run_id),
                ),
            )
        ]

    assert call_run_ids[0] == "run-789"
    assert call_run_ids[1] is not None
    assert call_run_ids[1] != "run-789"
    assert callback_run_ids == [run_id for run_id in call_run_ids if run_id is not None]
    assert recorder.run_id == call_run_ids[1]
    assert recorder.run_metadata is not None
    assert recorder.run_metadata[AI_RUN_METADATA_KEY]["prepared_context"] == {"tokens": 55_000}


@pytest.mark.asyncio
async def test_team_response_stream_retry_scrubs_queued_notice_before_second_attempt() -> None:
    """Streaming retries should scrub queued notices from the loaded team session before retrying."""
    config = _build_test_config()
    runtime_paths = runtime_paths_for(config)
    orchestrator = MagicMock()
    orchestrator.config = config
    orchestrator.runtime_paths = runtime_paths
    orchestrator.knowledge_managers = {}
    orchestrator.agent_bots = {"general": MagicMock(running=True)}
    fake_agent = _make_test_agent("GeneralAgent")
    with open_bound_scope_session_context(
        agents=[fake_agent],
        session_id="session-stream-retry-clean",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=None,
        create_session_if_missing=True,
    ) as scope_context:
        assert scope_context is not None
        assert scope_context.session is not None
        scope_context.storage.upsert_session(scope_context.session)
    mock_team = _make_test_team(name="General Team")
    prepared_scope_context = None
    attempts = 0

    async def fake_prepare_bound_team_execution_context(**kwargs: object) -> PreparedExecutionContext:
        nonlocal prepared_scope_context
        scope_context = kwargs["scope_context"]
        assert scope_context is not None
        assert scope_context.session is not None
        prepared_scope_context = scope_context
        return _prepared_team_execution_context(final_prompt="Analyze this.")

    async def fake_stream_raw(*_args: object, **_kwargs: object) -> AsyncIterator[object]:
        nonlocal attempts
        attempts += 1
        assert prepared_scope_context is not None
        assert prepared_scope_context.session is not None
        mock_team.db = prepared_scope_context.storage
        team_id = prepared_scope_context.session.team_id
        assert team_id is not None
        if attempts == 1:
            errored_output = TeamRunOutput(
                run_id="run-1",
                team_id=team_id,
                team_name="General Team",
                session_id="session-stream-retry-clean",
                content="Error code: 500 - audio input is not supported",
                messages=[_queued_notice_message()],
                status=RunStatus.error,
            )
            _cleanup_and_store(mock_team, errored_output, prepared_scope_context.session)
            yield errored_output
            return
        assert not any(_has_queued_notice(run.messages) for run in prepared_scope_context.session.runs or [])
        yield TeamRunOutput(
            run_id="run-2",
            team_id=team_id,
            team_name="General Team",
            session_id="session-stream-retry-clean",
            content="Recovered streamed response",
            status=RunStatus.completed,
        )

    with (
        patch("mindroom.teams.create_agent", return_value=fake_agent),
        patch("mindroom.teams.resolve_agent_knowledge_access", return_value=KnowledgeResolution(knowledge=None)),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
        patch(
            "mindroom.teams.prepare_bound_team_run_context",
            new=AsyncMock(side_effect=fake_prepare_bound_team_execution_context),
        ),
        patch("mindroom.teams._team_response_stream_raw", new=AsyncMock(side_effect=fake_stream_raw)),
    ):
        chunks = [
            chunk
            async for chunk in team_response_stream(
                agent_ids=[config.get_ids(runtime_paths_for(config))["general"]],
                message="Analyze this.",
                turn_recorder=_team_turn_recorder("Analyze this."),
                orchestrator=orchestrator,
                execution_identity=None,
                session_id="session-stream-retry-clean",
                media=MediaInputs(audio=[MagicMock(name="audio_input")]),
            )
        ]

    assert attempts == 2
    assert len(chunks) == 1
    assert "Recovered streamed response" in str(chunks[0])


@pytest.mark.asyncio
async def test_team_stream_raw_surfaces_setup_error_as_team_run_error_event() -> None:
    """Raw stream should surface setup failures as TeamRunErrorEvent for outer retry handling."""
    config = _build_test_config()
    orchestrator = MagicMock()
    orchestrator.config = config
    orchestrator.runtime_paths = runtime_paths_for(config)
    orchestrator.knowledge_managers = {}
    orchestrator.agent_bots = {"general": MagicMock()}

    media_validation_error = "Error code: 500 - audio input is not supported"

    mock_team = _make_test_team()
    mock_team.arun = MagicMock(side_effect=Exception(media_validation_error))
    audio_input = MagicMock(name="audio_input")

    fake_agent = _make_test_agent("GeneralAgent")
    with (
        patch("mindroom.teams.create_agent", return_value=fake_agent),
        patch("mindroom.teams.resolve_agent_knowledge_access", return_value=KnowledgeResolution(knowledge=None)),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
    ):
        team_members = _materialize_team_members(["general"], orchestrator, None)
        raw_stream = await _team_response_stream_raw(
            team=mock_team,
            team_members=team_members,
            prompt="Analyze this.",
            media=MediaInputs(audio=[audio_input]),
        )
        events = [event async for event in raw_stream]

    assert mock_team.arun.call_count == 1
    assert len(events) == 1
    assert isinstance(events[0], TeamRunErrorEvent)
    assert events[0].content == media_validation_error


@pytest.mark.asyncio
async def test_team_response_rejects_missing_materialized_members() -> None:
    """Exact team execution should reject when one requested member cannot be materialized."""
    runtime_paths = test_runtime_paths(Path(tempfile.mkdtemp()))
    config = bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(display_name="GeneralAgent", rooms=["#test:example.org"]),
                "research": AgentConfig(display_name="ResearchAgent", rooms=["#test:example.org"]),
            },
        ),
        runtime_paths,
    )
    orchestrator = MagicMock()
    orchestrator.config = config
    orchestrator.runtime_paths = runtime_paths_for(config)
    orchestrator.knowledge_managers = {}
    orchestrator.agent_bots = {"general": MagicMock()}

    with patch("mindroom.teams._create_team_instance") as mock_create_team:
        response = await team_response(
            agent_names=["general", "research"],
            mode=TeamMode.COORDINATE,
            message="Analyze this.",
            turn_recorder=_team_turn_recorder("Analyze this."),
            orchestrator=orchestrator,
            execution_identity=None,
        )

    assert response == "Team request includes agent 'research' that could not be materialized for this request."
    mock_create_team.assert_not_called()


@pytest.mark.asyncio
async def test_team_response_stream_uses_compaction_aware_member_execution() -> None:
    """Streaming team execution should prepare members before invoking the raw stream."""
    config = _build_test_config()
    orchestrator = MagicMock()
    orchestrator.config = config
    orchestrator.runtime_paths = runtime_paths_for(config)
    orchestrator.knowledge_managers = {}
    orchestrator.agent_bots = {"general": MagicMock(running=True)}
    fake_agent = _make_test_agent("GeneralAgent")
    collector: list[object] = []
    mock_team = _make_test_team(name="team")

    async def raw_stream() -> AsyncIterator[object]:
        yield TeamRunOutput(content="Streamed team response")

    with (
        patch("mindroom.teams.create_agent", return_value=fake_agent),
        patch("mindroom.teams.resolve_agent_knowledge_access", return_value=KnowledgeResolution(knowledge=None)),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
        patch("mindroom.teams.prepare_bound_team_run_context", new_callable=AsyncMock) as mock_prepare,
        patch(
            "mindroom.teams._team_response_stream_raw",
            new_callable=AsyncMock,
            return_value=raw_stream(),
        ) as mock_raw,
    ):
        mock_prepare.return_value = _prepared_team_execution_context(final_prompt="Analyze this.")
        chunks = [
            chunk
            async for chunk in team_response_stream(
                agent_ids=[config.get_ids(runtime_paths_for(config))["general"]],
                message="Analyze this.",
                turn_recorder=_team_turn_recorder("Analyze this."),
                orchestrator=orchestrator,
                execution_identity=None,
                session_id="session-123",
                compaction_outcomes_collector=collector,
            )
        ]

    assert len(chunks) == 1
    assert "Streamed team response" in str(chunks[0])
    assert mock_prepare.await_count == 1
    assert mock_prepare.await_args.kwargs["agents"] == [fake_agent]
    assert mock_prepare.await_args.kwargs["team"] is mock_team
    assert mock_prepare.await_args.kwargs["prompt"] == "Analyze this."
    scope_context = mock_prepare.await_args.kwargs["scope_context"]
    assert scope_context is not None
    assert scope_context.scope.kind == "team"
    assert mock_prepare.await_args.kwargs["compaction_outcomes_collector"] is collector
    assert mock_raw.await_count == 1
    assert mock_raw.await_args.kwargs["team"] is mock_team


@pytest.mark.asyncio
async def test_team_response_stream_prefers_persisted_history_over_thread_context_fallback() -> None:
    """Streaming team execution should use the plain prompt and native Agno replay."""
    config = _build_test_config()
    orchestrator = MagicMock()
    orchestrator.config = config
    orchestrator.runtime_paths = runtime_paths_for(config)
    orchestrator.knowledge_managers = {}
    orchestrator.agent_bots = {"general": MagicMock(running=True)}
    fake_agent = _make_test_agent("GeneralAgent")
    mock_team = _make_test_team(name="team")

    async def raw_stream() -> AsyncIterator[object]:
        yield TeamRunOutput(content="Streamed team response")

    with (
        patch("mindroom.teams.create_agent", return_value=fake_agent),
        patch("mindroom.teams.resolve_agent_knowledge_access", return_value=KnowledgeResolution(knowledge=None)),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
        patch("mindroom.teams.prepare_bound_team_run_context", new_callable=AsyncMock) as mock_prepare,
        patch(
            "mindroom.teams._team_response_stream_raw",
            new_callable=AsyncMock,
            return_value=raw_stream(),
        ) as mock_raw,
    ):
        mock_prepare.return_value = _prepared_team_execution_context(
            final_prompt="Analyze this.",
            replays_persisted_history=True,
        )
        chunks = [
            chunk
            async for chunk in team_response_stream(
                agent_ids=[config.get_ids(runtime_paths_for(config))["general"]],
                message="Analyze this.",
                turn_recorder=_team_turn_recorder("Analyze this."),
                thread_history=[make_visible_message(sender="user", body="Old thread context")],
                orchestrator=orchestrator,
                execution_identity=None,
                session_id="session-123",
            )
        ]

    assert len(chunks) == 1
    assert "Streamed team response" in str(chunks[0])
    assert mock_prepare.await_args.kwargs["team"] is mock_team
    assert mock_prepare.await_args.kwargs["prompt"] == "Analyze this."
    assert [message.body for message in mock_prepare.await_args.kwargs["thread_history"]] == [
        "Old thread context",
    ]
    prepared_prompt = mock_raw.await_args.kwargs["prompt"]
    assert prepared_prompt == "Analyze this."


@pytest.mark.asyncio
async def test_team_response_stream_preserves_unseen_matrix_thread_context_with_persisted_history() -> None:
    """Streaming Matrix team runs should include unseen live thread messages with native replay."""
    config = _build_test_config()
    runtime_paths = runtime_paths_for(config)
    orchestrator = MagicMock()
    orchestrator.config = config
    orchestrator.runtime_paths = runtime_paths
    orchestrator.knowledge_managers = {}
    orchestrator.agent_bots = {"general": MagicMock(running=True)}

    fake_agent = _make_test_agent("GeneralAgent")
    with open_bound_scope_session_context(
        agents=[fake_agent],
        session_id="session-789",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=None,
        create_session_if_missing=True,
    ) as scope_context:
        assert scope_context is not None
        assert scope_context.session is not None
        update_scope_seen_event_ids(scope_context.session, scope_context.scope, ["event-1"])
        scope_context.storage.upsert_session(scope_context.session)
    mock_team = _make_test_team(name="team")

    async def raw_stream() -> AsyncIterator[object]:
        yield TeamRunOutput(content="Streamed team response")

    with (
        patch("mindroom.teams.create_agent", return_value=fake_agent),
        patch("mindroom.teams.resolve_agent_knowledge_access", return_value=KnowledgeResolution(knowledge=None)),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
        patch("mindroom.teams.prepare_bound_team_run_context", new_callable=AsyncMock) as mock_prepare,
        patch(
            "mindroom.teams._team_response_stream_raw",
            new_callable=AsyncMock,
            return_value=raw_stream(),
        ) as mock_raw,
    ):
        mock_prepare.return_value = _prepared_team_execution_context(
            final_prompt="Analyze this.",
            replays_persisted_history=True,
            unseen_event_ids=["event-2"],
            context_messages=(Message(role="user", content="user: Fresh follow-up"),),
        )
        chunks = [
            chunk
            async for chunk in team_response_stream(
                agent_ids=[config.get_ids(runtime_paths)["general"]],
                message="Analyze this.",
                turn_recorder=_team_turn_recorder("Analyze this."),
                thread_history=[
                    make_visible_message(event_id="event-1", sender="user", body="Already seen"),
                    make_visible_message(event_id="event-2", sender="user", body="Fresh follow-up"),
                    make_visible_message(event_id="event-3", sender="user", body="Current message body"),
                ],
                orchestrator=orchestrator,
                execution_identity=None,
                session_id="session-789",
                reply_to_event_id="event-3",
                response_sender_id="@mindroom_team:example.org",
            )
        ]

    assert len(chunks) == 1
    assert mock_prepare.await_args.kwargs["team"] is mock_team
    prompt = mock_raw.await_args.kwargs["prompt"]
    assert prompt == "user: Fresh follow-up\n\nAnalyze this."


@pytest.mark.asyncio
async def test_team_response_stream_preserves_assistant_context_in_team_prompt() -> None:
    """Streaming team runs should pass the rendered assistant context string to Agno teams."""
    config = _build_test_config()
    orchestrator = MagicMock()
    orchestrator.config = config
    orchestrator.runtime_paths = runtime_paths_for(config)
    orchestrator.knowledge_managers = {}
    orchestrator.agent_bots = {"general": MagicMock(running=True)}
    fake_agent = _make_test_agent("GeneralAgent")
    mock_team = _make_test_team(name="team")

    async def raw_stream() -> AsyncIterator[object]:
        yield TeamRunOutput(content="Streamed team response")

    with (
        patch("mindroom.teams.create_agent", return_value=fake_agent),
        patch("mindroom.teams.resolve_agent_knowledge_access", return_value=KnowledgeResolution(knowledge=None)),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
        patch("mindroom.teams.prepare_bound_team_run_context", new_callable=AsyncMock) as mock_prepare,
        patch(
            "mindroom.teams._team_response_stream_raw",
            new_callable=AsyncMock,
            return_value=raw_stream(),
        ) as mock_raw,
    ):
        mock_prepare.return_value = _prepared_team_execution_context(
            final_prompt="Analyze this.",
            replays_persisted_history=True,
            context_messages=(Message(role="assistant", content="Previous team reply"),),
        )
        chunks = [
            chunk
            async for chunk in team_response_stream(
                agent_ids=[config.get_ids(runtime_paths_for(config))["general"]],
                message="Analyze this.",
                turn_recorder=_team_turn_recorder("Analyze this."),
                orchestrator=orchestrator,
                execution_identity=None,
                session_id="session-123",
                response_sender_id="@mindroom_team:example.org",
            )
        ]

    assert len(chunks) == 1
    assert "Streamed team response" in str(chunks[0])
    prompt = mock_raw.await_args.kwargs["prompt"]
    assert prompt == "assistant: Previous team reply\n\nAnalyze this."


def test_agno_team_message_normalization_drops_assistant_context() -> None:
    """Agno team list[Message] inputs flatten to user text only, so team callers must pass a string."""
    structured_messages = [
        Message(role="assistant", content="Previous team reply"),
        Message(role="user", content="Current request"),
    ]

    assert get_text_from_message(structured_messages) == "Current request"


@pytest.mark.asyncio
async def test_team_response_rejects_non_running_materialized_members() -> None:
    """Exact team execution should reject members that exist but are not running."""
    runtime_paths = test_runtime_paths(Path(tempfile.mkdtemp()))
    config = bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(display_name="GeneralAgent", rooms=["#test:example.org"]),
                "research": AgentConfig(display_name="ResearchAgent", rooms=["#test:example.org"]),
            },
        ),
        runtime_paths,
    )
    orchestrator = MagicMock()
    orchestrator.config = config
    orchestrator.runtime_paths = runtime_paths_for(config)
    orchestrator.knowledge_managers = {}
    orchestrator.agent_bots = {
        "general": MagicMock(running=True),
        "research": MagicMock(running=False),
    }

    with patch("mindroom.teams._create_team_instance") as mock_create_team:
        response = await team_response(
            agent_names=["general", "research"],
            mode=TeamMode.COORDINATE,
            message="Analyze this.",
            turn_recorder=_team_turn_recorder("Analyze this."),
            orchestrator=orchestrator,
            execution_identity=None,
        )

    assert response == "Team request includes agent 'research' that could not be materialized for this request."
    mock_create_team.assert_not_called()


@pytest.mark.asyncio
async def test_team_response_rejects_request_time_materialization_failure() -> None:
    """Exact team execution should reject when request-time member construction fails."""
    runtime_paths = test_runtime_paths(Path(tempfile.mkdtemp()))
    config = bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(display_name="GeneralAgent", rooms=["#test:example.org"]),
                "research": AgentConfig(display_name="ResearchAgent", rooms=["#test:example.org"]),
            },
        ),
        runtime_paths,
    )
    orchestrator = MagicMock()
    orchestrator.config = config
    orchestrator.runtime_paths = runtime_paths_for(config)
    orchestrator.knowledge_managers = {}
    orchestrator.agent_bots = {"general": MagicMock(), "research": MagicMock()}

    with (
        patch(
            "mindroom.teams.create_agent",
            side_effect=[MagicMock(name="GeneralAgent"), RuntimeError("boom")],
        ),
        patch("mindroom.teams.resolve_agent_knowledge_access", return_value=KnowledgeResolution(knowledge=None)),
        patch("mindroom.teams._create_team_instance") as mock_create_team,
    ):
        response = await team_response(
            agent_names=["general", "research"],
            mode=TeamMode.COORDINATE,
            message="Analyze this.",
            turn_recorder=_team_turn_recorder("Analyze this."),
            orchestrator=orchestrator,
            execution_identity=None,
            reason_prefix="Team 'summary'",
        )

    assert response == "Team 'summary' includes agent 'research' that could not be materialized for this request."
    mock_create_team.assert_not_called()


@pytest.mark.asyncio
async def test_team_stream_rejects_missing_materialized_members() -> None:
    """Streaming team execution should surface exact-materialization failures without shrinking."""
    runtime_paths = test_runtime_paths(Path(tempfile.mkdtemp()))
    config = bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(display_name="GeneralAgent", rooms=["#test:example.org"]),
                "research": AgentConfig(display_name="ResearchAgent", rooms=["#test:example.org"]),
            },
        ),
        runtime_paths,
    )
    orchestrator = MagicMock()
    orchestrator.config = config
    orchestrator.runtime_paths = runtime_paths_for(config)
    orchestrator.knowledge_managers = {}
    orchestrator.agent_bots = {"general": MagicMock()}

    with patch("mindroom.teams._create_team_instance") as mock_create_team:
        chunks = [
            chunk
            async for chunk in team_response_stream(
                agent_ids=[
                    config.get_ids(runtime_paths_for(config))["general"],
                    config.get_ids(runtime_paths_for(config))["research"],
                ],
                mode=TeamMode.COORDINATE,
                message="Analyze this.",
                turn_recorder=_team_turn_recorder("Analyze this."),
                orchestrator=orchestrator,
                execution_identity=None,
            )
        ]

    assert chunks == ["Team request includes agent 'research' that could not be materialized for this request."]
    mock_create_team.assert_not_called()


@pytest.mark.asyncio
async def test_team_stream_rejects_request_time_materialization_failure() -> None:
    """Streaming team execution should reject when request-time member construction fails."""
    runtime_paths = test_runtime_paths(Path(tempfile.mkdtemp()))
    config = bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(display_name="GeneralAgent", rooms=["#test:example.org"]),
                "research": AgentConfig(display_name="ResearchAgent", rooms=["#test:example.org"]),
            },
        ),
        runtime_paths,
    )
    orchestrator = MagicMock()
    orchestrator.config = config
    orchestrator.runtime_paths = runtime_paths_for(config)
    orchestrator.knowledge_managers = {}
    orchestrator.agent_bots = {"general": MagicMock(), "research": MagicMock()}

    with (
        patch(
            "mindroom.teams.create_agent",
            side_effect=[MagicMock(name="GeneralAgent"), RuntimeError("boom")],
        ),
        patch("mindroom.teams.resolve_agent_knowledge_access", return_value=KnowledgeResolution(knowledge=None)),
        patch("mindroom.teams._create_team_instance") as mock_create_team,
    ):
        chunks = [
            chunk
            async for chunk in team_response_stream(
                agent_ids=[
                    config.get_ids(runtime_paths_for(config))["general"],
                    config.get_ids(runtime_paths_for(config))["research"],
                ],
                mode=TeamMode.COORDINATE,
                message="Analyze this.",
                turn_recorder=_team_turn_recorder("Analyze this."),
                orchestrator=orchestrator,
                execution_identity=None,
                reason_prefix="Team 'summary'",
            )
        ]

    assert chunks == ["Team 'summary' includes agent 'research' that could not be materialized for this request."]
    mock_create_team.assert_not_called()


@pytest.mark.asyncio
async def test_team_stream_retries_without_inline_media_on_setup_error() -> None:
    """Team streaming should retry when stream setup fails before any output is emitted."""
    config = _build_test_config()
    orchestrator = MagicMock()
    orchestrator.config = config
    orchestrator.runtime_paths = runtime_paths_for(config)
    orchestrator.knowledge_managers = {}
    orchestrator.agent_bots = {"general": MagicMock()}

    media_validation_error = "Error code: 500 - audio input is not supported"

    async def successful_stream() -> AsyncIterator[object]:
        yield TeamRunContentEvent(content="Recovered setup stream")

    mock_team = _make_test_team()
    mock_team.arun = MagicMock(side_effect=[Exception(media_validation_error), successful_stream()])
    audio_input = MagicMock(name="audio_input")

    fake_agent = _make_test_agent("GeneralAgent")
    with (
        patch("mindroom.teams.create_agent", return_value=fake_agent),
        patch("mindroom.teams.resolve_agent_knowledge_access", return_value=KnowledgeResolution(knowledge=None)),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
    ):
        chunks = [
            chunk
            async for chunk in team_response_stream(
                agent_ids=[config.get_ids(runtime_paths_for(config))["general"]],
                mode=TeamMode.COORDINATE,
                message="Analyze this.",
                turn_recorder=_team_turn_recorder("Analyze this."),
                orchestrator=orchestrator,
                execution_identity=None,
                media=MediaInputs(audio=[audio_input]),
            )
        ]

    assert mock_team.arun.call_count == 2
    first_call = mock_team.arun.call_args_list[0]
    second_call = mock_team.arun.call_args_list[1]
    first_prompt = first_call.args[0]
    second_prompt = second_call.args[0]
    assert isinstance(first_prompt, list)
    assert isinstance(second_prompt, str)
    assert first_prompt[-1].audio == [audio_input]
    assert "Inline media unavailable for this model" in second_prompt

    rendered_output = "".join(chunk.content if hasattr(chunk, "content") else str(chunk) for chunk in chunks)
    assert "Recovered setup stream" in rendered_output


@pytest.mark.asyncio
async def test_team_stream_retries_without_inline_media_on_streamed_run_error() -> None:
    """Team streaming should retry on streamed run errors before any output is emitted."""
    config = _build_test_config()
    orchestrator = MagicMock()
    orchestrator.config = config
    orchestrator.runtime_paths = runtime_paths_for(config)
    orchestrator.knowledge_managers = {}
    orchestrator.agent_bots = {"general": MagicMock()}

    media_validation_error = "Error code: 500 - audio input is not supported"

    async def failing_stream() -> AsyncIterator[object]:
        yield TeamRunErrorEvent(content=media_validation_error)

    async def successful_stream() -> AsyncIterator[object]:
        yield TeamRunContentEvent(content="Recovered stream")

    mock_team = _make_test_team()
    mock_team.arun = MagicMock(side_effect=[failing_stream(), successful_stream()])
    audio_input = MagicMock(name="audio_input")

    fake_agent = _make_test_agent("GeneralAgent")
    with (
        patch("mindroom.teams.create_agent", return_value=fake_agent),
        patch("mindroom.teams.resolve_agent_knowledge_access", return_value=KnowledgeResolution(knowledge=None)),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
    ):
        chunks = [
            chunk
            async for chunk in team_response_stream(
                agent_ids=[config.get_ids(runtime_paths_for(config))["general"]],
                mode=TeamMode.COORDINATE,
                message="Analyze this.",
                turn_recorder=_team_turn_recorder("Analyze this."),
                orchestrator=orchestrator,
                execution_identity=None,
                media=MediaInputs(audio=[audio_input]),
            )
        ]

    assert mock_team.arun.call_count == 2
    first_call = mock_team.arun.call_args_list[0]
    second_call = mock_team.arun.call_args_list[1]
    first_prompt = first_call.args[0]
    second_prompt = second_call.args[0]
    assert isinstance(first_prompt, list)
    assert isinstance(second_prompt, str)
    assert first_prompt[-1].audio == [audio_input]
    assert "Inline media unavailable for this model" in second_prompt

    rendered_output = "".join(chunk.content if hasattr(chunk, "content") else str(chunk) for chunk in chunks)
    assert "Recovered stream" in rendered_output


class _DirectTeamAgentBot:
    running = True

    def __init__(self, agent_name: str, config: Config) -> None:
        self._agent_name = agent_name
        self._config = config

    @property
    def agent(self) -> object:
        return create_agent(self._agent_name, self._config, runtime_paths_for(self._config), execution_identity=None)


def _build_private_team_orchestrator(*, include_private_member: bool) -> tuple[Config, MagicMock]:
    runtime_paths = test_runtime_paths(Path(tempfile.mkdtemp()))
    config = bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(
                    display_name="GeneralAgent",
                    role="General assistant",
                    rooms=["#test:example.org"],
                    private=AgentPrivateConfig(per="user", root="mind_data"),
                ),
                "calculator": AgentConfig(
                    display_name="CalculatorAgent",
                    role="Calculator assistant",
                    rooms=["#test:example.org"],
                ),
            },
            models={
                "default": ModelConfig(provider="openai", id="gpt-4o-mini"),
            },
        ),
        runtime_paths,
    )
    orchestrator = MagicMock()
    orchestrator.config = config
    orchestrator.runtime_paths = runtime_paths_for(config)
    orchestrator.knowledge_managers = {}
    orchestrator.agent_bots = {
        "calculator": _DirectTeamAgentBot("calculator", config),
    }
    if include_private_member:
        orchestrator.agent_bots["general"] = _DirectTeamAgentBot("general", config)
    return config, orchestrator


@pytest.mark.asyncio
async def test_team_response_rejects_private_agents_in_ad_hoc_teams() -> None:
    """Direct team helpers should reject any team that includes a private agent."""
    _, orchestrator = _build_private_team_orchestrator(include_private_member=True)

    with pytest.raises(ValueError, match="private agents cannot participate in teams yet"):
        await team_response(
            agent_names=["general", "calculator"],
            mode=TeamMode.COORDINATE,
            message="Analyze this.",
            turn_recorder=_team_turn_recorder("Analyze this."),
            orchestrator=orchestrator,
            execution_identity=None,
        )


@pytest.mark.asyncio
async def test_team_response_rejects_private_agents_even_when_private_member_is_unavailable() -> None:
    """Direct team helpers should reject requested private members before availability filtering."""
    _, orchestrator = _build_private_team_orchestrator(include_private_member=False)

    with pytest.raises(ValueError, match="private agents cannot participate in teams yet"):
        await team_response(
            agent_names=["general", "calculator"],
            mode=TeamMode.COORDINATE,
            message="Analyze this.",
            turn_recorder=_team_turn_recorder("Analyze this."),
            orchestrator=orchestrator,
            execution_identity=None,
        )


@pytest.mark.asyncio
async def test_team_response_stream_rejects_private_agents_even_when_private_member_is_unavailable() -> None:
    """Streaming team helpers should reject requested private members before availability filtering."""
    config, orchestrator = _build_private_team_orchestrator(include_private_member=False)

    with pytest.raises(ValueError, match="private agents cannot participate in teams yet"):
        [
            chunk
            async for chunk in team_response_stream(
                agent_ids=[
                    config.get_ids(runtime_paths_for(config))["general"],
                    config.get_ids(runtime_paths_for(config))["calculator"],
                ],
                mode=TeamMode.COORDINATE,
                message="Analyze this.",
                turn_recorder=_team_turn_recorder("Analyze this."),
                orchestrator=orchestrator,
                execution_identity=None,
            )
        ]


@pytest.mark.asyncio
async def test_team_response_rejects_members_that_delegate_to_private_agents() -> None:
    """Direct team helpers should reject shared members that reach private agents via delegation."""
    runtime_paths = test_runtime_paths(Path(tempfile.mkdtemp()))
    config = bind_runtime_paths(
        Config(
            agents={
                "leader": AgentConfig(display_name="Leader", delegate_to=["mind"]),
                "helper": AgentConfig(display_name="Helper"),
                "mind": AgentConfig(
                    display_name="Mind",
                    private=AgentPrivateConfig(per="user", root="mind_data"),
                ),
            },
        ),
        runtime_paths,
    )
    orchestrator = MagicMock()
    orchestrator.config = config
    orchestrator.runtime_paths = runtime_paths_for(config)
    orchestrator.knowledge_managers = {}
    orchestrator.agent_bots = {
        "leader": _DirectTeamAgentBot("leader", config),
        "helper": _DirectTeamAgentBot("helper", config),
    }

    with pytest.raises(
        ValueError,
        match="reaches private agent 'mind' via delegation; private agents cannot participate in teams yet",
    ):
        await team_response(
            agent_names=["leader", "helper"],
            mode=TeamMode.COORDINATE,
            message="Analyze this.",
            turn_recorder=_team_turn_recorder("Analyze this."),
            orchestrator=orchestrator,
            execution_identity=None,
        )


@pytest.mark.asyncio
async def test_team_response_ignores_router_in_direct_team_member_list() -> None:
    """Direct team helpers should skip router entries before request-scoped setup."""
    config = _build_test_config()
    orchestrator = MagicMock()
    orchestrator.config = config
    orchestrator.runtime_paths = runtime_paths_for(config)
    orchestrator.knowledge_managers = {}
    general_bot = MagicMock()
    general_bot.agent = MagicMock()
    general_bot.agent.name = "GeneralAgent"
    general_bot.agent.instructions = []
    orchestrator.agent_bots = {"general": general_bot}
    mock_team = _make_test_team()
    mock_team.arun = AsyncMock(return_value=TeamRunOutput(content="General response"))

    fake_agent = _make_test_agent("GeneralAgent")

    with (
        patch("mindroom.teams.create_agent", return_value=fake_agent),
        patch("mindroom.teams.resolve_agent_knowledge_access", return_value=KnowledgeResolution(knowledge=None)),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
    ):
        response = await team_response(
            agent_names=["router", "general"],
            mode=TeamMode.COORDINATE,
            message="Analyze this.",
            turn_recorder=_team_turn_recorder("Analyze this."),
            orchestrator=orchestrator,
            execution_identity=None,
        )

    assert "General response" in response


@pytest.mark.asyncio
async def test_team_response_stream_ignores_router_in_direct_team_member_list() -> None:
    """Streaming team helpers should skip router entries before request-scoped setup."""
    config = _build_test_config()
    orchestrator = MagicMock()
    orchestrator.config = config
    orchestrator.runtime_paths = runtime_paths_for(config)
    orchestrator.knowledge_managers = {}
    orchestrator.agent_bots = {"general": MagicMock(running=True)}

    async def successful_stream() -> AsyncIterator[object]:
        yield TeamRunContentEvent(content="General response")

    mock_team = _make_test_team()
    mock_team.arun = MagicMock(return_value=successful_stream())
    fake_agent = _make_test_agent("GeneralAgent")

    with (
        patch("mindroom.teams.create_agent", return_value=fake_agent),
        patch("mindroom.teams.resolve_agent_knowledge_access", return_value=KnowledgeResolution(knowledge=None)),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
    ):
        chunks = [
            chunk
            async for chunk in team_response_stream(
                agent_ids=[
                    config.get_ids(runtime_paths_for(config))[ROUTER_AGENT_NAME],
                    config.get_ids(runtime_paths_for(config))["general"],
                ],
                mode=TeamMode.COORDINATE,
                message="Analyze this.",
                turn_recorder=_team_turn_recorder("Analyze this."),
                orchestrator=orchestrator,
                execution_identity=None,
            )
        ]

    rendered_output = "".join(chunk.content if hasattr(chunk, "content") else str(chunk) for chunk in chunks)
    assert "General response" in rendered_output


@pytest.mark.asyncio
async def test_team_response_forwards_session_and_user_id_to_team_run() -> None:
    """Direct team helpers should preserve session and requester identity in Team.arun()."""
    config = _build_test_config()
    orchestrator = MagicMock()
    orchestrator.config = config
    orchestrator.runtime_paths = runtime_paths_for(config)
    orchestrator.knowledge_managers = {}
    general_bot = MagicMock()
    general_bot.agent = MagicMock()
    general_bot.agent.name = "GeneralAgent"
    general_bot.agent.instructions = []
    orchestrator.agent_bots = {"general": general_bot}
    mock_team = _make_test_team()
    mock_team.arun = AsyncMock(return_value=TeamRunOutput(content="General response"))

    fake_agent = _make_test_agent("GeneralAgent")

    with (
        patch("mindroom.teams.create_agent", return_value=fake_agent),
        patch("mindroom.teams.resolve_agent_knowledge_access", return_value=KnowledgeResolution(knowledge=None)),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
    ):
        response = await team_response(
            agent_names=["general"],
            mode=TeamMode.COORDINATE,
            message="Analyze this.",
            turn_recorder=_team_turn_recorder("Analyze this."),
            orchestrator=orchestrator,
            execution_identity=None,
            session_id="session-123",
            user_id="@alice:example.org",
        )

    assert "General response" in response
    assert mock_team.arun.await_args.kwargs["session_id"] == "session-123"
    assert mock_team.arun.await_args.kwargs["user_id"] == "@alice:example.org"


@pytest.mark.asyncio
async def test_team_response_materializes_members_with_request_execution_identity() -> None:
    """Direct team helpers should build members with the live request identity."""
    config = _build_test_config()
    orchestrator = MagicMock()
    orchestrator.config = config
    orchestrator.runtime_paths = runtime_paths_for(config)
    orchestrator.knowledge_managers = {}

    class _FailIfAccessed:
        running = True

        @property
        def agent(self) -> object:
            msg = "team member resolution should not use AgentBot.agent"
            raise AssertionError(msg)

    orchestrator.agent_bots = {"general": _FailIfAccessed()}
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="summary",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-123",
    )
    fake_agent = _make_test_agent("GeneralAgent")
    mock_team = _make_test_team()
    mock_team.arun = AsyncMock(return_value=TeamRunOutput(content="General response"))

    with (
        patch("mindroom.teams.create_agent", return_value=fake_agent) as mock_create_agent,
        patch("mindroom.teams.resolve_agent_knowledge_access", return_value=KnowledgeResolution(knowledge=None)),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
    ):
        response = await team_response(
            agent_names=["general"],
            mode=TeamMode.COORDINATE,
            message="Analyze this.",
            turn_recorder=_team_turn_recorder("Analyze this."),
            orchestrator=orchestrator,
            execution_identity=identity,
        )

    assert "General response" in response
    assert mock_create_agent.call_args.kwargs["execution_identity"] is identity
