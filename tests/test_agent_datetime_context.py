"""Test that agents receive stable date context in their prompts."""

from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest

from mindroom.agent_loop.build import prompt_for_turn
from mindroom.agents import create_agent
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig
from mindroom.prompts import DATETIME_CONTEXT_TEMPLATE
from mindroom.system_prompt import render_date_context
from mindroom.teams.members import build_team_spec, materialize_exact_team_members
from mindroom.teams.spec import TeamMode
from tests.conftest import bind_runtime_paths, install_model_providers, runtime_paths_for, test_runtime_paths
from tests.fakes.scripted_model import ScriptedModel

if TYPE_CHECKING:
    from mindroom.agent_loop.build import AgentBuild


def _datetime_test_config() -> Config:
    """Build a deterministic config for datetime prompt tests."""
    runtime_paths = test_runtime_paths(Path(tempfile.mkdtemp()))
    return bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(
                    display_name="GeneralAgent",
                    role="General assistant",
                    rooms=[],
                    include_default_tools=False,
                ),
            },
            defaults=DefaultsConfig(tools=[]),
        ),
        runtime_paths,
    )


def test_render_date_context_format() -> None:
    """Test the datetime context formatting."""
    frozen_now = datetime(2026, 3, 20, 13, 30, tzinfo=ZoneInfo("America/New_York"))
    with patch("mindroom.system_prompt.datetime") as mock_datetime:
        mock_datetime.now.return_value = frozen_now
        context = render_date_context("America/New_York", datetime_context_template=DATETIME_CONTEXT_TEMPLATE)

    assert context == (
        "## Current Date and Time\nToday is Friday, March 20, 2026.\nTimezone: America/New_York (EDT)\n\n"
    )


def test_render_date_context_utc() -> None:
    """Test datetime context with UTC timezone."""
    frozen_now = datetime(2026, 3, 20, 8, 15, tzinfo=ZoneInfo("UTC"))
    with patch("mindroom.system_prompt.datetime") as mock_datetime:
        mock_datetime.now.return_value = frozen_now
        context = render_date_context("UTC", datetime_context_template=DATETIME_CONTEXT_TEMPLATE)

    assert context == ("## Current Date and Time\nToday is Friday, March 20, 2026.\nTimezone: UTC (UTC)\n\n")


def test_render_date_context_invalid_timezone() -> None:
    """Test that invalid timezone raises ZoneInfoNotFoundError."""
    with pytest.raises(ZoneInfoNotFoundError):
        render_date_context("Invalid/Timezone", datetime_context_template=DATETIME_CONTEXT_TEMPLATE)


def _build(config: Config, monkeypatch: pytest.MonkeyPatch) -> AgentBuild:
    """Build the general agent on the scripted provider."""
    install_model_providers(monkeypatch, {"default": ScriptedModel([])})
    return create_agent("general", config, runtime_paths_for(config), execution_identity=None)


def test_agent_prompt_includes_datetime(monkeypatch: pytest.MonkeyPatch) -> None:
    """Date context remains in the system prompt after the shared role."""
    config = _datetime_test_config()
    config.timezone = "America/Los_Angeles"

    with patch("mindroom.system_prompt.datetime") as mock_datetime:
        mock_datetime.now.side_effect = lambda tz: datetime(2026, 3, 20, 8, 15, tzinfo=tz)
        build = _build(config, monkeypatch)

    role = build.prompt.role
    assert role is not None

    assert "## Your Identity" in role
    assert "You are GeneralAgent" in role
    assert "@mindroom_general" in role

    assert "General assistant" in role
    assert "## Current Date and Time" not in role
    message = prompt_for_turn(build)
    assert isinstance(message.content, str)
    assert "## Current Date and Time" in message.content
    assert "Today is Friday, March 20, 2026." in message.content
    assert "Timezone: America/Los_Angeles (PDT)" in message.content
    assert "The current time is" not in message.content
    assert message.content.index("General assistant") < message.content.index("## Current Date and Time")


def test_agent_prompt_datetime_changes_with_timezone(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that changing timezone in config changes the prompt timezone line."""
    config = _datetime_test_config()

    with patch("mindroom.system_prompt.datetime") as mock_datetime:
        mock_datetime.now.side_effect = lambda tz: datetime(2026, 3, 20, 8, 15, tzinfo=tz)
        config.timezone = "America/New_York"
        agent_ny = _build(config, monkeypatch)

        config.timezone = "Asia/Tokyo"
        agent_tokyo = _build(config, monkeypatch)

    assert agent_ny.prompt.turn_context is not None
    assert agent_tokyo.prompt.turn_context is not None
    assert "Timezone: America/New_York (EDT)" in agent_ny.prompt.turn_context
    assert "Timezone: Asia/Tokyo (JST)" in agent_tokyo.prompt.turn_context
    assert agent_ny.prompt.role == agent_tokyo.prompt.role


def test_agent_prompt_datetime_stable_within_same_day(monkeypatch: pytest.MonkeyPatch) -> None:
    """System prompt date context should stay identical across turns within one day."""
    config = _datetime_test_config()
    config.timezone = "UTC"

    with patch("mindroom.system_prompt.datetime") as mock_datetime:
        mock_datetime.now.side_effect = [
            datetime(2026, 3, 20, 0, 1, tzinfo=ZoneInfo("UTC")),
            datetime(2026, 3, 20, 23, 59, tzinfo=ZoneInfo("UTC")),
        ]
        first_agent = _build(config, monkeypatch)
        second_agent = _build(config, monkeypatch)

    assert first_agent.prompt.role == second_agent.prompt.role
    assert first_agent.prompt.turn_context == second_agent.prompt.turn_context
    assert first_agent.prompt.turn_context is not None
    assert "Today is Friday, March 20, 2026." in first_agent.prompt.turn_context


def test_team_leader_keeps_date_when_members_have_stable_roles(monkeypatch: pytest.MonkeyPatch) -> None:
    """Moving dates out of member roles must not remove the leader's date context."""
    config = _datetime_test_config()
    config.timezone = "UTC"
    runtime_paths = runtime_paths_for(config)
    install_model_providers(monkeypatch, {"default": ScriptedModel([])})
    with patch("mindroom.system_prompt.datetime") as mock_datetime:
        mock_datetime.now.return_value = datetime(2026, 3, 20, 8, 15, tzinfo=ZoneInfo("UTC"))
        members = materialize_exact_team_members(
            ["general"],
            config=config,
            runtime_paths=runtime_paths,
            execution_identity=None,
        )
        team = build_team_spec(
            members=members,
            mode=TeamMode.COORDINATE,
            config=config,
            runtime_paths=runtime_paths,
            team_display_name="Team-general",
            scope_context=None,
            execution_identity=None,
            model_name="default",
            configured_team_name=None,
        )

    [member] = members.builds
    assert member.prompt.role is not None
    assert "## Current Date and Time" not in member.prompt.role
    message = team.spec.system_message
    assert message is not None
    assert isinstance(message.content, str)
    assert "Today is Friday, March 20, 2026." in message.content
    assert "Timezone: UTC (UTC)" in message.content
