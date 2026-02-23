"""Test that voice handler correctly formats agent mentions."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom.config import AgentConfig, Config
from mindroom.voice_handler import (
    _is_speculative_command_rewrite,
    _process_transcription,
    _sanitize_unavailable_mentions,
)


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


@pytest.mark.asyncio
async def test_voice_prompt_scopes_agents_to_room_entities() -> None:
    """Test that room-scoped entities are the only entities listed in the prompt."""
    config = MagicMock(spec=Config)
    config.agents = {
        "openclaw": MagicMock(spec=AgentConfig, display_name="OpenClaw"),
        "code": MagicMock(spec=AgentConfig, display_name="CodeAgent"),
    }
    config.teams = {}
    config.voice = MagicMock()
    config.voice.intelligence = MagicMock()
    config.voice.intelligence.model = "test-model"

    captured_prompt = None

    async def capture_run(prompt: str, **kwargs: str) -> MagicMock:  # noqa: ARG001
        nonlocal captured_prompt
        captured_prompt = prompt
        mock_resp = MagicMock()
        mock_resp.content = "@openclaw test"
        return mock_resp

    with (
        patch("mindroom.voice_handler.Agent") as mock_agent_class,
        patch("mindroom.voice_handler.get_model_instance") as mock_get_model,
    ):
        mock_agent = MagicMock()
        mock_agent.arun = AsyncMock(side_effect=capture_run)
        mock_agent_class.return_value = mock_agent
        mock_get_model.return_value = MagicMock()

        await _process_transcription(
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
    config = MagicMock(spec=Config)
    config.agents = {
        "openclaw": MagicMock(spec=AgentConfig, display_name="OpenClaw"),
        "code": MagicMock(spec=AgentConfig, display_name="CodeAgent"),
    }
    config.teams = {}
    config.voice = MagicMock()
    config.voice.intelligence = MagicMock()
    config.voice.intelligence.model = "test-model"

    with (
        patch("mindroom.voice_handler.Agent") as mock_agent_class,
        patch("mindroom.voice_handler.get_model_instance") as mock_get_model,
    ):
        mock_agent = MagicMock()
        mock_response = MagicMock()
        mock_response.content = "@code review this"
        mock_agent.arun = AsyncMock(return_value=mock_response)
        mock_agent_class.return_value = mock_agent
        mock_get_model.return_value = MagicMock()

        result = await _process_transcription(
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


@pytest.mark.parametrize(
    ("transcription", "formatted_message", "expected"),
    [
        ("How do agent sessions work?", "!skill session list", True),
        ("Can you explain this concept?", "!help", True),
        ("run skill session list", "!skill session list", False),
        ("help command", "!help", False),
        ("show me help", "!help", False),
        ("What is my schedule today?", "!list_schedules", False),
        ("@research can you help me?", "@research can you help me?", False),
    ],
)
def test_is_speculative_command_rewrite(
    transcription: str,
    formatted_message: str,
    expected: bool,
) -> None:
    """Only explicit command intent should survive command rewrites."""
    assert _is_speculative_command_rewrite(transcription, formatted_message) is expected


@pytest.mark.asyncio
async def test_voice_transcription_rejects_invented_skill_command() -> None:
    """A general sessions question must not be rewritten into !skill."""
    config = MagicMock(spec=Config)
    config.agents = {}
    config.teams = {}
    config.voice = MagicMock()
    config.voice.intelligence = MagicMock()
    config.voice.intelligence.model = "test-model"

    with (
        patch("mindroom.voice_handler.Agent") as mock_agent_class,
        patch("mindroom.voice_handler.get_model_instance") as mock_get_model,
    ):
        mock_agent = MagicMock()
        mock_response = MagicMock()
        mock_response.content = "!skill session list"
        mock_agent.arun = AsyncMock(return_value=mock_response)
        mock_agent_class.return_value = mock_agent
        mock_get_model.return_value = MagicMock()

        transcription = "How do agent sessions work?"
        result = await _process_transcription(transcription, config)

    assert result == transcription


@pytest.mark.asyncio
async def test_voice_transcription_keeps_explicit_skill_command() -> None:
    """Explicit skill intent should continue to produce !skill commands."""
    config = MagicMock(spec=Config)
    config.agents = {}
    config.teams = {}
    config.voice = MagicMock()
    config.voice.intelligence = MagicMock()
    config.voice.intelligence.model = "test-model"

    with (
        patch("mindroom.voice_handler.Agent") as mock_agent_class,
        patch("mindroom.voice_handler.get_model_instance") as mock_get_model,
    ):
        mock_agent = MagicMock()
        mock_response = MagicMock()
        mock_response.content = "!skill session list"
        mock_agent.arun = AsyncMock(return_value=mock_response)
        mock_agent_class.return_value = mock_agent
        mock_get_model.return_value = MagicMock()

        result = await _process_transcription("run skill session list", config)

    assert result == "!skill session list"


@pytest.mark.asyncio
async def test_voice_transcription_rejects_invented_help_command() -> None:
    """A general question must not be rewritten into !help."""
    config = MagicMock(spec=Config)
    config.agents = {}
    config.teams = {}
    config.voice = MagicMock()
    config.voice.intelligence = MagicMock()
    config.voice.intelligence.model = "test-model"

    with (
        patch("mindroom.voice_handler.Agent") as mock_agent_class,
        patch("mindroom.voice_handler.get_model_instance") as mock_get_model,
    ):
        mock_agent = MagicMock()
        mock_response = MagicMock()
        mock_response.content = "!help"
        mock_agent.arun = AsyncMock(return_value=mock_response)
        mock_agent_class.return_value = mock_agent
        mock_get_model.return_value = MagicMock()

        transcription = "What is photosynthesis?"
        result = await _process_transcription(transcription, config)

    assert result == transcription


@pytest.mark.asyncio
async def test_voice_transcription_keeps_explicit_help_command() -> None:
    """Explicit help intent should continue to produce !help commands."""
    config = MagicMock(spec=Config)
    config.agents = {}
    config.teams = {}
    config.voice = MagicMock()
    config.voice.intelligence = MagicMock()
    config.voice.intelligence.model = "test-model"

    with (
        patch("mindroom.voice_handler.Agent") as mock_agent_class,
        patch("mindroom.voice_handler.get_model_instance") as mock_get_model,
    ):
        mock_agent = MagicMock()
        mock_response = MagicMock()
        mock_response.content = "!help"
        mock_agent.arun = AsyncMock(return_value=mock_response)
        mock_agent_class.return_value = mock_agent
        mock_get_model.return_value = MagicMock()

        result = await _process_transcription("help command", config)

    assert result == "!help"
