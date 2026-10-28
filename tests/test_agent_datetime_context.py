"""Test that agents receive stable date context in their prompts."""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime
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
from mindroom.llm.messages import system_text
from mindroom.system_prompt import render_session_context
from mindroom.teams.members import build_team_spec, resolve_exact_team_members
from mindroom.teams.spec import TeamMode
from tests.conftest import (
    bind_runtime_paths,
    install_model_providers,
    runtime_paths_for,
    test_runtime_paths,
    unstored_team_scope_context,
)
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


def test_the_session_context_names_the_date_and_timezone() -> None:
    """The session context holds the date and the timezone it is rendered in, without a clock time."""
    frozen_now = datetime(2026, 3, 20, 13, 30, tzinfo=ZoneInfo("America/New_York"))
    with patch("mindroom.system_prompt.datetime") as mock_datetime:
        mock_datetime.now.return_value = frozen_now
        context = render_session_context(Config(), "America/New_York")

    assert context == (
        "<mindroom_session_context>\n## Current Date and Time\nToday is Friday, March 20, 2026.\n"
        "Timezone: America/New_York (EDT)\n</mindroom_session_context>"
    )


def test_the_session_context_in_utc() -> None:
    """The default timezone is UTC."""
    frozen_now = datetime(2026, 3, 20, 8, 15, tzinfo=ZoneInfo("UTC"))
    with patch("mindroom.system_prompt.datetime") as mock_datetime:
        mock_datetime.now.return_value = frozen_now
        context = render_session_context(Config(), Config().timezone)

    assert "Today is Friday, March 20, 2026.\nTimezone: UTC (UTC)\n" in context


def test_the_session_context_rejects_an_unknown_timezone() -> None:
    """An unknown timezone raises ZoneInfoNotFoundError."""
    with pytest.raises(ZoneInfoNotFoundError):
        render_session_context(Config(), "Invalid/Timezone")


def _build(config: Config, monkeypatch: pytest.MonkeyPatch) -> AgentBuild:
    """Build the general agent on the scripted provider."""
    install_model_providers(monkeypatch, {"default": ScriptedModel([])})
    return create_agent(
        "general",
        config,
        runtime_paths_for(config),
        execution_identity=None,
        knowledge=None,
        active_model_name=None,
    )


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
    stable, turn = prompt_for_turn(build)
    assert isinstance(turn.content, str)
    assert "## Current Date and Time" in turn.content
    assert "Today is Friday, March 20, 2026." in turn.content
    assert "Timezone: America/Los_Angeles (PDT)" in turn.content
    assert "The current time is" not in turn.content
    assert "General assistant" in str(stable.content)
    assert "## Current Date and Time" not in str(stable.content)


def test_agent_timezone_overrides_the_global_timezone_in_the_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    """An agent with its own timezone sees its own date, even when the install's timezone is still on the day before."""
    config = _datetime_test_config()
    config.timezone = "America/Los_Angeles"
    config.agents["general"].timezone = "Europe/Amsterdam"
    instant = datetime(2026, 3, 20, 23, 30, tzinfo=UTC)

    with patch("mindroom.system_prompt.datetime") as mock_datetime:
        mock_datetime.now.side_effect = instant.astimezone
        build = _build(config, monkeypatch)

    assert build.prompt.turn_context is not None
    assert "Today is Saturday, March 21, 2026." in build.prompt.turn_context
    assert "Timezone: Europe/Amsterdam (CET)" in build.prompt.turn_context


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
        members = resolve_exact_team_members(
            ["general"],
            config=config,
            runtime_paths=runtime_paths,
            execution_identity=None,
            refresh_scheduler=None,
            active_model_names=None,
        )
        team = build_team_spec(
            members=members,
            mode=TeamMode.COORDINATE,
            config=config,
            runtime_paths=runtime_paths,
            scope_context=unstored_team_scope_context(config, ["general"], team_name=None),
            execution_identity=None,
            model_name="default",
            configured_team_name=None,
            timezone="UTC",
        )
        member = members.build("general", members.model_names["general"])

    assert member.prompt.role is not None
    assert "## Current Date and Time" not in member.prompt.role
    leader_prompt = system_text(team.spec.leader.system_messages)
    assert "Today is Friday, March 20, 2026." in leader_prompt
    assert "Timezone: UTC (UTC)" in leader_prompt
