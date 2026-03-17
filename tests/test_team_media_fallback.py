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
from mindroom.config.agent import AgentConfig, AgentPrivateConfig, AgentPrivateKnowledgeConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.knowledge.utils import bound_knowledge_managers, get_bound_knowledge_manager
from mindroom.media_inputs import MediaInputs
from mindroom.teams import TeamMode, _team_response_stream_raw, team_response, team_response_stream
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, tool_execution_identity
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
    orchestrator.agent_bots = {"general": MagicMock()}

    media_validation_error = "Error code: 500 - audio input is not supported"

    mock_team = MagicMock()
    mock_team.arun = MagicMock(side_effect=Exception(media_validation_error))
    audio_input = MagicMock(name="audio_input")

    with (
        patch("mindroom.teams._get_agents_from_orchestrator", return_value=[MagicMock(name="GeneralAgent")]),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
    ):
        raw_stream = await _team_response_stream_raw(
            agent_ids=[config.get_ids(runtime_paths_for(config))["general"]],
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
async def test_team_stream_retries_without_inline_media_on_setup_error() -> None:
    """Team streaming should retry when stream setup fails before any output is emitted."""
    config = _build_test_config()
    orchestrator = MagicMock()
    orchestrator.config = config
    orchestrator.runtime_paths = runtime_paths_for(config)
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
        return create_agent(self._agent_name, self._config, runtime_paths_for(self._config))


@pytest.mark.asyncio
async def test_team_response_requires_active_execution_identity_for_private_agents() -> None:
    """Direct team helpers should reject private agents without an ambient execution identity."""
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
    orchestrator.agent_bots = {"general": _DirectTeamAgentBot("general", config)}

    with pytest.raises(ValueError, match="requires an active execution identity"):
        await team_response(
            agent_names=["general"],
            mode=TeamMode.COORDINATE,
            message="Analyze this.",
            orchestrator=orchestrator,
        )


@pytest.mark.asyncio
async def test_team_response_uses_ambient_execution_identity_for_private_agents() -> None:
    """Direct team helpers should honor the ambient execution identity context."""
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
    orchestrator.agent_bots = {"general": _DirectTeamAgentBot("general", config)}
    mock_team = MagicMock()
    mock_team.arun = AsyncMock(return_value=TeamRunOutput(content="Private response"))
    execution_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="$thread",
    )

    with (
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
        tool_execution_identity(execution_identity),
    ):
        response = await team_response(
            agent_names=["general"],
            mode=TeamMode.COORDINATE,
            message="Analyze this.",
            orchestrator=orchestrator,
        )

    assert "Private response" in response


@pytest.mark.asyncio
async def test_team_response_ignores_router_in_direct_team_member_list() -> None:
    """Direct team helpers should skip router entries before request-scoped setup."""
    config = _build_test_config()
    orchestrator = MagicMock()
    orchestrator.config = config
    orchestrator.runtime_paths = runtime_paths_for(config)
    general_bot = MagicMock()
    general_bot.agent = MagicMock()
    general_bot.agent.name = "GeneralAgent"
    general_bot.agent.instructions = []
    orchestrator.agent_bots = {"general": general_bot}
    mock_team = MagicMock()
    mock_team.arun = AsyncMock(return_value=TeamRunOutput(content="General response"))

    with patch("mindroom.teams._create_team_instance", return_value=mock_team):
        response = await team_response(
            agent_names=["router", "general"],
            mode=TeamMode.COORDINATE,
            message="Analyze this.",
            orchestrator=orchestrator,
        )

    assert "General response" in response


@pytest.mark.asyncio
async def test_team_response_forwards_session_and_user_id_to_team_run() -> None:
    """Direct team helpers should preserve session and requester identity in Team.arun()."""
    config = _build_test_config()
    orchestrator = MagicMock()
    orchestrator.config = config
    orchestrator.runtime_paths = runtime_paths_for(config)
    general_bot = MagicMock()
    general_bot.agent = MagicMock()
    general_bot.agent.name = "GeneralAgent"
    general_bot.agent.instructions = []
    orchestrator.agent_bots = {"general": general_bot}
    mock_team = MagicMock()
    mock_team.arun = AsyncMock(return_value=TeamRunOutput(content="General response"))

    with patch("mindroom.teams._create_team_instance", return_value=mock_team):
        response = await team_response(
            agent_names=["general"],
            mode=TeamMode.COORDINATE,
            message="Analyze this.",
            orchestrator=orchestrator,
            session_id="session-123",
            user_id="@alice:example.org",
        )

    assert "General response" in response
    assert mock_team.arun.await_args.kwargs["session_id"] == "session-123"
    assert mock_team.arun.await_args.kwargs["user_id"] == "@alice:example.org"


@pytest.mark.asyncio
async def test_team_response_binds_private_knowledge_for_direct_team_helpers() -> None:
    """Direct team helpers should bind request-scoped private knowledge during agent materialization."""
    runtime_paths = test_runtime_paths(Path(tempfile.mkdtemp()))
    config = bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(
                    display_name="GeneralAgent",
                    role="General assistant",
                    private=AgentPrivateConfig(
                        per="user",
                        root="mind_data",
                        knowledge=AgentPrivateKnowledgeConfig(path="memory"),
                    ),
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
    orchestrator.agent_bots = {"general": MagicMock()}
    private_base_id = config.get_agent_private_knowledge_base_id("general")
    assert private_base_id is not None
    bound_manager = MagicMock()
    team_agent = MagicMock()
    team_agent.name = "GeneralAgent"
    team_agent.instructions = []
    execution_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="$thread",
    )

    def _assert_bound_manager(agent_names: list[str], _orchestrator: MagicMock) -> list[MagicMock]:
        assert agent_names == ["general"]
        assert get_bound_knowledge_manager(private_base_id) is bound_manager
        return [team_agent]

    mock_team = MagicMock()
    mock_team.arun = AsyncMock(return_value=TeamRunOutput(content="Private knowledge response"))

    with (
        patch(
            "mindroom.teams.ensure_request_knowledge_managers",
            new=AsyncMock(return_value={private_base_id: bound_manager}),
        ),
        patch("mindroom.teams._get_agents_from_orchestrator", side_effect=_assert_bound_manager),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
        tool_execution_identity(execution_identity),
    ):
        response = await team_response(
            agent_names=["general"],
            mode=TeamMode.COORDINATE,
            message="Analyze this.",
            orchestrator=orchestrator,
        )

    assert "Private knowledge response" in response


@pytest.mark.asyncio
async def test_team_response_reuses_already_bound_request_knowledge() -> None:
    """Direct team helpers should not re-ensure knowledge managers already bound by the caller."""
    runtime_paths = test_runtime_paths(Path(tempfile.mkdtemp()))
    config = bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(
                    display_name="GeneralAgent",
                    role="General assistant",
                    private=AgentPrivateConfig(
                        per="user",
                        root="mind_data",
                        knowledge=AgentPrivateKnowledgeConfig(path="memory"),
                    ),
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
    orchestrator.agent_bots = {"general": MagicMock()}
    private_base_id = config.get_agent_private_knowledge_base_id("general")
    assert private_base_id is not None
    bound_manager = MagicMock()
    team_agent = MagicMock()
    team_agent.name = "GeneralAgent"
    team_agent.instructions = []
    execution_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="$thread",
    )

    def _assert_bound_manager(agent_names: list[str], _orchestrator: MagicMock) -> list[MagicMock]:
        assert agent_names == ["general"]
        assert get_bound_knowledge_manager(private_base_id) is bound_manager
        return [team_agent]

    mock_team = MagicMock()
    mock_team.arun = AsyncMock(return_value=TeamRunOutput(content="Private knowledge response"))

    with (
        patch(
            "mindroom.teams.ensure_request_knowledge_managers",
            new_callable=AsyncMock,
            side_effect=AssertionError("request knowledge should already be bound"),
        ),
        patch("mindroom.teams._get_agents_from_orchestrator", side_effect=_assert_bound_manager),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
        tool_execution_identity(execution_identity),
        bound_knowledge_managers({private_base_id: bound_manager}),
    ):
        response = await team_response(
            agent_names=["general"],
            mode=TeamMode.COORDINATE,
            message="Analyze this.",
            orchestrator=orchestrator,
        )

    assert "Private knowledge response" in response


@pytest.mark.asyncio
async def test_team_response_honors_degraded_request_knowledge_context() -> None:
    """Direct team helpers should not retry knowledge init after the caller degraded to no knowledge."""
    runtime_paths = test_runtime_paths(Path(tempfile.mkdtemp()))
    config = bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(
                    display_name="GeneralAgent",
                    role="General assistant",
                    private=AgentPrivateConfig(
                        per="user",
                        root="mind_data",
                        knowledge=AgentPrivateKnowledgeConfig(path="memory"),
                    ),
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
    orchestrator.agent_bots = {"general": MagicMock()}
    team_agent = MagicMock()
    team_agent.name = "GeneralAgent"
    team_agent.instructions = []
    execution_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="$thread",
    )

    mock_team = MagicMock()
    mock_team.arun = AsyncMock(return_value=TeamRunOutput(content="Degraded team response"))

    with (
        patch(
            "mindroom.teams.ensure_request_knowledge_managers",
            new_callable=AsyncMock,
            side_effect=AssertionError("request knowledge should not be retried"),
        ),
        patch("mindroom.teams._get_agents_from_orchestrator", return_value=[team_agent]),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
        tool_execution_identity(execution_identity),
        bound_knowledge_managers({}),
    ):
        response = await team_response(
            agent_names=["general"],
            mode=TeamMode.COORDINATE,
            message="Analyze this.",
            orchestrator=orchestrator,
        )

    assert "Degraded team response" in response
