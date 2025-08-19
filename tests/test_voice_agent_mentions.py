"""Test that voice handler correctly formats agent mentions."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom.config import AgentConfig, Config
from mindroom.voice_handler import _process_transcription


@pytest.mark.asyncio
async def test_voice_correctly_formats_agent_mentions() -> None:
    """Test that voice processing uses correct agent names, not display names."""
    # Create a config with an agent that has different name and display name
    config = MagicMock(spec=Config)
    config.agents = {
        "home": MagicMock(spec=AgentConfig, display_name="HomeAssistant"),
        "research": MagicMock(spec=AgentConfig, display_name="Research Agent"),
    }
    config.teams = {}
    # Mock the voice configuration
    config.voice = MagicMock()
    config.voice.intelligence = MagicMock()
    config.voice.intelligence.model = "test-model"

    # Mock the Agent to return a response that tests our prompt
    # The AI should understand to use @home not @homeassistant
    mock_response = MagicMock()
    mock_response.content = "@home turn on the lights"

    # Test 1: Simple agent mention
    with (
        patch("mindroom.voice_handler.Agent") as mock_agent_class,
        patch("mindroom.voice_handler.get_model_instance") as mock_get_model,
    ):
        mock_agent = MagicMock()
        mock_agent.arun = AsyncMock(return_value=mock_response)
        mock_agent_class.return_value = mock_agent
        mock_get_model.return_value = MagicMock()  # Mock model instance

        result = await _process_transcription("HomeAssistant turn on the lights", config)
        assert result == "@home turn on the lights"

    # Test 2: Agent with command
    mock_response.content = "!schedule in 10 minutes @home turn off the lights"
    with (
        patch("mindroom.voice_handler.Agent") as mock_agent_class,
        patch("mindroom.voice_handler.get_model_instance") as mock_get_model,
    ):
        mock_agent = MagicMock()
        mock_agent.arun = AsyncMock(return_value=mock_response)
        mock_agent_class.return_value = mock_agent
        mock_get_model.return_value = MagicMock()

        result = await _process_transcription(
            "hey home assistant schedule to turn off the lights in 10 minutes",
            config,
        )
        assert result == "!schedule in 10 minutes @home turn off the lights"

    # Test 3: Research agent (multi-word display name)
    mock_response.content = "@research find papers on AI"
    with (
        patch("mindroom.voice_handler.Agent") as mock_agent_class,
        patch("mindroom.voice_handler.get_model_instance") as mock_get_model,
    ):
        mock_agent = MagicMock()
        mock_agent.arun = AsyncMock(return_value=mock_response)
        mock_agent_class.return_value = mock_agent
        mock_get_model.return_value = MagicMock()

        result = await _process_transcription("research agent find papers on AI", config)
        assert result == "@research find papers on AI"


@pytest.mark.asyncio
async def test_voice_prompt_includes_correct_agent_format() -> None:
    """Test that the AI prompt correctly shows agent names vs display names."""
    config = MagicMock(spec=Config)
    config.agents = {
        "home": MagicMock(spec=AgentConfig, display_name="HomeAssistant"),
        "calc": MagicMock(spec=AgentConfig, display_name="Calculator"),
    }
    config.teams = {}
    # Mock the voice configuration
    config.voice = MagicMock()
    config.voice.intelligence = MagicMock()
    config.voice.intelligence.model = "test-model"

    # Capture the prompt sent to the AI
    captured_prompt = None

    async def capture_run(prompt: str, **kwargs: str) -> MagicMock:  # noqa: ARG001
        nonlocal captured_prompt
        captured_prompt = prompt
        mock_resp = MagicMock()
        mock_resp.content = "@home test"
        return mock_resp

    with (
        patch("mindroom.voice_handler.Agent") as mock_agent_class,
        patch("mindroom.voice_handler.get_model_instance") as mock_get_model,
    ):
        mock_agent = MagicMock()
        mock_agent.arun = AsyncMock(side_effect=capture_run)
        mock_agent_class.return_value = mock_agent
        mock_get_model.return_value = MagicMock()

        await _process_transcription("test", config)

        # Verify the prompt shows the correct format
        assert "@home or @mindroom_home (spoken as: HomeAssistant)" in captured_prompt
        assert "@calc or @mindroom_calc (spoken as: Calculator)" in captured_prompt
        assert "use EXACT agent name after @" in captured_prompt
        assert 'use "@home" NOT "@homeassistant"' in captured_prompt
