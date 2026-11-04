"""A room that the current task context sends into only from inside an allowed scope.

A contact turn guards its contact room, so everything it runs, its tools and their middleware included, reaches that
room only through a send the contact's message policy approved. Tasks a guarded context starts inherit the guard.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

_GUARDED_ROOM: ContextVar[str | None] = ContextVar("guarded_room", default=None)
_SEND_ALLOWED: ContextVar[bool] = ContextVar("guarded_room_send_allowed", default=False)


@contextmanager
def guarded_room(room_id: str) -> Iterator[None]:
    """Refuse every send into ``room_id`` from this context unless it runs inside ``allowed_send``."""
    token = _GUARDED_ROOM.set(room_id)
    try:
        yield
    finally:
        _GUARDED_ROOM.reset(token)


@contextmanager
def allowed_send() -> Iterator[None]:
    """Let the sends made inside this scope reach the guarded room; use it only around a send its policy approved."""
    token = _SEND_ALLOWED.set(True)
    try:
        yield
    finally:
        _SEND_ALLOWED.reset(token)


def send_refused(room_id: str) -> bool:
    """Return whether a send into ``room_id`` is refused here: it is the guarded room, outside any allowed scope."""
    return room_id == _GUARDED_ROOM.get() and not _SEND_ALLOWED.get()
