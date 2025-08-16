"""Test timezone functionality in scheduled tasks."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from mindroom.config import Config
from mindroom.scheduling import _format_scheduled_time


def test_format_scheduled_time_utc() -> None:
    """Test formatting with UTC timezone."""
    dt = datetime.now(UTC) + timedelta(hours=2, minutes=30)
    result = _format_scheduled_time(dt, "UTC")

    # Should contain relative time
    assert "in 2 hours" in result
    assert "UTC" in result


def test_format_scheduled_time_eastern() -> None:
    """Test formatting with Eastern timezone."""
    dt = datetime.now(UTC) + timedelta(days=1, hours=3)
    result = _format_scheduled_time(dt, "America/New_York")

    # Should contain relative time (allowing for slight time differences)
    assert "in 1 day" in result
    assert "EST" in result or "EDT" in result  # Depends on daylight savings


def test_format_scheduled_time_minutes() -> None:
    """Test formatting with minutes only."""
    dt = datetime.now(UTC) + timedelta(minutes=45)
    result = _format_scheduled_time(dt, "UTC")

    # Should show minutes when less than a day (allowing for execution time)
    assert "minute" in result
    assert "in" in result


def test_format_scheduled_time_now() -> None:
    """Test formatting for immediate execution."""
    dt = datetime.now(UTC) + timedelta(seconds=30)
    result = _format_scheduled_time(dt, "UTC")

    # Should show "now" for very near future
    assert "now" in result


def test_format_scheduled_time_past() -> None:
    """Test formatting for past time."""
    dt = datetime.now(UTC) - timedelta(hours=1)
    result = _format_scheduled_time(dt, "UTC")

    # Should indicate it's in the past
    assert "in the past" in result


def test_format_scheduled_time_invalid_timezone() -> None:
    """Test fallback for invalid timezone."""
    dt = datetime.now(UTC) + timedelta(hours=2)
    result = _format_scheduled_time(dt, "Invalid/Timezone")

    # Should fallback to UTC format
    assert "UTC" in result


def test_config_timezone_field() -> None:
    """Test that Config accepts timezone field."""
    config = Config(timezone="America/Los_Angeles")
    assert config.timezone == "America/Los_Angeles"

    # Test default
    config_default = Config()
    assert config_default.timezone == "UTC"
