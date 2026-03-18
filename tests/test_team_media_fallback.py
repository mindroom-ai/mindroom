"""Tests for team inline-media fallback behavior."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from agno.run.team import RunContentEvent as TeamRunContentEvent
from agno.run.team import RunErrorEvent as TeamRunErrorEvent
from agno.run.team import TeamRunOutput

from mindroom.agents import create_agent
from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.media_inputs import MediaInputs
from mindroom.teams import (
    TeamMode,
    _materialize_team_members,
    _team_response_stream_raw,
    team_response,
    team_response_stream,
)
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


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
    mock_team = MagicMock()
    mock_team.arun = AsyncMock(
        side_effect=[
            Exception(media_validation_error),
            TeamRunOutput(content="Recovered team response"),
        ],
    )
    audio_input = MagicMock(name="audio_input")

    with (
        patch("mindroom.teams._get_agents_from_orchestrator", return_value=[MagicMock(name="GeneralAgent")]),
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
    assert list(first_call.kwargs["audio"]) == [audio_input]
    assert list(second_call.kwargs["audio"]) == []
    assert "Inline media unavailable for this model" in second_call.args[0]


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

    mock_team = MagicMock()
    mock_team.arun = MagicMock(side_effect=Exception(media_validation_error))
    audio_input = MagicMock(name="audio_input")

    with (
        patch("mindroom.teams._get_agents_from_orchestrator", return_value=[MagicMock(name="GeneralAgent")]),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
    ):
        team_members = _materialize_team_members(["general"], orchestrator, None)
        raw_stream = await _team_response_stream_raw(
            team_members=team_members,
            mode=TeamMode.COORDINATE,
            message="Analyze this.",
            orchestrator=orchestrator,
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

    mock_team = MagicMock()
    mock_team.arun = MagicMock(side_effect=[Exception(media_validation_error), successful_stream()])
    audio_input = MagicMock(name="audio_input")

    with (
        patch("mindroom.teams._get_agents_from_orchestrator", return_value=[MagicMock(name="GeneralAgent")]),
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
    assert list(first_call.kwargs["audio"]) == [audio_input]
    assert list(second_call.kwargs["audio"]) == []
    assert "Inline media unavailable for this model" in second_call.args[0]

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

    mock_team = MagicMock()
    mock_team.arun = MagicMock(side_effect=[failing_stream(), successful_stream()])
    audio_input = MagicMock(name="audio_input")

    with (
        patch("mindroom.teams._get_agents_from_orchestrator", return_value=[MagicMock(name="GeneralAgent")]),
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
    assert list(first_call.kwargs["audio"]) == [audio_input]
    assert list(second_call.kwargs["audio"]) == []
    assert "Inline media unavailable for this model" in second_call.args[0]

    rendered_output = "".join(chunk.content if hasattr(chunk, "content") else str(chunk) for chunk in chunks)
    assert "Recovered stream" in rendered_output


class _DirectTeamAgentBot:
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
    mock_team = MagicMock()
    mock_team.arun = AsyncMock(return_value=TeamRunOutput(content="General response"))

    fake_agent = MagicMock()
    fake_agent.name = "GeneralAgent"

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
    mock_team = MagicMock()
    mock_team.arun = AsyncMock(return_value=TeamRunOutput(content="General response"))

    fake_agent = MagicMock()
    fake_agent.name = "GeneralAgent"

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
    fake_agent = MagicMock()
    fake_agent.name = "GeneralAgent"
    mock_team = MagicMock()
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
