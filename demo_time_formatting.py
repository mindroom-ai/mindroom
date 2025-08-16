"""Demo of the new timezone and humanized time formatting."""

from datetime import UTC, datetime, timedelta

from mindroom.config import Config
from mindroom.scheduling import _format_scheduled_time


def demo() -> None:
    """Show examples of the new time formatting."""
    print("=== MindRoom Scheduled Task Time Formatting Demo ===\n")

    # Test with different timezones
    timezones = ["UTC", "America/New_York", "Europe/London", "Asia/Tokyo"]

    # Test with different time deltas
    test_cases = [
        ("30 seconds from now", timedelta(seconds=30)),
        ("5 minutes from now", timedelta(minutes=5)),
        ("45 minutes from now", timedelta(minutes=45)),
        ("2 hours from now", timedelta(hours=2)),
        ("1 day from now", timedelta(days=1)),
        ("3 days from now", timedelta(days=3)),
        ("1 week from now", timedelta(weeks=1)),
        ("1 hour ago", timedelta(hours=-1)),
    ]

    for name, delta in test_cases:
        dt = datetime.now(UTC) + delta
        print(f"\n{name}:")
        for tz in timezones:
            formatted = _format_scheduled_time(dt, tz)
            print(f"  {tz:20} → {formatted}")

    # Show Config with timezone
    print("\n\n=== Config Example ===")
    config = Config(timezone="America/Los_Angeles")
    print(f"Config timezone: {config.timezone}")

    # Example of how it looks in a scheduled task message
    print("\n=== Example Scheduled Task Message ===")
    dt = datetime.now(UTC) + timedelta(hours=2, minutes=30)
    formatted = _format_scheduled_time(dt, "America/New_York")
    print(f"✅ Scheduled for {formatted}")
    print("Task: Daily standup reminder")
    print("Will post: @team Time for our daily standup!")


if __name__ == "__main__":
    demo()
