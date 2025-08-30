"""Test that agents receive current date and time context in their prompts."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest

from mindroom.agents import create_agent, get_datetime_context
from mindroom.config import Config


def test_get_datetime_context_format() -> None:
    """Test the datetime context formatting."""
    context = get_datetime_context("America/New_York")

    # Should have the header
    assert "## Current Date and Time" in context

    # Should have today's date
    assert "Today is" in context

    # Should have current time
    assert "The current time is" in context

    # Should show timezone
    assert "America/New_York timezone" in context

    # Should have proper 24-hour time format (e.g., "13:30 EST")
    # Check for HH:MM format with 2 digits for hour and minute
    assert re.search(r"\d{2}:\d{2}", context) is not None
    assert "EST" in context or "EDT" in context  # Eastern time zones


def test_get_datetime_context_utc() -> None:
    """Test datetime context with UTC timezone."""
    context = get_datetime_context("UTC")

    assert "## Current Date and Time" in context
    assert "UTC timezone" in context
    assert "UTC" in context  # Should appear in the time string too


def test_get_datetime_context_invalid_timezone() -> None:
    """Test that invalid timezone raises ZoneInfoNotFoundError."""
    with pytest.raises(ZoneInfoNotFoundError):
        get_datetime_context("Invalid/Timezone")


def test_agent_prompt_includes_datetime() -> None:
    """Test that agent's role prompt includes datetime context."""
    # Create a test config
    config = Config.from_yaml(Path("config.yaml"))
    config.timezone = "America/Los_Angeles"

    # Create an agent
    agent = create_agent("general", config)

    # Check that the role includes all expected sections
    role = agent.role

    # Should have identity context
    assert "## Your Identity" in role
    assert "You are GeneralAgent" in role
    assert "@mindroom_general" in role

    # Should have datetime context
    assert "## Current Date and Time" in role
    assert "Today is" in role
    assert "The current time is" in role
    assert "America/Los_Angeles timezone" in role

    # Should have time in Pacific timezone (24-hour format)
    assert re.search(r"\d{2}:\d{2}", role) is not None  # HH:MM format
    assert "PST" in role or "PDT" in role  # Pacific time zones

    # Should have the actual agent prompt content
    assert "## Core Expertise" in role  # From GENERAL_AGENT_PROMPT


def test_agent_prompt_datetime_changes_with_timezone() -> None:
    """Test that changing timezone in config changes the agent's datetime context."""
    config = Config.from_yaml(Path("config.yaml"))

    # Test with New York timezone
    config.timezone = "America/New_York"
    agent_ny = create_agent("general", config)

    # Test with Tokyo timezone
    config.timezone = "Asia/Tokyo"
    agent_tokyo = create_agent("general", config)

    # The prompts should be different (different timezones)
    assert "America/New_York timezone" in agent_ny.role
    assert "Asia/Tokyo timezone" in agent_tokyo.role

    # Time zones should be different
    assert "EST" in agent_ny.role or "EDT" in agent_ny.role
    assert "JST" in agent_tokyo.role

    # Same date format but potentially different dates due to timezone
    # (e.g., if it's late evening in NY, it's already tomorrow in Tokyo)
    # Both should have weekday and month
    for role in [agent_ny.role, agent_tokyo.role]:
        # Check for weekday names
        weekdays = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        assert any(day in role for day in weekdays)

        # Check for month names
        months = [
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        ]
        assert any(month in role for month in months)


def test_datetime_context_is_current() -> None:
    """Test that the datetime context shows the current time (within a minute)."""
    context = get_datetime_context("UTC")

    # Extract the time from the context
    # Looking for pattern like "13:30 UTC" (24-hour format)
    time_match = re.search(r"(\d{2}):(\d{2}) UTC", context)
    assert time_match is not None

    hour = int(time_match.group(1))
    minute = int(time_match.group(2))

    # Get current UTC time
    now = datetime.now(ZoneInfo("UTC"))

    # Check that the time is current (within 1 minute)
    # This accounts for the time between when context was generated and when we check
    time_diff = abs(now.hour - hour) * 60 + abs(now.minute - minute)

    # Handle edge case of time wrapping around midnight
    if time_diff > 720:  # More than 12 hours difference
        time_diff = 1440 - time_diff  # 24 hours - time_diff

    assert time_diff <= 1, (
        f"Time in context ({hour:02d}:{minute:02d}) differs from current time ({now.hour:02d}:{now.minute:02d}) by {time_diff} minutes"
    )
