"""Tests for team inline-media fallback behavior."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from agno.media import File
from agno.run.team import RunContentEvent as TeamRunContentEvent
from agno.run.team import TeamRunOutput

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.media_inputs import MediaInputs
from mindroom.teams import TeamMode, _team_response_stream_raw, team_response

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


def _build_test_config() -> Config:
    return Config(
        agents={
            "general": AgentConfig(display_name="GeneralAgent", rooms=["#test:example.org"]),
        },
    )


@pytest.mark.asyncio
async def test_team_response_retries_without_inline_media_on_validation_error(tmp_path: Path) -> None:
    """Non-streaming team response should retry once without inline media."""
    config = _build_test_config()
    orchestrator = MagicMock()
    orchestrator.config = config

    media_validation_error = (
        "litellm.BadRequestError: invalid_request_error: "
        "document.source.base64.media_type: Input should be 'application/pdf'"
    )
    mock_team = MagicMock()
    mock_team.arun = AsyncMock(
        side_effect=[
            Exception(media_validation_error),
            TeamRunOutput(content="Recovered team response"),
        ],
    )
    document_file = File(
        filepath=str(tmp_path / "report.pdf"),
        filename="report.pdf",
        mime_type="application/pdf",
    )

    with (
        patch("mindroom.teams._get_agents_from_orchestrator", return_value=[MagicMock(name="GeneralAgent")]),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
    ):
        response = await team_response(
            agent_names=["general"],
            mode=TeamMode.COORDINATE,
            message="Analyze this.",
            orchestrator=orchestrator,
            media=MediaInputs(files=[document_file]),
        )

    assert "Recovered team response" in response
    assert mock_team.arun.await_count == 2
    first_call = mock_team.arun.await_args_list[0]
    second_call = mock_team.arun.await_args_list[1]
    assert list(first_call.kwargs["files"]) == [document_file]
    assert list(second_call.kwargs["files"]) == []
    assert "Inline media unavailable for this model" in second_call.args[0]


@pytest.mark.asyncio
async def test_team_stream_raw_retries_without_inline_media_on_validation_error(tmp_path: Path) -> None:
    """Streaming team setup should retry once without inline media."""
    config = _build_test_config()
    orchestrator = MagicMock()
    orchestrator.config = config

    media_validation_error = (
        "litellm.BadRequestError: invalid_request_error: "
        "document.source.base64.media_type: Input should be 'application/pdf'"
    )

    async def successful_stream() -> AsyncIterator[object]:
        yield TeamRunContentEvent(content="Recovered stream")

    mock_team = MagicMock()
    mock_team.arun = MagicMock(side_effect=[Exception(media_validation_error), successful_stream()])
    document_file = File(
        filepath=str(tmp_path / "report.pdf"),
        filename="report.pdf",
        mime_type="application/pdf",
    )

    with (
        patch("mindroom.teams._get_agents_from_orchestrator", return_value=[MagicMock(name="GeneralAgent")]),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
    ):
        raw_stream = await _team_response_stream_raw(
            agent_ids=[config.ids["general"]],
            mode=TeamMode.COORDINATE,
            message="Analyze this.",
            orchestrator=orchestrator,
            media=MediaInputs(files=[document_file]),
        )
        events = [event async for event in raw_stream]

    assert mock_team.arun.call_count == 2
    first_call = mock_team.arun.call_args_list[0]
    second_call = mock_team.arun.call_args_list[1]
    assert list(first_call.kwargs["files"]) == [document_file]
    assert list(second_call.kwargs["files"]) == []
    assert "Inline media unavailable for this model" in second_call.args[0]
    assert any(isinstance(event, TeamRunContentEvent) and event.content == "Recovered stream" for event in events)
