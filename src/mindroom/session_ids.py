"""Leaf helpers for the canonical persisted room/thread session ID.

Kept free of matrix-client imports so the tool registry chain can build
session IDs without dragging in ``nio`` (#1436).
"""

from __future__ import annotations

_LISTENING_SESSION_SUFFIX = ":listen"


def create_session_id(room_id: str, thread_id: str | None) -> str:
    """Create a session ID with thread awareness."""
    # Thread sessions include thread ID
    return f"{room_id}:{thread_id}" if thread_id else room_id


def session_in_room(session_id: str, room_id: str) -> bool:
    """Return whether a session ID names a conversation in this room: the room's own, or one of its threads."""
    return session_id == room_id or session_id.startswith(f"{room_id}:")


def listening_session_id(room_id: str) -> str:
    """Return the session of a listening agent's silent turns in one room, which is none of the room's conversations."""
    return f"{room_id}{_LISTENING_SESSION_SUFFIX}"


def is_listening_session(session_id: str) -> bool:
    """Return whether a session ID names a room's silent turns; a thread's session holds the thread's ``$`` event ID."""
    return session_id.endswith(_LISTENING_SESSION_SUFFIX) and "$" not in session_id
