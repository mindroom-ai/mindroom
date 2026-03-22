"""Test that agents receive stable date context in their prompts."""

from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest
from agno.models.ollama import Ollama

from mindroom.agents import _get_datetime_context, create_agent
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths


def _datetime_test_config() -> Config:
    """Build a deterministic config for datetime prompt tests."""
    runtime_paths = test_runtime_paths(Path(tempfile.mkdtemp()))
    return bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(
                    display_name="GeneralAgent",
                    rooms=[],
                    include_default_tools=False,
                ),
            },
            defaults=DefaultsConfig(tools=[]),
        ),
        runtime_paths,
    )


def test_get_datetime_context_format() -> None:
    """Test the datetime context formatting."""
    frozen_now = datetime(2026, 3, 20, 13, 30, tzinfo=ZoneInfo("America/New_York"))
    with patch("mindroom.agents.datetime") as mock_datetime:
        mock_datetime.now.return_value = frozen_now
        context = _get_datetime_context("America/New_York")

    assert context == (
        "## Current Date and Time\nToday is Friday, March 20, 2026.\nTimezone: America/New_York (EDT)\n\n"
    )


def test_get_datetime_context_utc() -> None:
    """Test datetime context with UTC timezone."""
    frozen_now = datetime(2026, 3, 20, 8, 15, tzinfo=ZoneInfo("UTC"))
    with patch("mindroom.agents.datetime") as mock_datetime:
        mock_datetime.now.return_value = frozen_now
        context = _get_datetime_context("UTC")

    assert context == ("## Current Date and Time\nToday is Friday, March 20, 2026.\nTimezone: UTC (UTC)\n\n")


def test_get_datetime_context_invalid_timezone() -> None:
    """Test that invalid timezone raises ZoneInfoNotFoundError."""
    with pytest.raises(ZoneInfoNotFoundError):
        _get_datetime_context("Invalid/Timezone")


def test_agent_prompt_includes_datetime() -> None:
    """Test that agent's role prompt includes datetime context."""
    config = _datetime_test_config()
    config.timezone = "America/Los_Angeles"
    runtime_paths = runtime_paths_for(config)
    model = Ollama(id="test-model")

    with (
        patch("mindroom.agents.datetime") as mock_datetime,
        patch("mindroom.ai.get_model_instance", return_value=model),
    ):
        mock_datetime.now.side_effect = lambda tz: datetime(2026, 3, 20, 8, 15, tzinfo=tz)
        agent = create_agent("general", config, runtime_paths, execution_identity=None)

    role = agent.role

    assert "## Your Identity" in role
    assert "You are GeneralAgent" in role
    assert "@mindroom_general" in role

    assert "## Current Date and Time" in role
    assert "Today is Friday, March 20, 2026." in role
    assert "Timezone: America/Los_Angeles (PDT)" in role
    assert "The current time is" not in role
    assert "## Core Expertise" in role


def test_agent_prompt_datetime_changes_with_timezone() -> None:
    """Test that changing timezone in config changes the prompt timezone line."""
    config = _datetime_test_config()
    runtime_paths = runtime_paths_for(config)
    model = Ollama(id="test-model")

    with (
        patch("mindroom.agents.datetime") as mock_datetime,
        patch("mindroom.ai.get_model_instance", return_value=model),
    ):
        mock_datetime.now.side_effect = lambda tz: datetime(2026, 3, 20, 8, 15, tzinfo=tz)
        config.timezone = "America/New_York"
        agent_ny = create_agent("general", config, runtime_paths, execution_identity=None)

        config.timezone = "Asia/Tokyo"
        agent_tokyo = create_agent("general", config, runtime_paths, execution_identity=None)

    assert "Timezone: America/New_York (EDT)" in agent_ny.role
    assert "Timezone: Asia/Tokyo (JST)" in agent_tokyo.role
    assert agent_ny.role != agent_tokyo.role


def test_agent_prompt_datetime_stable_within_same_day() -> None:
    """System prompt date context should stay identical across turns within one day."""
    config = _datetime_test_config()
    config.timezone = "UTC"
    runtime_paths = runtime_paths_for(config)
    model = Ollama(id="test-model")

    with (
        patch("mindroom.agents.datetime") as mock_datetime,
        patch("mindroom.ai.get_model_instance", return_value=model),
    ):
        mock_datetime.now.side_effect = [
            datetime(2026, 3, 20, 0, 1, tzinfo=ZoneInfo("UTC")),
            datetime(2026, 3, 20, 23, 59, tzinfo=ZoneInfo("UTC")),
        ]
        first_agent = create_agent("general", config, runtime_paths, execution_identity=None)
        second_agent = create_agent("general", config, runtime_paths, execution_identity=None)

    assert first_agent.role == second_agent.role
