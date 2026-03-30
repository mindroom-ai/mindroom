"""Tests for team inline-media fallback behavior."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from agno.run.base import RunStatus
from agno.run.team import RunCancelledEvent as TeamRunCancelledEvent
from agno.run.team import RunContentEvent as TeamRunContentEvent
from agno.run.team import RunErrorEvent as TeamRunErrorEvent
from agno.run.team import TeamRunOutput
from agno.session.agent import AgentSession

from mindroom.agents import create_agent, create_session_storage
from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.history import PreparedHistory
from mindroom.history.runtime import load_scope_session_context
from mindroom.history.storage import read_scope_seen_event_ids, update_scope_seen_event_ids
from mindroom.history.types import HistoryScope
from mindroom.matrix.identity import MatrixID
from mindroom.media_inputs import MediaInputs
from mindroom.team_runtime_resolution import (
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
    mock_team = MagicMock()
    mock_team.arun = AsyncMock(
        side_effect=[
            Exception(media_validation_error),
            TeamRunOutput(content="Recovered team response"),
        ],
    )
    audio_input = MagicMock(name="audio_input")

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
async def test_team_response_uses_compaction_aware_member_execution() -> None:
    """Direct team execution should prepare member history and apply queued compactions."""
    config = _build_test_config()
    orchestrator = MagicMock()
    orchestrator.config = config
    orchestrator.runtime_paths = runtime_paths_for(config)
    orchestrator.knowledge_managers = {}
    orchestrator.agent_bots = {"general": MagicMock()}
    mock_team = MagicMock()
    mock_team.arun = AsyncMock(return_value=TeamRunOutput(content="Recovered team response"))
    fake_agent = MagicMock()
    fake_agent.name = "GeneralAgent"
    collector: list[object] = []

    with (
        patch("mindroom.teams.create_agent", return_value=fake_agent),
        patch("mindroom.teams.get_agent_knowledge", return_value=None),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
        patch("mindroom.teams.prepare_bound_agents_for_run", new_callable=AsyncMock) as mock_prepare,
    ):
        mock_prepare.return_value = PreparedHistory()
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
    assert mock_prepare.await_args.kwargs["full_prompt"] == "Analyze this."
    assert mock_prepare.await_args.kwargs["session_id"] == "session-123"
    assert mock_prepare.await_args.kwargs["compaction_outcomes_collector"] is collector


@pytest.mark.asyncio
async def test_team_response_prefers_persisted_replay_over_thread_context_fallback() -> None:
    """Stored team replay should inject the summary and skip thread-stuffing fallback."""
    config = _build_test_config()
    orchestrator = MagicMock()
    orchestrator.config = config
    orchestrator.runtime_paths = runtime_paths_for(config)
    orchestrator.knowledge_managers = {}
    orchestrator.agent_bots = {"general": MagicMock()}

    summary_prefix = "<history_context>\n<summary>\nTeam summary\n</summary>\n</history_context>\n\n"
    mock_team = MagicMock()
    mock_team.arun = AsyncMock(return_value=TeamRunOutput(content="Recovered team response"))
    fake_agent = MagicMock()
    fake_agent.name = "GeneralAgent"

    with (
        patch("mindroom.teams.create_agent", return_value=fake_agent),
        patch("mindroom.teams.get_agent_knowledge", return_value=None),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
        patch("mindroom.teams.prepare_bound_agents_for_run", new_callable=AsyncMock) as mock_prepare,
    ):
        mock_prepare.return_value = PreparedHistory(
            summary_prompt_prefix=summary_prefix,
            has_stored_replay_state=True,
        )
        response = await team_response(
            agent_names=["general"],
            mode=TeamMode.COORDINATE,
            message="Analyze this.",
            thread_history=[{"sender": "user", "body": "Old thread context"}],
            orchestrator=orchestrator,
            execution_identity=None,
            session_id="session-123",
        )

    assert "Recovered team response" in response
    assert mock_prepare.await_args.kwargs["full_prompt"] == "Analyze this."
    prompt = mock_team.arun.await_args.args[0]
    assert prompt == f"{summary_prefix}Analyze this."
    assert "Thread Context:" not in prompt
    assert "Old thread context" not in prompt


@pytest.mark.asyncio
async def test_team_response_preserves_unseen_matrix_thread_context_with_stored_replay() -> None:
    """Matrix team runs should include unseen live thread messages alongside stored replay."""
    config = _build_test_config()
    runtime_paths = runtime_paths_for(config)
    orchestrator = MagicMock()
    orchestrator.config = config
    orchestrator.runtime_paths = runtime_paths
    orchestrator.knowledge_managers = {}
    orchestrator.agent_bots = {"general": MagicMock()}

    storage = create_session_storage("general", config, runtime_paths, execution_identity=None)
    team_scope = HistoryScope(kind="team", scope_id="team-general")
    session = AgentSession(
        session_id="session-123",
        runs=[],
        created_at=1,
        updated_at=1,
    )
    update_scope_seen_event_ids(session, team_scope, ["event-1"])
    storage.upsert_session(
        session,
    )

    summary_prefix = "<history_context>\n<summary>\nTeam summary\n</summary>\n</history_context>\n\n"
    mock_team = MagicMock()
    mock_team.arun = AsyncMock(return_value=TeamRunOutput(content="Recovered team response"))
    fake_agent = MagicMock()
    fake_agent.id = "general"
    fake_agent.name = "GeneralAgent"
    fake_agent.team_id = "team-general"

    thread_history = [
        {"event_id": "event-1", "sender": "user", "body": "Already seen"},
        {"event_id": "event-2", "sender": "user", "body": "Fresh follow-up"},
        {"event_id": "event-3", "sender": "user", "body": "Current message body"},
    ]

    with (
        patch("mindroom.teams.create_agent", return_value=fake_agent),
        patch("mindroom.teams.get_agent_knowledge", return_value=None),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
        patch("mindroom.teams.prepare_bound_agents_for_run", new_callable=AsyncMock) as mock_prepare,
    ):
        mock_prepare.return_value = PreparedHistory(
            summary_prompt_prefix=summary_prefix,
            has_stored_replay_state=True,
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
    budget_prompt = mock_prepare.await_args.kwargs["full_prompt"]
    assert "Fresh follow-up" in budget_prompt
    assert "Already seen" not in budget_prompt
    assert "Current message body" not in budget_prompt
    prompt = mock_team.arun.await_args.args[0]
    assert summary_prefix in prompt
    assert "Fresh follow-up" in prompt
    assert "Already seen" not in prompt
    assert "Thread Context:" not in prompt


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

    storage = create_session_storage("general", config, runtime_paths, execution_identity=None)
    storage.upsert_session(
        AgentSession(
            session_id="session-456",
            runs=[],
            created_at=1,
            updated_at=1,
        ),
    )

    mock_team = MagicMock()
    mock_team.arun = AsyncMock(return_value=TeamRunOutput(content="Recovered team response"))
    fake_agent = MagicMock()
    fake_agent.id = "general"
    fake_agent.name = "GeneralAgent"
    fake_agent.team_id = "team-general"

    with (
        patch("mindroom.teams.create_agent", return_value=fake_agent),
        patch("mindroom.teams.get_agent_knowledge", return_value=None),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
        patch("mindroom.teams.prepare_bound_agents_for_run", new_callable=AsyncMock) as mock_prepare,
    ):
        mock_prepare.return_value = PreparedHistory(
            summary_prompt_prefix="<history_context>\n<summary>\nTeam summary\n</summary>\n</history_context>\n\n",
            has_stored_replay_state=True,
        )
        await team_response(
            agent_names=["general"],
            mode=TeamMode.COORDINATE,
            message="Analyze this.",
            thread_history=[
                {"event_id": "event-1", "sender": "user", "body": "Fresh follow-up"},
                {"event_id": "event-2", "sender": "user", "body": "Current message body"},
            ],
            orchestrator=orchestrator,
            execution_identity=None,
            session_id="session-456",
            reply_to_event_id="event-2",
            response_sender_id="@mindroom_team:example.org",
        )

    scope_context = load_scope_session_context(
        agent=fake_agent,
        agent_name="general",
        session_id="session-456",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=None,
    )
    assert scope_context is not None
    assert scope_context.session is not None
    assert read_scope_seen_event_ids(scope_context.session, HistoryScope(kind="team", scope_id="team-general")) == {
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

    mock_team = MagicMock()
    mock_team.arun = AsyncMock(
        return_value=TeamRunOutput(content="Recovered team response", status=RunStatus.completed),
    )

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

    mock_team = MagicMock()
    mock_team.arun = AsyncMock(
        return_value=TeamRunOutput(
            content="Run run-123 was cancelled",
            status=RunStatus.cancelled,
        ),
    )

    fake_agent = MagicMock()
    fake_agent.name = "GeneralAgent"
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

    mock_team = MagicMock()
    mock_team.arun = AsyncMock(
        return_value=TeamRunOutput(content="validation failed in team", status=RunStatus.error),
    )

    fake_agent = MagicMock()
    fake_agent.name = "GeneralAgent"
    with (
        patch("mindroom.teams.create_agent", return_value=fake_agent),
        patch("mindroom.teams.get_agent_knowledge", return_value=None),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
        patch("mindroom.teams.get_user_friendly_error_message", return_value="friendly-team-error") as mock_friendly,
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

    mock_team = MagicMock()
    mock_team.arun = AsyncMock(
        side_effect=[
            TeamRunOutput(content="Error code: 500 - audio input is not supported", status=RunStatus.error),
            TeamRunOutput(content="Recovered team response", status=RunStatus.completed),
        ],
    )

    fake_agent = MagicMock()
    fake_agent.name = "GeneralAgent"
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
        patch("mindroom.teams._create_team_instance", return_value=MagicMock()),
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
        patch("mindroom.teams._create_team_instance", return_value=MagicMock()),
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
        patch("mindroom.teams._create_team_instance", return_value=MagicMock()),
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
        patch("mindroom.teams._create_team_instance", return_value=MagicMock()),
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
        patch("mindroom.teams._create_team_instance", return_value=MagicMock()),
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

    mock_team = MagicMock()
    mock_team.arun = MagicMock(side_effect=Exception(media_validation_error))
    audio_input = MagicMock(name="audio_input")

    fake_agent = MagicMock()
    fake_agent.name = "GeneralAgent"
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
    fake_agent = MagicMock()
    fake_agent.name = "GeneralAgent"
    collector: list[object] = []
    mock_team = MagicMock(name="team")

    async def raw_stream() -> AsyncIterator[object]:
        yield TeamRunOutput(content="Streamed team response")

    with (
        patch("mindroom.teams.create_agent", return_value=fake_agent),
        patch("mindroom.teams.get_agent_knowledge", return_value=None),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
        patch("mindroom.teams.prepare_bound_agents_for_run", new_callable=AsyncMock) as mock_prepare,
        patch(
            "mindroom.teams._team_response_stream_raw",
            new_callable=AsyncMock,
            return_value=raw_stream(),
        ) as mock_raw,
    ):
        mock_prepare.return_value = PreparedHistory()
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
    assert mock_prepare.await_args.kwargs["full_prompt"] == "Analyze this."
    assert mock_prepare.await_args.kwargs["session_id"] == "session-123"
    assert mock_prepare.await_args.kwargs["compaction_outcomes_collector"] is collector
    assert mock_raw.await_count == 1
    assert mock_raw.await_args.kwargs["team"] is mock_team


@pytest.mark.asyncio
async def test_team_response_stream_prefers_persisted_replay_over_thread_context_fallback() -> None:
    """Streaming team execution should pass the persisted-summary prompt to the raw stream."""
    config = _build_test_config()
    orchestrator = MagicMock()
    orchestrator.config = config
    orchestrator.runtime_paths = runtime_paths_for(config)
    orchestrator.knowledge_managers = {}
    orchestrator.agent_bots = {"general": MagicMock(running=True)}
    fake_agent = MagicMock()
    fake_agent.name = "GeneralAgent"
    summary_prefix = "<history_context>\n<summary>\nTeam summary\n</summary>\n</history_context>\n\n"
    mock_team = MagicMock(name="team")

    async def raw_stream() -> AsyncIterator[object]:
        yield TeamRunOutput(content="Streamed team response")

    with (
        patch("mindroom.teams.create_agent", return_value=fake_agent),
        patch("mindroom.teams.get_agent_knowledge", return_value=None),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
        patch("mindroom.teams.prepare_bound_agents_for_run", new_callable=AsyncMock) as mock_prepare,
        patch(
            "mindroom.teams._team_response_stream_raw",
            new_callable=AsyncMock,
            return_value=raw_stream(),
        ) as mock_raw,
    ):
        mock_prepare.return_value = PreparedHistory(
            summary_prompt_prefix=summary_prefix,
            has_stored_replay_state=True,
        )
        chunks = [
            chunk
            async for chunk in team_response_stream(
                agent_ids=[config.get_ids(runtime_paths_for(config))["general"]],
                message="Analyze this.",
                thread_history=[{"sender": "user", "body": "Old thread context"}],
                orchestrator=orchestrator,
                execution_identity=None,
                session_id="session-123",
            )
        ]

    assert len(chunks) == 1
    assert "Streamed team response" in str(chunks[0])
    assert mock_prepare.await_args.kwargs["full_prompt"] == "Analyze this."
    assert mock_raw.await_args.kwargs["prompt"] == f"{summary_prefix}Analyze this."


@pytest.mark.asyncio
async def test_team_response_stream_preserves_unseen_matrix_thread_context_with_stored_replay() -> None:
    """Streaming Matrix team runs should include unseen live thread messages alongside stored replay."""
    config = _build_test_config()
    runtime_paths = runtime_paths_for(config)
    orchestrator = MagicMock()
    orchestrator.config = config
    orchestrator.runtime_paths = runtime_paths
    orchestrator.knowledge_managers = {}
    orchestrator.agent_bots = {"general": MagicMock(running=True)}

    storage = create_session_storage("general", config, runtime_paths, execution_identity=None)
    team_scope = HistoryScope(kind="team", scope_id="team-general")
    session = AgentSession(
        session_id="session-789",
        runs=[],
        created_at=1,
        updated_at=1,
    )
    update_scope_seen_event_ids(session, team_scope, ["event-1"])
    storage.upsert_session(
        session,
    )

    fake_agent = MagicMock()
    fake_agent.id = "general"
    fake_agent.name = "GeneralAgent"
    fake_agent.team_id = "team-general"
    summary_prefix = "<history_context>\n<summary>\nTeam summary\n</summary>\n</history_context>\n\n"
    mock_team = MagicMock(name="team")

    async def raw_stream() -> AsyncIterator[object]:
        yield TeamRunOutput(content="Streamed team response")

    with (
        patch("mindroom.teams.create_agent", return_value=fake_agent),
        patch("mindroom.teams.get_agent_knowledge", return_value=None),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
        patch("mindroom.teams.prepare_bound_agents_for_run", new_callable=AsyncMock) as mock_prepare,
        patch(
            "mindroom.teams._team_response_stream_raw",
            new_callable=AsyncMock,
            return_value=raw_stream(),
        ) as mock_raw,
    ):
        mock_prepare.return_value = PreparedHistory(
            summary_prompt_prefix=summary_prefix,
            has_stored_replay_state=True,
        )
        chunks = [
            chunk
            async for chunk in team_response_stream(
                agent_ids=[config.get_ids(runtime_paths)["general"]],
                message="Analyze this.",
                thread_history=[
                    {"event_id": "event-1", "sender": "user", "body": "Already seen"},
                    {"event_id": "event-2", "sender": "user", "body": "Fresh follow-up"},
                    {"event_id": "event-3", "sender": "user", "body": "Current message body"},
                ],
                orchestrator=orchestrator,
                execution_identity=None,
                session_id="session-789",
                reply_to_event_id="event-3",
                response_sender_id="@mindroom_team:example.org",
            )
        ]

    assert len(chunks) == 1
    budget_prompt = mock_prepare.await_args.kwargs["full_prompt"]
    assert "Fresh follow-up" in budget_prompt
    assert "Already seen" not in budget_prompt
    prompt = mock_raw.await_args.kwargs["prompt"]
    assert summary_prefix in prompt
    assert "Fresh follow-up" in prompt
    assert "Already seen" not in prompt


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

    mock_team = MagicMock()
    mock_team.arun = MagicMock(side_effect=[Exception(media_validation_error), successful_stream()])
    audio_input = MagicMock(name="audio_input")

    fake_agent = MagicMock()
    fake_agent.name = "GeneralAgent"
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

    fake_agent = MagicMock()
    fake_agent.name = "GeneralAgent"
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
    assert list(first_call.kwargs["audio"]) == [audio_input]
    assert list(second_call.kwargs["audio"]) == []
    assert "Inline media unavailable for this model" in second_call.args[0]

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

    mock_team = MagicMock()
    mock_team.arun = MagicMock(return_value=successful_stream())
    fake_agent = MagicMock()
    fake_agent.name = "GeneralAgent"

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
