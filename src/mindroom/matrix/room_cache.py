"""Helpers for reading nio's in-memory room cache safely."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import nio


def cached_rooms(client: nio.AsyncClient) -> dict[str, nio.MatrixRoom]:
    """Return the client room cache when nio has initialized it.

    Uses ``vars()`` to read the ``rooms`` dict directly from the instance
    without triggering ``__getattr__``. This is intentional: in tests the
    client is often an ``AsyncMock`` whose attribute protocol would return a
    coroutine-mock instead of the plain dict that was assigned.
    """
    rooms = vars(client).get("rooms")
    return rooms if isinstance(rooms, dict) else {}


def cached_room(client: nio.AsyncClient, room_id: str) -> nio.MatrixRoom | None:
    """Return one cached room when it is available."""
    return cached_rooms(client).get(room_id)
