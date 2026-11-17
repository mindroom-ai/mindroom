"""The Matrix events the current task context sent, for the turn that keeps them.

A tool call records the events it sends into its turn's list, so the run that made the call stores them as seen: its
call and result already hold what it sent. Tasks a recording context starts record into the same list.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

_SENT_EVENT_IDS: ContextVar[list[str] | None] = ContextVar("sent_event_ids", default=None)


@contextmanager
def recording_sent_events(sent_event_ids: list[str] | None) -> Iterator[None]:
    """Record every event this context sends in ``sent_event_ids``; None records none."""
    token = _SENT_EVENT_IDS.set(sent_event_ids)
    try:
        yield
    finally:
        _SENT_EVENT_IDS.reset(token)


def record_sent_event(event_id: str) -> None:
    """Record one event the homeserver accepted, when this context records what it sends."""
    sent_event_ids = _SENT_EVENT_IDS.get()
    if sent_event_ids is not None:
        sent_event_ids.append(event_id)
