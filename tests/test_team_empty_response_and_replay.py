"""Tests for the team empty-run guard and recorder-less interrupted replay."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.run.team import TeamRunOutput

from mindroom import ai_runtime
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


def _empty_team_run(run_id: str) -> TeamRunOutput:
    output = TeamRunOutput(content="", run_id=run_id, session_id="session-1")
    output.status = RunStatus.completed
    return output


def _completed_team_run(content: str) -> TeamRunOutput:
    output = TeamRunOutput(content=content, run_id="team-run-final", session_id="session-1")
    output.status = RunStatus.completed
    return output


def _cancelled_team_run() -> TeamRunOutput:
    output = TeamRunOutput(
        content="",
        run_id="team-run-1",
        session_id="session-1",
        member_responses=[RunOutput(agent_name="GeneralAgent", content="partial member text")],
    )
    output.status = RunStatus.cancelled
    return output


@pytest.mark.asyncio
async def test_team_response_retries_once_after_empty_completed_run() -> None:
    """One empty completed team run is discarded and retried before answering."""
    orchestrator, _config = _make_orchestrator()
    mock_team = _make_test_team()
    mock_team.arun = AsyncMock(side_effect=[_empty_team_run("team-run-1"), _completed_team_run("Recovered answer")])

    patches = _team_patches(mock_team)
    with patches[0], patches[1], patches[2]:
        response = await team_response(
            agent_names=["general"],
            mode=TeamMode.COORDINATE,
            message="Say something.",
            turn_recorder=TurnRecorder(user_message="Say something."),
            orchestrator=orchestrator,
            execution_identity=None,
            session_id="session-1",
        )

    assert "Recovered answer" in response
    assert mock_team.arun.await_count == 2


@pytest.mark.asyncio
async def test_team_response_returns_fallback_notice_when_retry_is_also_empty() -> None:
    """A second empty completed team run surfaces the shared fallback notice."""
    orchestrator, _config = _make_orchestrator()
    mock_team = _make_test_team()
    mock_team.arun = AsyncMock(side_effect=[_empty_team_run("team-run-1"), _empty_team_run("team-run-2")])
    recorder = TurnRecorder(user_message="Say something.")

    patches = _team_patches(mock_team)
    with patches[0], patches[1], patches[2]:
        response = await team_response(
            agent_names=["general"],
            mode=TeamMode.COORDINATE,
            message="Say something.",
            turn_recorder=recorder,
            orchestrator=orchestrator,
            execution_identity=None,
            session_id="session-1",
        )

    assert response == ai_runtime.EMPTY_RESPONSE_NOTICE
    assert mock_team.arun.await_count == 2
    # The fallback notice stays out of the recorded turn.
    assert recorder.outcome == "completed"
    assert recorder.assistant_text == ""


@pytest.mark.asyncio
async def test_team_response_stream_yields_fallback_notice_when_retry_is_also_empty() -> None:
    """The streaming empty-run guard retries once, then yields the notice chunk."""
    orchestrator, config = _make_orchestrator()

    async def empty_stream(run_id: str) -> AsyncIterator[object]:
        yield _empty_team_run(run_id)

    mock_team = _make_test_team()
    mock_team.arun = MagicMock(side_effect=[empty_stream("team-run-1"), empty_stream("team-run-2")])

    patches = _team_patches(mock_team)
    with patches[0], patches[1], patches[2]:
        chunks = [
            chunk
            async for chunk in team_response_stream(
                agent_ids=[entity_ids(config, runtime_paths_for(config))["general"]],
                mode=TeamMode.COORDINATE,
                message="Say something.",
                turn_recorder=TurnRecorder(user_message="Say something."),
                orchestrator=orchestrator,
                execution_identity=None,
                session_id="session-1",
            )
        ]

    assert mock_team.arun.call_count == 2
    rendered = "".join(chunk.content if hasattr(chunk, "content") else str(chunk) for chunk in chunks)
    assert ai_runtime.EMPTY_RESPONSE_NOTICE in rendered


@pytest.mark.asyncio
async def test_team_response_stream_empty_event_stream_retries_then_notices() -> None:
    """A stream that ends with no events at all still triggers the empty-run guard.

    Real Agno team streams never emit a terminal run output object, so the
    guard must fire from the plain end-of-stream resolution.
    """
    orchestrator, config = _make_orchestrator()

    async def silent_stream() -> AsyncIterator[object]:
        return
        yield  # pragma: no cover - makes this an async generator

    mock_team = _make_test_team()
    mock_team.arun = MagicMock(side_effect=[silent_stream(), silent_stream()])
    recorder = TurnRecorder(user_message="Say something.")

    patches = _team_patches(mock_team)
    with patches[0], patches[1], patches[2]:
        chunks = [
            chunk
            async for chunk in team_response_stream(
                agent_ids=[entity_ids(config, runtime_paths_for(config))["general"]],
                mode=TeamMode.COORDINATE,
                message="Say something.",
                turn_recorder=recorder,
                orchestrator=orchestrator,
                execution_identity=None,
                session_id="session-1",
            )
        ]

    assert mock_team.arun.call_count == 2
    rendered = "".join(chunk.content if hasattr(chunk, "content") else str(chunk) for chunk in chunks)
    assert ai_runtime.EMPTY_RESPONSE_NOTICE in rendered
    # The notice-only turn records an empty completion, not the placeholder document.
    assert recorder.outcome == "completed"
    assert recorder.assistant_text == ""


@pytest.mark.asyncio
async def test_team_response_without_recorder_persists_interrupted_replay() -> None:
    """A recorder-less cancelled team run persists a standalone interrupted replay."""
    orchestrator, _config = _make_orchestrator()
    mock_team = _make_test_team()
    mock_team.arun = AsyncMock(return_value=_cancelled_team_run())

    patches = _team_patches(mock_team)
    with (
        patches[0],
        patches[1],
        patches[2],
        patch("mindroom.teams.persist_interrupted_replay") as mock_persist,
        pytest.raises(asyncio.CancelledError),
    ):
        await team_response(
            agent_names=["general"],
            mode=TeamMode.COORDINATE,
            message="Analyze this.",
            orchestrator=orchestrator,
            execution_identity=None,
            session_id="session-1",
        )

    assert mock_persist.call_count == 1
    persist_kwargs = mock_persist.call_args.kwargs
    assert persist_kwargs["is_team"] is True
    assert persist_kwargs["session_id"] == "session-1"
    assert persist_kwargs["user_message"] == "Analyze this."
    assert "partial member text" in persist_kwargs["partial_text"]


@pytest.mark.asyncio
async def test_team_response_without_recorder_persists_replay_on_external_cancel() -> None:
    """An external task cancel without a recorder persists the fallback replay once."""
    orchestrator, _config = _make_orchestrator()
    mock_team = _make_test_team()
    mock_team.arun = AsyncMock(side_effect=asyncio.CancelledError)

    patches = _team_patches(mock_team)
    with (
        patches[0],
        patches[1],
        patches[2],
        patch("mindroom.teams.persist_interrupted_replay") as mock_persist,
        pytest.raises(asyncio.CancelledError),
    ):
        await team_response(
            agent_names=["general"],
            mode=TeamMode.COORDINATE,
            message="Analyze this.",
            orchestrator=orchestrator,
            execution_identity=None,
            session_id="session-1",
        )

    assert mock_persist.call_count == 1
    persist_kwargs = mock_persist.call_args.kwargs
    assert persist_kwargs["is_team"] is True
    assert persist_kwargs["partial_text"] == ""
