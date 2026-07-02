"""Tests for per-run token/usage metadata on the team paths."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from agno.models.metrics import Metrics
from agno.run.agent import RunContentEvent as AgentRunContentEvent
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.run.team import ModelRequestCompletedEvent as TeamModelRequestCompletedEvent
from agno.run.team import RunCompletedEvent as TeamRunCompletedEvent
from agno.run.team import RunErrorEvent as TeamRunErrorEvent
from agno.run.team import TeamRunOutput

from mindroom.history.turn_recorder import TurnRecorder
from mindroom.knowledge.utils import _KnowledgeResolution
from mindroom.teams import TeamMode, team_response, team_response_stream
from tests.conftest import runtime_paths_for
from tests.identity_helpers import entity_ids
from tests.test_team_media_fallback import _build_test_config, _make_test_agent, _make_test_team

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from contextlib import AbstractContextManager

    from agno.team import Team as AgnoTeam


def _make_orchestrator() -> tuple[MagicMock, object]:
    config = _build_test_config()
    orchestrator = MagicMock()
    orchestrator.config = config
    orchestrator.runtime_paths = runtime_paths_for(config)
    orchestrator.knowledge_managers = {}
    orchestrator.agent_bots = {"general": MagicMock()}
    return orchestrator, config


def _team_patches(mock_team: AgnoTeam) -> list[AbstractContextManager[object]]:
    fake_agent = _make_test_agent("GeneralAgent")
    return [
        patch("mindroom.teams.create_agent", return_value=fake_agent),
        patch("mindroom.teams.resolve_agent_knowledge_access", return_value=_KnowledgeResolution(knowledge=None)),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
    ]


def _team_run_output_with_metrics(
    content: str = "Team answer",
    *,
    status: RunStatus = RunStatus.completed,
) -> TeamRunOutput:
    output = TeamRunOutput(
        content=content,
        run_id="team-run-1",
        session_id="session-1",
        model="test-model",
        model_provider="openai",
        member_responses=[RunOutput(agent_name="GeneralAgent", content="Member answer")],
    )
    output.metrics = Metrics(
        input_tokens=800,
        output_tokens=120,
        total_tokens=920,
        cache_read_tokens=640,
        cache_write_tokens=32,
        reasoning_tokens=24,
        time_to_first_token=0.42,
        duration=1.75,
    )
    output.status = status
    return output


@pytest.mark.asyncio
async def test_team_response_collects_run_metadata() -> None:
    """The non-streaming team path exposes model/token/context metadata."""
    orchestrator, _config = _make_orchestrator()
    mock_team = _make_test_team()
    mock_team.arun = AsyncMock(return_value=_team_run_output_with_metrics())
    collector: dict[str, object] = {}

    patches = _team_patches(mock_team)
    with patches[0], patches[1], patches[2]:
        response = await team_response(
            agent_names=["general"],
            mode=TeamMode.COORDINATE,
            message="Analyze this.",
            turn_recorder=TurnRecorder(user_message="Analyze this."),
            orchestrator=orchestrator,
            execution_identity=None,
            session_id="session-1",
            run_metadata_collector=collector,
        )

    assert "Member answer" in response
    payload = collector["io.mindroom.ai_run"]
    assert payload["version"] == 1
    assert payload["run_id"] == "team-run-1"
    assert payload["status"] == "completed"
    assert payload["usage"]["input_tokens"] == 800
    assert payload["usage"]["output_tokens"] == 120
    assert payload["usage"]["cache_read_tokens"] == 640
    assert payload["usage"]["cache_write_tokens"] == 32
    assert payload["usage"]["reasoning_tokens"] == 24
    assert payload["tools"]["count"] == 0


def _member_output_with_metrics() -> RunOutput:
    member = RunOutput(agent_name="GeneralAgent", content="Member answer")
    member.metrics = Metrics(input_tokens=300, output_tokens=50, total_tokens=350, duration=6.0)
    return member


@pytest.mark.asyncio
async def test_team_response_usage_aggregates_member_metrics() -> None:
    """Run-level usage sums leader and member tokens, not just the leader's."""
    orchestrator, _config = _make_orchestrator()
    output = _team_run_output_with_metrics()
    output.member_responses = [_member_output_with_metrics()]
    mock_team = _make_test_team()
    mock_team.arun = AsyncMock(return_value=output)
    collector: dict[str, object] = {}

    patches = _team_patches(mock_team)
    with patches[0], patches[1], patches[2]:
        await team_response(
            agent_names=["general"],
            mode=TeamMode.COORDINATE,
            message="Analyze this.",
            turn_recorder=TurnRecorder(user_message="Analyze this."),
            orchestrator=orchestrator,
            execution_identity=None,
            session_id="session-1",
            run_metadata_collector=collector,
        )

    payload = collector["io.mindroom.ai_run"]
    assert payload["usage"]["input_tokens"] == 1100
    assert payload["usage"]["output_tokens"] == 170
    # Member runs execute inside the leader's window: duration must stay the
    # leader's (1.75), not the sum with the member's 6.0.
    assert payload["usage"]["duration"] == "1.75"


@pytest.mark.asyncio
async def test_team_response_collects_cancelled_run_metadata() -> None:
    """A cancelled team run still publishes its run metadata before raising."""
    orchestrator, _config = _make_orchestrator()
    mock_team = _make_test_team()
    mock_team.arun = AsyncMock(return_value=_team_run_output_with_metrics(status=RunStatus.cancelled))
    collector: dict[str, object] = {}

    patches = _team_patches(mock_team)
    with patches[0], patches[1], patches[2], pytest.raises(asyncio.CancelledError):
        await team_response(
            agent_names=["general"],
            mode=TeamMode.COORDINATE,
            message="Analyze this.",
            turn_recorder=TurnRecorder(user_message="Analyze this."),
            orchestrator=orchestrator,
            execution_identity=None,
            session_id="session-1",
            run_metadata_collector=collector,
        )

    payload = collector["io.mindroom.ai_run"]
    assert payload["status"] == "cancelled"
    assert payload["usage"]["input_tokens"] == 800


@pytest.mark.asyncio
async def test_team_response_stream_collects_run_metadata_from_completed_event() -> None:
    """A realistic events-only stream publishes usage from the run-completed event.

    Real Agno team streams never yield a terminal run output object; the
    streamed run-completed event is the usage and identity source.
    """
    orchestrator, config = _make_orchestrator()

    async def stream() -> AsyncIterator[object]:
        yield AgentRunContentEvent(agent_name="GeneralAgent", content="Member answer")
        yield TeamRunCompletedEvent(
            run_id="team-run-1",
            session_id="session-1",
            metrics=Metrics(input_tokens=800, output_tokens=120, total_tokens=920),
            member_responses=[_member_output_with_metrics()],
        )

    mock_team = _make_test_team()
    mock_team.arun = MagicMock(return_value=stream())
    collector: dict[str, object] = {}

    patches = _team_patches(mock_team)
    with patches[0], patches[1], patches[2]:
        chunks = [
            chunk
            async for chunk in team_response_stream(
                agent_ids=[entity_ids(config, runtime_paths_for(config))["general"]],
                mode=TeamMode.COORDINATE,
                message="Analyze this.",
                turn_recorder=TurnRecorder(user_message="Analyze this."),
                orchestrator=orchestrator,
                execution_identity=None,
                session_id="session-1",
                run_metadata_collector=collector,
            )
        ]

    rendered = "".join(chunk.content if hasattr(chunk, "content") else str(chunk) for chunk in chunks)
    assert "Member answer" in rendered
    payload = collector["io.mindroom.ai_run"]
    assert payload["run_id"] == "team-run-1"
    assert payload["status"] == "completed"
    # Leader (800/120) plus member (300/50) usage.
    assert payload["usage"]["input_tokens"] == 1100
    assert payload["usage"]["output_tokens"] == 170


@pytest.mark.asyncio
async def test_team_response_stream_falls_back_to_model_request_totals() -> None:
    """Without a run-completed event, usage comes from the model-request totals."""
    orchestrator, config = _make_orchestrator()

    async def stream() -> AsyncIterator[object]:
        yield AgentRunContentEvent(agent_name="GeneralAgent", content="Member answer")
        yield TeamModelRequestCompletedEvent(
            model="test-model",
            model_provider="openai",
            input_tokens=500,
            output_tokens=80,
            total_tokens=580,
        )

    mock_team = _make_test_team()
    mock_team.arun = MagicMock(return_value=stream())
    collector: dict[str, object] = {}

    patches = _team_patches(mock_team)
    with patches[0], patches[1], patches[2]:
        _ = [
            chunk
            async for chunk in team_response_stream(
                agent_ids=[entity_ids(config, runtime_paths_for(config))["general"]],
                mode=TeamMode.COORDINATE,
                message="Analyze this.",
                turn_recorder=TurnRecorder(user_message="Analyze this."),
                orchestrator=orchestrator,
                execution_identity=None,
                session_id="session-1",
                run_metadata_collector=collector,
            )
        ]

    payload = collector["io.mindroom.ai_run"]
    assert payload["status"] == "completed"
    assert payload["usage"]["input_tokens"] == 500
    assert payload["usage"]["output_tokens"] == 80


@pytest.mark.asyncio
async def test_team_response_stream_publishes_usage_when_stream_errors() -> None:
    """An errored team stream still publishes the usage it observed.

    The error arm ends the turn with a handled attempt (the driver never sees
    a resolution to publish from), so the attempt must fill the collector
    itself — otherwise billed tokens vanish from the run metadata.
    """
    orchestrator, config = _make_orchestrator()

    async def stream() -> AsyncIterator[object]:
        yield TeamModelRequestCompletedEvent(
            model="test-model",
            model_provider="openai",
            input_tokens=500,
            output_tokens=80,
            total_tokens=580,
        )
        yield TeamRunErrorEvent(content="provider exploded")

    mock_team = _make_test_team()
    mock_team.arun = MagicMock(return_value=stream())
    collector: dict[str, object] = {}

    patches = _team_patches(mock_team)
    with patches[0], patches[1], patches[2]:
        chunks = [
            chunk
            async for chunk in team_response_stream(
                agent_ids=[entity_ids(config, runtime_paths_for(config))["general"]],
                mode=TeamMode.COORDINATE,
                message="Analyze this.",
                turn_recorder=TurnRecorder(user_message="Analyze this."),
                orchestrator=orchestrator,
                execution_identity=None,
                session_id="session-1",
                run_metadata_collector=collector,
            )
        ]

    rendered = "".join(chunk.content if hasattr(chunk, "content") else str(chunk) for chunk in chunks)
    assert "error" in rendered.lower()
    payload = collector["io.mindroom.ai_run"]
    assert payload["status"] == "error"
    assert payload["usage"]["input_tokens"] == 500
    assert payload["usage"]["output_tokens"] == 80


@pytest.mark.asyncio
async def test_team_response_stream_collects_run_metadata_from_terminal_output() -> None:
    """A terminal fallback run output (yield_run_output shape) still publishes metadata."""
    orchestrator, config = _make_orchestrator()

    async def stream() -> AsyncIterator[object]:
        yield _team_run_output_with_metrics()

    mock_team = _make_test_team()
    mock_team.arun = MagicMock(return_value=stream())
    collector: dict[str, object] = {}

    patches = _team_patches(mock_team)
    with patches[0], patches[1], patches[2]:
        chunks = [
            chunk
            async for chunk in team_response_stream(
                agent_ids=[entity_ids(config, runtime_paths_for(config))["general"]],
                mode=TeamMode.COORDINATE,
                message="Analyze this.",
                turn_recorder=TurnRecorder(user_message="Analyze this."),
                orchestrator=orchestrator,
                execution_identity=None,
                session_id="session-1",
                run_metadata_collector=collector,
            )
        ]

    rendered = "".join(chunk.content if hasattr(chunk, "content") else str(chunk) for chunk in chunks)
    assert "Member answer" in rendered
    payload = collector["io.mindroom.ai_run"]
    assert payload["run_id"] == "team-run-1"
    assert payload["status"] == "completed"
    assert payload["usage"]["input_tokens"] == 800
