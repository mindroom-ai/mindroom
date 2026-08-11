"""Shared Matrix sync-token values and their normalization.

``SyncCheckpoint`` lives here rather than with the certifier that produces one
because the durable continuity record contains it: the checkpoint outlives
whatever decided to save it, and its persisted shape must stay readable for as
long as `mindroom_data/sync_continuity/` does.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SyncCheckpoint:
    """A sync token saved after its sync response was durably recorded."""

    token: str
    store_generation: str | None = None


def normalize_sync_token(value: object) -> str | None:
    """Return a stripped sync token or ``None`` for invalid or empty values."""
    if not isinstance(value, str):
        return None
    return value.strip() or None
