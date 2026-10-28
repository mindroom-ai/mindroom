"""Shared startup error types."""

from __future__ import annotations


class PermanentStartupError(ValueError):
    """Raised for startup failures that should not be retried."""


class EventJournalLostError(RuntimeError):
    """Raised after a runtime lost its event journal's lock session or a journal connection; a supervisor restarts it."""
