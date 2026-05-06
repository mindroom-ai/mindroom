"""Shared Matrix sync-token value normalization."""

from __future__ import annotations


def normalize_sync_token(value: object) -> str | None:
    """Return a stripped sync token or ``None`` for invalid or empty values."""
    if not isinstance(value, str):
        return None
    return value.strip() or None
