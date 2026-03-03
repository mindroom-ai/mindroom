"""Tests for team inline-media fallback behavior."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from agno.run.team import RunContentEvent as TeamRunContentEvent
from agno.run.team import RunErrorEvent as TeamRunErrorEvent
from agno.run.team import TeamRunOutput

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.media_fallback import clear_inline_audio_capability_cache
from mindroom.media_inputs import MediaInputs
from mindroom.teams import TeamMode, _team_response_stream_raw, team_response, team_response_stream

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


def _build_test_config(model_config: ModelConfig | None = None) -> Config:
    models = {"default": model_config} if model_config else {}
    return Config(
        agents={
            "general": AgentConfig(display_name="GeneralAgent", rooms=["#test:example.org"]),
        },
        models=models,
    )


@pytest.fixture(autouse=True)
def _clear_inline_audio_cache() -> None:
    clear_inline_audio_capability_cache()


@pytest.mark.asyncio
async def test_team_response_retries_without_inline_media_on_validation_error() -> None:
    """Non-streaming team response should retry once without inline media."""
    config = _build_test_config()
    orchestrator = MagicMock()
    orchestrator.config = config

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

    media_validation_error = "Error code: 500 - audio input is not supported"

    mock_team = MagicMock()
    mock_team.arun = MagicMock(side_effect=Exception(media_validation_error))
    audio_input = MagicMock(name="audio_input")

    with (
        patch("mindroom.teams._get_agents_from_orchestrator", return_value=[MagicMock(name="GeneralAgent")]),
        patch("mindroom.teams._create_team_instance", return_value=mock_team),
    ):
        raw_stream = await _team_response_stream_raw(
            agent_ids=[config.ids["general"]],
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
    """Unknown capability paths should keep first audio attempt and rely on existing retry behavior."""
    config = _build_test_config(
        ModelConfig(
            provider="openai",
            id="gpt-4o",
            extra_kwargs={"base_url": "https://api.openai.com/v1"},
        ),
    )
    orchestrator = MagicMock()
    orchestrator.config = config

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
                agent_ids=[config.ids["general"]],
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
async def test_team_stream_does_not_preflight_inline_audio_for_ollama() -> None:
    """Ollama should use existing retry behavior rather than blanket audio preflight."""
    config = _build_test_config(
        ModelConfig(
            provider="ollama",
            id="llama3",
        ),
    )
    orchestrator = MagicMock()
    orchestrator.config = config

    media_validation_error = "Error code: 500 - audio input is not supported"

    async def successful_stream() -> AsyncIterator[object]:
        yield TeamRunContentEvent(content="Recovered ollama stream")

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
                agent_ids=[config.ids["general"]],
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
    assert "Recovered ollama stream" in rendered_output


@pytest.mark.asyncio
async def test_team_stream_preflight_strips_inline_audio_after_learning_unsupported_model() -> None:
    """A model that previously rejected audio should preflight-drop audio next time."""
    config = _build_test_config(
        ModelConfig(
            provider="openai",
            id="gpt-4o",
            extra_kwargs={"base_url": "https://api.openai.com/v1"},
        ),
    )
    orchestrator = MagicMock()
    orchestrator.config = config

    media_validation_error = "Error code: 500 - audio input is not supported"

    async def recovered_stream() -> AsyncIterator[object]:
        yield TeamRunContentEvent(content="Recovered stream")

    async def preflight_stream() -> AsyncIterator[object]:
        yield TeamRunContentEvent(content="Preflight stream")

    mock_team_first = MagicMock()
    mock_team_first.arun = MagicMock(side_effect=[Exception(media_validation_error), recovered_stream()])
    mock_team_second = MagicMock()
    mock_team_second.arun = MagicMock(return_value=preflight_stream())
    audio_input = MagicMock(name="audio_input")

    with (
        patch("mindroom.teams._get_agents_from_orchestrator", return_value=[MagicMock(name="GeneralAgent")]),
        patch("mindroom.teams._create_team_instance", side_effect=[mock_team_first, mock_team_first, mock_team_second]),
    ):
        first_chunks = [
            chunk
            async for chunk in team_response_stream(
                agent_ids=[config.ids["general"]],
                mode=TeamMode.COORDINATE,
                message="Analyze this.",
                orchestrator=orchestrator,
                media=MediaInputs(audio=[audio_input]),
            )
        ]
        second_chunks = [
            chunk
            async for chunk in team_response_stream(
                agent_ids=[config.ids["general"]],
                mode=TeamMode.COORDINATE,
                message="Analyze this.",
                orchestrator=orchestrator,
                media=MediaInputs(audio=[audio_input]),
            )
        ]

    assert mock_team_first.arun.call_count == 2
    first_run_call = mock_team_first.arun.call_args_list[0]
    first_run_retry = mock_team_first.arun.call_args_list[1]
    assert list(first_run_call.kwargs["audio"]) == [audio_input]
    assert list(first_run_retry.kwargs["audio"]) == []
    assert "Inline media unavailable for this model" in first_run_retry.args[0]
    first_rendered_output = "".join(
        chunk.content if hasattr(chunk, "content") else str(chunk) for chunk in first_chunks
    )
    assert "Recovered stream" in first_rendered_output

    assert mock_team_second.arun.call_count == 1
    second_run_call = mock_team_second.arun.call_args_list[0]
    assert list(second_run_call.kwargs["audio"]) == []
    assert "Inline media unavailable for this model" in second_run_call.args[0]
    rendered_output = "".join(chunk.content if hasattr(chunk, "content") else str(chunk) for chunk in second_chunks)
    assert "Preflight stream" in rendered_output


@pytest.mark.asyncio
async def test_team_stream_retries_without_inline_media_on_streamed_run_error() -> None:
    """Team streaming should retry on streamed run errors before any output is emitted."""
    config = _build_test_config()
    orchestrator = MagicMock()
    orchestrator.config = config

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
                agent_ids=[config.ids["general"]],
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
