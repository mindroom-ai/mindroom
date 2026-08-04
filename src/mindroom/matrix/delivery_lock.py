"""Application-owned Matrix room delivery serialization."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from threading import Lock
from typing import TYPE_CHECKING
from weakref import WeakKeyDictionary

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import nio


_ROOM_DELIVERY_LOCKS: WeakKeyDictionary[nio.AsyncClient, dict[str, asyncio.Lock]] = WeakKeyDictionary()
_ROOM_DELIVERY_LOCKS_GUARD = Lock()


def _client_room_delivery_lock(client: nio.AsyncClient, room_id: str) -> asyncio.Lock:
    """Return the application-owned delivery lock for one client room."""
    with _ROOM_DELIVERY_LOCKS_GUARD:
        client_locks = _ROOM_DELIVERY_LOCKS.setdefault(client, {})
        return client_locks.setdefault(room_id, asyncio.Lock())


@asynccontextmanager
async def room_delivery_lock(client: nio.AsyncClient, room_id: str) -> AsyncIterator[None]:
    """Serialize one room's application sends and local state mutations."""
    async with _client_room_delivery_lock(client, room_id):
        yield


__all__ = ["room_delivery_lock"]
