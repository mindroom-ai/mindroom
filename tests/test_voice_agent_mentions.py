"""Test that voice handler normalizes mentions without rewriting commands."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.voice_handler import (
    _process_transcription,
    _sanitize_unavailable_mentions,
)
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths


def _voice_config(agent_display_names: dict[str, str]) -> Config:
    runtime_paths = test_runtime_paths(Path(tempfile.mkdtemp()))
    config = bind_runtime_paths(
        Config(
            agents={
                agent_name: AgentConfig(display_name=display_name)
                for agent_name, display_name in agent_display_names.items()
            },
            models={"default": ModelConfig(provider="ollama", id="test-model")},
        ),
        runtime_paths,
    )
    config.voice.intelligence.model = "test-model"
    return config


async def _process_transcription_for_test(transcription: str, config: Config, **kwargs: object) -> str:
    """Run voice transcription processing with the test config's runtime context."""
    return await _process_transcription(transcription, config, runtime_paths_for(config), **kwargs)


@pytest.mark.asyncio
async def test_voice_correctly_formats_agent_mentions() -> None:
    """Test that voice processing uses correct agent names, not display names."""
    # Create a config with an agent that has different name and display name
    config = _voice_config(
        {
            "home": "HomeAssistant",
            "research": "Research Agent",
        },
    )

    # Mock the Agent to return a response that tests our prompt
    # The AI should understand to use @home not @homeassistant
    mock_response = MagicMock()
    mock_response.content = "@home turn on the lights"

    # Test 1: Simple agent mention
    with (
        patch("mindroom.voice_handler.Agent") as mock_agent_class,
        patch("mindroom.model_loading.get_model_instance") as mock_get_model,
    ):
        mock_agent = MagicMock()
        mock_agent.arun = AsyncMock(return_value=mock_response)
        mock_agent_class.return_value = mock_agent
        mock_get_model.return_value = MagicMock()  # Mock model instance

        result = await _process_transcription_for_test("HomeAssistant turn on the lights", config)
        assert result == "@home turn on the lights"

    # Test 2: Agent mention stays natural language
    mock_response.content = "@home schedule to turn off the lights in 10 minutes"
    with (
        patch("mindroom.voice_handler.Agent") as mock_agent_class,
        patch("mindroom.model_loading.get_model_instance") as mock_get_model,
    ):
        mock_agent = MagicMock()
        mock_agent.arun = AsyncMock(return_value=mock_response)
        mock_agent_class.return_value = mock_agent
        mock_get_model.return_value = MagicMock()

        result = await _process_transcription_for_test(
            "hey home assistant schedule to turn off the lights in 10 minutes",
            config,
        )
        assert result == "@home schedule to turn off the lights in 10 minutes"

    # Test 3: Research agent (multi-word display name)
    mock_response.content = "@research find papers on AI"
    with (
        patch("mindroom.voice_handler.Agent") as mock_agent_class,
        patch("mindroom.model_loading.get_model_instance") as mock_get_model,
    ):
        mock_agent = MagicMock()
        mock_agent.arun = AsyncMock(return_value=mock_response)
        mock_agent_class.return_value = mock_agent
        mock_get_model.return_value = MagicMock()

        result = await _process_transcription_for_test("research agent find papers on AI", config)
        assert result == "@research find papers on AI"


@pytest.mark.asyncio
async def test_voice_prompt_includes_correct_agent_format() -> None:
    """Test that the AI prompt correctly shows agent names vs display names."""
    config = _voice_config(
        {
            "home": "HomeAssistant",
            "calc": "Calculator",
        },
    )

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
        patch("mindroom.model_loading.get_model_instance") as mock_get_model,
    ):
        mock_agent = MagicMock()
        mock_agent.arun = AsyncMock(side_effect=capture_run)
        mock_agent_class.return_value = mock_agent
        mock_get_model.return_value = MagicMock()

        await _process_transcription_for_test("test", config)

        # Verify the prompt shows the correct format
        assert "@home or @mindroom_home (spoken as: HomeAssistant)" in captured_prompt
        assert "@calc or @mindroom_calc (spoken as: Calculator)" in captured_prompt
        assert "use EXACT agent name after @" in captured_prompt
        assert 'use "@home" NOT "@homeassistant"' in captured_prompt
        assert "NEVER rewrite speech into Matrix bot commands" in captured_prompt
        assert "!schedule" not in captured_prompt
        assert "!help" not in captured_prompt
        assert "!skill" not in captured_prompt


@pytest.mark.asyncio
async def test_voice_prompt_scopes_agents_to_room_entities() -> None:
    """Test that room-scoped entities are the only entities listed in the prompt."""
    config = _voice_config(
        {
            "openclaw": "OpenClaw",
            "code": "CodeAgent",
        },
    )

    captured_prompt = None

    async def capture_run(prompt: str, **kwargs: str) -> MagicMock:  # noqa: ARG001
        nonlocal captured_prompt
        captured_prompt = prompt
        mock_resp = MagicMock()
        mock_resp.content = "@openclaw test"
        return mock_resp

    with (
        patch("mindroom.voice_handler.Agent") as mock_agent_class,
        patch("mindroom.model_loading.get_model_instance") as mock_get_model,
    ):
        mock_agent = MagicMock()
        mock_agent.arun = AsyncMock(side_effect=capture_run)
        mock_agent_class.return_value = mock_agent
        mock_get_model.return_value = MagicMock()

        await _process_transcription_for_test(
            "test",
            config,
            available_agent_names=["openclaw"],
            available_team_names=[],
        )

    assert "@openclaw or @mindroom_openclaw (spoken as: OpenClaw)" in captured_prompt
    assert "@code or @mindroom_code (spoken as: CodeAgent)" not in captured_prompt
    assert "Available teams (use EXACT team name after @):\n  (none)" in captured_prompt


@pytest.mark.asyncio
async def test_voice_transcription_strips_unavailable_entity_mentions() -> None:
    """Test that configured but unavailable entities are not left as mentions."""
    config = _voice_config(
        {
            "openclaw": "OpenClaw",
            "code": "CodeAgent",
        },
    )

    with (
        patch("mindroom.voice_handler.Agent") as mock_agent_class,
        patch("mindroom.model_loading.get_model_instance") as mock_get_model,
    ):
        mock_agent = MagicMock()
        mock_response = MagicMock()
        mock_response.content = "@code review this"
        mock_agent.arun = AsyncMock(return_value=mock_response)
        mock_agent_class.return_value = mock_agent
        mock_get_model.return_value = MagicMock()

        result = await _process_transcription_for_test(
            "review this",
            config,
            available_agent_names=["openclaw"],
            available_team_names=[],
        )

    assert result == "code review this"


@pytest.mark.parametrize(
    ("text", "allowed_entities", "configured_entities", "expected"),
    [
        ("@code review this", {"openclaw"}, {"openclaw", "code"}, "code review this"),
        ("@mindroom_code review this", {"openclaw"}, {"openclaw", "code"}, "mindroom_code review this"),
        ("@code:server.com review this", {"openclaw"}, {"openclaw", "code"}, "code:server.com review this"),
        ("@openclaw review this", {"openclaw"}, {"openclaw", "code"}, "@openclaw review this"),
        ("@unknown review this", {"openclaw"}, {"openclaw", "code"}, "@unknown review this"),
        ("@Code review this", {"openclaw"}, {"openclaw", "code"}, "Code review this"),
        ("@openclaw ask @code to help", {"openclaw"}, {"openclaw", "code"}, "@openclaw ask code to help"),
        ("", {"openclaw"}, {"openclaw", "code"}, ""),
        ("no mentions in this sentence", {"openclaw"}, {"openclaw", "code"}, "no mentions in this sentence"),
    ],
)
def test_sanitize_unavailable_mentions_direct(
    text: str,
    allowed_entities: set[str],
    configured_entities: set[str],
    expected: str,
) -> None:
    """Test direct sanitizer behavior for mention edge cases."""
    result = _sanitize_unavailable_mentions(
        text,
        allowed_entities=allowed_entities,
        configured_entities=configured_entities,
    )
    assert result == expected


@pytest.mark.asyncio
async def test_voice_transcription_does_not_rewrite_schedule_language_to_command() -> None:
    """Voice normalization should keep schedule phrasing as plain text."""
    config = _voice_config({})

    with (
        patch("mindroom.voice_handler.Agent") as mock_agent_class,
        patch("mindroom.model_loading.get_model_instance") as mock_get_model,
    ):
        mock_agent = MagicMock()
        mock_response = MagicMock()
        mock_response.content = "schedule something tomorrow"
        mock_agent.arun = AsyncMock(return_value=mock_response)
        mock_agent_class.return_value = mock_agent
        mock_get_model.return_value = MagicMock()

        result = await _process_transcription_for_test("schedule something tomorrow", config)

    assert result == "schedule something tomorrow"
    assert not result.startswith("!")
