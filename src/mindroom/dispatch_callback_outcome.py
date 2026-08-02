"""Explicit outcomes for turn-backed Matrix dispatch callbacks."""

from enum import StrEnum


class TurnDispatchOutcome(StrEnum):
    """Ownership disposition returned by one message or media callback."""

    DEFERRED = "deferred"
    INTENTIONALLY_IGNORED = "intentionally_ignored"
