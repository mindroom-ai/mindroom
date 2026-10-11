"""Shared startup error types."""

from __future__ import annotations


class PermanentStartupError(ValueError):
    """Raised for startup failures that should not be retried."""


class EventJournalHoldLostError(RuntimeError):
    """Raised after a runtime stopped because it no longer held its event journal; a supervisor restarts it."""
