from enum import StrEnum


class TimelineEventProvenance(StrEnum):
    """Whether an event is live, continuity-recovered, or cold history."""

    LIVE = "live"
    RECOVERED = "recovered"
    HISTORY = "history"
