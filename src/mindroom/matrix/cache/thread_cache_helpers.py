"""Shared pure helpers for Matrix thread cache policies."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.matrix.client import ResolvedVisibleMessage


def event_id_from_event_source(event_source: dict[str, object]) -> str | None:
    """Return the event ID when one cached event source contains it."""
    event_id = event_source.get("event_id")
    return event_id if isinstance(event_id, str) else None


def latest_visible_thread_event_id(history: Sequence[ResolvedVisibleMessage]) -> str | None:
    """Return the latest visible event ID from one resolved thread history."""
    if not history:
        return None
    return history[-1].visible_event_id or history[-1].event_id or None
