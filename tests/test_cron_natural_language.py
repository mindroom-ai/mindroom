"""Tests for cron natural language descriptions."""

from __future__ import annotations

import pytest

from mindroom.scheduling import CronSchedule


class TestCronNaturalLanguage:
    """Test CronSchedule natural language descriptions."""

    def test_every_minute(self) -> None:
        """Test converting every minute cron to natural language."""
        schedule = CronSchedule(minute="*", hour="*", day="*", month="*", weekday="*")
        assert schedule.to_cron_string() == "* * * * *"
        description = schedule.to_natural_language()
        assert "Every minute" in description

    def test_every_two_minutes(self) -> None:
        """Test converting every 2 minutes cron to natural language."""
        schedule = CronSchedule(minute="*/2", hour="*", day="*", month="*", weekday="*")
        assert schedule.to_cron_string() == "*/2 * * * *"
        description = schedule.to_natural_language()
        assert "2 minutes" in description or "2 minute" in description

    def test_every_five_minutes(self) -> None:
        """Test converting every 5 minutes cron to natural language."""
        schedule = CronSchedule(minute="*/5", hour="*", day="*", month="*", weekday="*")
        assert schedule.to_cron_string() == "*/5 * * * *"
        description = schedule.to_natural_language()
        assert "5 minutes" in description or "5 minute" in description

    def test_daily_at_9am(self) -> None:
        """Test converting daily at 9am cron to natural language."""
        schedule = CronSchedule(minute="0", hour="9", day="*", month="*", weekday="*")
        assert schedule.to_cron_string() == "0 9 * * *"
        description = schedule.to_natural_language()
        # Could be "At 09:00" or "At 9:00 AM" depending on locale
        assert "09:00" in description or "9:00" in description

    def test_weekly_monday_at_9am(self) -> None:
        """Test converting weekly Monday at 9am cron to natural language."""
        schedule = CronSchedule(minute="0", hour="9", day="*", month="*", weekday="1")
        assert schedule.to_cron_string() == "0 9 * * 1"
        description = schedule.to_natural_language()
        assert "Monday" in description
        assert "09:00" in description or "9:00" in description

    def test_hourly(self) -> None:
        """Test converting hourly cron to natural language."""
        schedule = CronSchedule(minute="0", hour="*", day="*", month="*", weekday="*")
        assert schedule.to_cron_string() == "0 * * * *"
        description = schedule.to_natural_language()
        # Should contain "hour" in some form
        assert "hour" in description.lower()

    def test_every_4_hours(self) -> None:
        """Test converting every 4 hours cron to natural language."""
        schedule = CronSchedule(minute="0", hour="*/4", day="*", month="*", weekday="*")
        assert schedule.to_cron_string() == "0 */4 * * *"
        description = schedule.to_natural_language()
        assert "4 hour" in description

    def test_complex_schedule(self) -> None:
        """Test converting complex cron to natural language."""
        schedule = CronSchedule(minute="15,45", hour="9,17", day="*", month="*", weekday="1-5")
        assert schedule.to_cron_string() == "15,45 9,17 * * 1-5"
        description = schedule.to_natural_language()
        # Should mention minutes 15 and 45
        assert "15" in description
        assert "45" in description

    def test_description_fallback_on_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Falls back to raw cron string if description fails."""
        schedule = CronSchedule(minute="*/5")

        def boom(*args, **kwargs) -> None:  # noqa: ANN002, ANN003, ARG001
            msg = "descriptor failure"
            raise ValueError(msg)

        # On main, get_description is imported in mindroom.scheduling
        monkeypatch.setattr("mindroom.scheduling.get_description", boom)
        assert schedule.to_natural_language().startswith("Cron: ")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
