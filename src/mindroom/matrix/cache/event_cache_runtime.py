"""Runtime lifecycle and lock coordination for the SQLite-backed Matrix event cache."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from mindroom.logging_config import get_logger

from . import event_cache_lifecycle

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    import aiosqlite


_LOCK_WAIT_LOG_THRESHOLD_SECONDS = 0.1
_MAX_CACHED_ROOM_LOCKS = 256
logger = get_logger(__name__)


@dataclass
class _RoomLockEntry:
    """Track one room lock plus queued users that still rely on it."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    active_users: int = 0


class _EventCacheRuntime:
    """Own the runtime-only lifecycle state for one event cache instance."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None
        self._disabled_reason: str | None = None
        # One shared SQLite connection must serialize lifecycle changes with all
        # in-flight DB operations so shutdown cannot close it mid-query.
        self._db_lock = asyncio.Lock()
        # These locks preserve logical room ordering for the advisory cache and
        # keep contention visible in logs even though DB operations are gated by
        # the shared connection lock above.
        self._room_locks: OrderedDict[str, _RoomLockEntry] = OrderedDict()

    @property
    def db_path(self) -> Path:
        """Return the SQLite database path for this cache instance."""
        return self._db_path

    @property
    def db(self) -> aiosqlite.Connection | None:
        """Return the active SQLite connection, if initialized."""
        return self._db

    @property
    def is_initialized(self) -> bool:
        """Return whether the SQLite connection is currently open."""
        return self._db is not None

    @property
    def is_disabled(self) -> bool:
        """Return whether the advisory cache is disabled for this runtime."""
        return self._disabled_reason is not None

    def disable(self, reason: str) -> None:
        """Disable the advisory cache for the rest of the runtime."""
        if self._disabled_reason is not None:
            return
        self._disabled_reason = reason
        logger.warning(
            "Disabling advisory Matrix event cache",
            db_path=str(self._db_path),
            reason=reason,
        )

    async def initialize(self) -> None:
        """Open the SQLite database and create the cache schema."""
        async with self._db_lock:
            if self._disabled_reason is not None or self._db is not None:
                return
            self._db = await event_cache_lifecycle.initialize_event_cache_db(self._db_path)

    async def close(self) -> None:
        """Close the SQLite connection when the cache is no longer needed."""
        async with self._db_lock:
            if self._db is None:
                return
            await self._db.close()
            self._db = None
            self._room_locks.clear()

    def room_lock_entry(self, room_id: str, *, active_user_increment: int = 0) -> _RoomLockEntry:
        """Return the cached room lock entry, creating it on demand."""
        entry = self._room_locks.get(room_id)
        if entry is None:
            entry = _RoomLockEntry(active_users=active_user_increment)
        else:
            entry.active_users += active_user_increment
        self._room_locks[room_id] = entry
        self._room_locks.move_to_end(room_id)
        self._prune_room_locks()
        return entry

    @asynccontextmanager
    async def acquire_room_lock(self, room_id: str, *, operation: str) -> AsyncIterator[None]:
        """Serialize runtime-visible work for one room."""
        entry = self.room_lock_entry(room_id, active_user_increment=1)
        wait_started = time.perf_counter()
        acquired = False
        try:
            await entry.lock.acquire()
            acquired = True
            wait_time = time.perf_counter() - wait_started
            if wait_time > _LOCK_WAIT_LOG_THRESHOLD_SECONDS:
                logger.debug(
                    "Waited for _EventCache room lock",
                    room_id=room_id,
                    operation=operation,
                    wait_time_ms=round(wait_time * 1000, 2),
                )
            yield
        finally:
            if acquired:
                entry.lock.release()
            entry.active_users -= 1
            if entry.active_users == 0:
                self._prune_room_locks()

    @asynccontextmanager
    async def acquire_db_operation(
        self,
        room_id: str,
        *,
        operation: str,
    ) -> AsyncIterator[aiosqlite.Connection]:
        """Serialize one DB operation with lifecycle changes and room ordering."""
        if self._db is None:
            await self.initialize()
        async with self._db_lock, self.acquire_room_lock(room_id, operation=operation):
            yield self.require_db()

    def require_db(self) -> aiosqlite.Connection:
        """Return the active SQLite connection or raise if uninitialized."""
        if self._db is None:
            msg = "_EventCache has not been initialized"
            raise RuntimeError(msg)
        return self._db

    def _prune_room_locks(self) -> None:
        while len(self._room_locks) > _MAX_CACHED_ROOM_LOCKS:
            evicted_room_id: str | None = None
            for cached_room_id, cached_entry in self._room_locks.items():
                if cached_entry.active_users > 0:
                    continue
                evicted_room_id = cached_room_id
                break
            if evicted_room_id is None:
                return
            self._room_locks.pop(evicted_room_id, None)
