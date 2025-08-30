"""Test timezone functionality in scheduled tasks."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfoNotFoundError

import pytest

from mindroom.config import Config
from mindroom.scheduling import _format_scheduled_time


def test_format_scheduled_time_utc() -> None:
    """Test formatting with UTC timezone."""
    dt = datetime.now(UTC) + timedelta(hours=2, minutes=30)
    result = _format_scheduled_time(dt, "UTC")

    # Should contain relative time from humanize (e.g., "2 hours from now")
    assert "from now" in result
    assert "hour" in result or "hours" in result
    assert "UTC" in result


def test_format_scheduled_time_eastern() -> None:
    """Test formatting with Eastern timezone."""
    dt = datetime.now(UTC) + timedelta(days=1, hours=3)
    result = _format_scheduled_time(dt, "America/New_York")

    # Should contain relative time from humanize (e.g., "a day from now")
    assert "from now" in result
    assert "day" in result or "hours" in result  # humanize might say "27 hours from now" or "a day from now"
    assert "EST" in result or "EDT" in result  # Depends on daylight savings


def test_format_scheduled_time_minutes() -> None:
    """Test formatting with minutes only."""
    dt = datetime.now(UTC) + timedelta(minutes=45)
    result = _format_scheduled_time(dt, "UTC")

    # Should show minutes from humanize (e.g., "44 minutes from now")
    assert "minute" in result or "minutes" in result
    assert "from now" in result


def test_format_scheduled_time_now() -> None:
    """Test formatting for immediate execution."""
    dt = datetime.now(UTC) + timedelta(seconds=30)
    result = _format_scheduled_time(dt, "UTC")

    # humanize shows "in a few seconds" or "in 30 seconds" for very near future
    assert "second" in result or "few" in result or "moment" in result


def test_format_scheduled_time_past() -> None:
    """Test formatting for past time."""
    dt = datetime.now(UTC) - timedelta(hours=1)
    result = _format_scheduled_time(dt, "UTC")

    # humanize shows "an hour ago" for past times
    assert "ago" in result
    assert "hour" in result or "hours" in result


def test_format_scheduled_time_invalid_timezone() -> None:
    """Test that invalid timezone raises an exception."""
    dt = datetime.now(UTC) + timedelta(hours=2)

    # Should raise an exception for invalid timezone
    with pytest.raises(ZoneInfoNotFoundError):
        _format_scheduled_time(dt, "Invalid/Timezone")


def test_config_timezone_field() -> None:
    """Test that Config accepts timezone field."""
    config = Config(timezone="America/Los_Angeles")
    assert config.timezone == "America/Los_Angeles"

    # Test default
    config_default = Config()
    assert config_default.timezone == "UTC"
