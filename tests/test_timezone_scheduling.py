"""Test timezone functionality in scheduled tasks."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta, tzinfo
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfoNotFoundError

import pytest
from pydantic import ValidationError

from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.scheduling.tasks import _format_scheduled_time

if TYPE_CHECKING:
    from collections.abc import Iterator


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


def test_format_scheduled_time_now() -> None:
    """Test formatting for immediate execution."""
    dt = datetime.now(UTC) + timedelta(seconds=30)
    result = _format_scheduled_time(dt, "UTC")

    # humanize shows "in a few seconds" or "in 30 seconds" for very near future
    assert "second" in result or "few" in result or "moment" in result


@pytest.fixture
def _host_on_us_pacific_time() -> Iterator[None]:
    """Run the host's local clock on US Pacific rules, written as a POSIX rule so no tz database is needed."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("TZ", "PST8PDT,M3.2.0,M11.1.0")
        time.tzset()
        yield
    time.tzset()


@pytest.mark.usefixtures("_host_on_us_pacific_time")
@pytest.mark.parametrize(
    ("now", "scheduled_at", "expected"),
    [
        # Los Angeles wall clocks show 01:00-02:00 twice on 2026-11-01: first PDT (08:00-09:00 UTC), then PST.
        pytest.param(
            datetime(2026, 11, 1, 8, 30, tzinfo=UTC),
            datetime(2026, 11, 1, 9, 15, tzinfo=UTC),
            "2026-11-01 01:15 PST (45 minutes from now)",
            id="ahead",
        ),
        pytest.param(
            datetime(2026, 11, 1, 9, 30, tzinfo=UTC),
            datetime(2026, 11, 1, 8, 30, tzinfo=UTC),
            "2026-11-01 01:30 PDT (an hour ago)",
            id="behind",
        ),
    ],
)
def test_relative_time_is_exact_in_the_repeated_fall_back_hour(
    monkeypatch: pytest.MonkeyPatch,
    now: datetime,
    scheduled_at: datetime,
    expected: str,
) -> None:
    """The relative part counts real elapsed time, even when the host's local wall clock repeats an hour."""

    class _Clock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:
            return now.astimezone(tz)

    monkeypatch.setattr("mindroom.scheduling.tasks.datetime", _Clock)

    assert _format_scheduled_time(scheduled_at, "America/Los_Angeles") == expected


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


def test_agent_timezone_overrides_the_global_timezone_and_falls_back_to_it() -> None:
    """An agent's own timezone wins; agents without one, teams, the router, and agentless jobs use the global one."""
    config = Config(
        timezone="America/Los_Angeles",
        agents={
            "atlas": AgentConfig(display_name="Atlas", timezone="Europe/Amsterdam"),
            "nova": AgentConfig(display_name="Nova"),
        },
        teams={"crew": TeamConfig(display_name="Crew", role="Pair", agents=["atlas", "nova"])},
    )

    assert config.resolve_scope("atlas").timezone == "Europe/Amsterdam"
    assert config.resolve_scope("nova").timezone == "America/Los_Angeles"
    assert config.resolve_scope("crew").timezone == "America/Los_Angeles"
    assert config.resolve_scope("router").timezone == "America/Los_Angeles"
    assert config.resolve_scope(None).timezone == "America/Los_Angeles"


@pytest.mark.parametrize("zone", ["Mars/Olympus_Mons", "", "../etc/localtime"])
def test_unknown_timezones_are_rejected_by_config_validation(zone: str) -> None:
    """Both the global and the per-agent timezone must name an IANA zone."""
    with pytest.raises(ValidationError, match="timezone"):
        AgentConfig(display_name="Atlas", timezone=zone)
    with pytest.raises(ValidationError, match="timezone"):
        Config(timezone=zone)
