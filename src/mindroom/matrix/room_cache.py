"""Helpers for reading nio's in-memory room cache safely."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import nio


def cached_rooms(client: nio.AsyncClient) -> dict[str, nio.MatrixRoom]:
    """Return the client room cache when nio has initialized it.

    ``AsyncClient.rooms`` is the source of truth. Non-dict values are treated
    as an empty cache so simple test doubles can opt in by assigning a real
    ``rooms`` dict.
    """
    rooms = client.rooms
    return rooms if isinstance(rooms, dict) else {}


def cached_room(client: nio.AsyncClient, room_id: str) -> nio.MatrixRoom | None:
    """Return one cached room when it is available."""
    return cached_rooms(client).get(room_id)
