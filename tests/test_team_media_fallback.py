"""Tests for team inline-media fallback behavior."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from agno.agent import Agent as AgnoAgent
from agno.models.message import Message
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.run.team import RunCancelledEvent as TeamRunCancelledEvent
from agno.run.team import RunContentEvent as TeamRunContentEvent
from agno.run.team import RunErrorEvent as TeamRunErrorEvent
from agno.run.team import TeamRunOutput
from agno.team import Team as AgnoTeam
from agno.team._run import _cleanup_and_store
from agno.utils.message import get_text_from_message

from mindroom.agents import create_agent
from mindroom.ai import QUEUED_MESSAGE_NOTICE_TEXT
from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.execution_preparation import PreparedExecutionContext
from mindroom.history.runtime import open_bound_scope_session_context
from mindroom.history.storage import read_scope_seen_event_ids, update_scope_seen_event_ids
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
    _team_response_stream_raw,
    team_response,
    team_response_stream,
)
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
) -> PreparedExecutionContext:
    return PreparedExecutionContext(
        messages=(*context_messages, Message(role="user", content=final_prompt)),
        replay_plan=None,
        unseen_event_ids=unseen_event_ids or [],
        replays_persisted_history=replays_persisted_history,
        compaction_outcomes=[],
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
        patch("mindroom.teams.get_agent_knowledge", return_value=None),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
    ):
        response = await team_response(
            agent_names=["general"],
            mode=TeamMode.COORDINATE,
            message="Analyze this.",
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
    assert isinstance(second_prompt, list)
    assert first_prompt[-1].audio == [audio_input]
    assert not second_prompt[-1].audio
    assert "Inline media unavailable for this model" in str(second_prompt[-1].content)


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
        patch("mindroom.teams.get_agent_knowledge", return_value=None),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
    ):
        response = await team_response(
            agent_names=["general"],
            mode=TeamMode.COORDINATE,
            message="Analyze this.",
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
        patch("mindroom.teams.get_agent_knowledge", return_value=None),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
        patch("mindroom.teams.get_user_friendly_error_message", return_value="friendly-team-error"),
    ):
        response = await team_response(
            agent_names=["general"],
            mode=TeamMode.COORDINATE,
            message="Analyze this.",
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
        patch("mindroom.teams.get_agent_knowledge", return_value=None),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
        patch("mindroom.teams.prepare_bound_team_execution_context", new_callable=AsyncMock) as mock_prepare,
    ):
        mock_prepare.return_value = _prepared_team_execution_context(final_prompt="Analyze this.")
        response = await team_response(
            agent_names=["general"],
            mode=TeamMode.COORDINATE,
            message="Analyze this.",
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
        patch("mindroom.teams.get_agent_knowledge", return_value=None),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
        patch("mindroom.teams.prepare_bound_team_execution_context", new_callable=AsyncMock) as mock_prepare,
    ):
        mock_prepare.return_value = _prepared_team_execution_context(
            final_prompt="Analyze this.",
            replays_persisted_history=True,
        )
        response = await team_response(
            agent_names=["general"],
            mode=TeamMode.COORDINATE,
            message="Analyze this.",
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
    assert isinstance(prompt, list)
    assert [message.content for message in prompt] == ["Analyze this."]


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
        patch("mindroom.teams.get_agent_knowledge", return_value=None),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
        patch("mindroom.teams.prepare_bound_team_execution_context", new_callable=AsyncMock) as mock_prepare,
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
    assert isinstance(prompt, list)
    assert [message.content for message in prompt] == [
        "user: Fresh follow-up",
        "Analyze this.",
    ]


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
        patch("mindroom.teams.get_agent_knowledge", return_value=None),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
        patch(
            "mindroom.teams.prepare_bound_team_execution_context",
            new=AsyncMock(side_effect=fake_prepare_bound_team_execution_context),
        ),
    ):
        response = await team_response(
            agent_names=["general"],
            mode=TeamMode.COORDINATE,
            message="Analyze this.",
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
        patch("mindroom.teams.get_agent_knowledge", return_value=None),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
        patch(
            "mindroom.teams.prepare_bound_team_execution_context",
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
        patch("mindroom.teams.get_agent_knowledge", return_value=None),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
        patch(
            "mindroom.teams.prepare_bound_team_execution_context",
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
        patch("mindroom.teams.get_agent_knowledge", return_value=None),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
        patch("mindroom.teams.prepare_bound_team_execution_context", new_callable=AsyncMock) as mock_prepare,
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
        patch("mindroom.teams.get_agent_knowledge", return_value=None),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
    ):
        response = await team_response(
            agent_names=["general"],
            mode=TeamMode.COORDINATE,
            message="Analyze this.",
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
        patch("mindroom.teams.get_agent_knowledge", return_value=None),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
        pytest.raises(asyncio.CancelledError),
    ):
        await team_response(
            agent_names=["general"],
            mode=TeamMode.COORDINATE,
            message="Analyze this.",
            orchestrator=orchestrator,
            execution_identity=None,
            run_id="run-123",
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
        patch("mindroom.teams.get_agent_knowledge", return_value=None),
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
            orchestrator=orchestrator,
            execution_identity=None,
        )

    assert response == "friendly-team-error"
    mock_friendly.assert_called_once()


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
    callback_run_ids: list[str] = []
    with (
        patch("mindroom.teams.create_agent", return_value=fake_agent),
        patch("mindroom.teams.get_agent_knowledge", return_value=None),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
    ):
        response = await team_response(
            agent_names=["general"],
            mode=TeamMode.COORDINATE,
            message="Analyze this.",
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

    with (
        patch("mindroom.teams._ensure_request_team_knowledge_managers", new=AsyncMock(return_value={})),
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
        patch("mindroom.teams._ensure_request_team_knowledge_managers", new=AsyncMock(return_value={})),
        patch("mindroom.teams._materialize_team_members", return_value=team_members),
        patch("mindroom.teams._create_team_instance", return_value=_make_test_team()),
        patch("mindroom.teams._team_response_stream_raw", new=AsyncMock(side_effect=fake_stream_raw)),
    ):
        chunks = [
            chunk
            async for chunk in team_response_stream(
                agent_ids=team_agent_ids,
                message="Analyze this.",
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
        patch("mindroom.teams._ensure_request_team_knowledge_managers", new=AsyncMock(return_value={})),
        patch("mindroom.teams._materialize_team_members", return_value=team_members),
        patch("mindroom.teams._create_team_instance", return_value=_make_test_team()),
        patch("mindroom.teams._team_response_stream_raw", new=AsyncMock(side_effect=fake_stream_raw)),
        pytest.raises(asyncio.CancelledError),
    ):
        async for _chunk in team_response_stream(
            agent_ids=team_agent_ids,
            message="Analyze this.",
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
        patch("mindroom.teams._ensure_request_team_knowledge_managers", new=AsyncMock(return_value={})),
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
        patch("mindroom.teams._ensure_request_team_knowledge_managers", new=AsyncMock(return_value={})),
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
        patch("mindroom.teams._ensure_request_team_knowledge_managers", new=AsyncMock(return_value={})),
        patch("mindroom.teams._materialize_team_members", return_value=team_members),
        patch("mindroom.teams._create_team_instance", return_value=_make_test_team()),
        patch("mindroom.teams._team_response_stream_raw", new=AsyncMock(side_effect=fake_stream_raw)),
    ):
        chunks = [
            chunk
            async for chunk in team_response_stream(
                agent_ids=team_agent_ids,
                message="Analyze this.",
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
        patch("mindroom.teams.get_agent_knowledge", return_value=None),
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
        patch("mindroom.teams.get_agent_knowledge", return_value=None),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
        patch("mindroom.teams.prepare_bound_team_execution_context", new_callable=AsyncMock) as mock_prepare,
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
        patch("mindroom.teams.get_agent_knowledge", return_value=None),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
        patch("mindroom.teams.prepare_bound_team_execution_context", new_callable=AsyncMock) as mock_prepare,
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
    assert isinstance(prepared_prompt, list)
    assert [message.content for message in prepared_prompt] == ["Analyze this."]


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
        patch("mindroom.teams.get_agent_knowledge", return_value=None),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
        patch("mindroom.teams.prepare_bound_team_execution_context", new_callable=AsyncMock) as mock_prepare,
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
    assert isinstance(prompt, list)
    assert [message.content for message in prompt] == ["user: Fresh follow-up", "Analyze this."]


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
        patch("mindroom.teams.get_agent_knowledge", return_value=None),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
        patch("mindroom.teams.prepare_bound_team_execution_context", new_callable=AsyncMock) as mock_prepare,
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
                orchestrator=orchestrator,
                execution_identity=None,
                session_id="session-123",
                response_sender_id="@mindroom_team:example.org",
            )
        ]

    assert len(chunks) == 1
    assert "Streamed team response" in str(chunks[0])
    prompt = mock_raw.await_args.kwargs["prompt"]
    assert isinstance(prompt, list)
    assert [message.content for message in prompt] == ["Previous team reply", "Analyze this."]


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
        patch("mindroom.teams.get_agent_knowledge", return_value=None),
        patch("mindroom.teams._create_team_instance") as mock_create_team,
    ):
        response = await team_response(
            agent_names=["general", "research"],
            mode=TeamMode.COORDINATE,
            message="Analyze this.",
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
        patch("mindroom.teams.get_agent_knowledge", return_value=None),
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
        patch("mindroom.teams.get_agent_knowledge", return_value=None),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
    ):
        chunks = [
            chunk
            async for chunk in team_response_stream(
                agent_ids=[config.get_ids(runtime_paths_for(config))["general"]],
                mode=TeamMode.COORDINATE,
                message="Analyze this.",
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
    assert isinstance(second_prompt, list)
    assert first_prompt[-1].audio == [audio_input]
    assert not second_prompt[-1].audio
    assert "Inline media unavailable for this model" in str(second_prompt[-1].content)

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
        patch("mindroom.teams.get_agent_knowledge", return_value=None),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
    ):
        chunks = [
            chunk
            async for chunk in team_response_stream(
                agent_ids=[config.get_ids(runtime_paths_for(config))["general"]],
                mode=TeamMode.COORDINATE,
                message="Analyze this.",
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
    assert isinstance(second_prompt, list)
    assert first_prompt[-1].audio == [audio_input]
    assert not second_prompt[-1].audio
    assert "Inline media unavailable for this model" in str(second_prompt[-1].content)

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
        patch("mindroom.teams.get_agent_knowledge", return_value=None),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
    ):
        response = await team_response(
            agent_names=["router", "general"],
            mode=TeamMode.COORDINATE,
            message="Analyze this.",
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
        patch("mindroom.teams.get_agent_knowledge", return_value=None),
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
        patch("mindroom.teams.get_agent_knowledge", return_value=None),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
    ):
        response = await team_response(
            agent_names=["general"],
            mode=TeamMode.COORDINATE,
            message="Analyze this.",
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
        patch("mindroom.teams.get_agent_knowledge", return_value=None),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
    ):
        response = await team_response(
            agent_names=["general"],
            mode=TeamMode.COORDINATE,
            message="Analyze this.",
            orchestrator=orchestrator,
            execution_identity=identity,
        )

    assert "General response" in response
    assert mock_create_agent.call_args.kwargs["execution_identity"] is identity
