"""Shared startup error types."""

from __future__ import annotations


class PermanentStartupError(ValueError):
    """Raised for startup failures that should not be retried."""
