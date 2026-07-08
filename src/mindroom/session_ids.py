"""Leaf helpers for the canonical persisted room/thread session ID.

Kept free of matrix-client imports so the tool registry chain can build
session IDs without dragging in ``nio`` (#1436).
"""

from __future__ import annotations


def create_session_id(room_id: str, thread_id: str | None) -> str:
    """Create a session ID with thread awareness."""
    # Thread sessions include thread ID
    return f"{room_id}:{thread_id}" if thread_id else room_id


def parse_session_id(session_id: str) -> tuple[str, str | None]:
    """Parse the canonical persisted room/thread session ID."""
    room_id, marker, thread_suffix = session_id.rpartition(":$")
    return (room_id, f"${thread_suffix}") if marker else (session_id, None)
