"""SQLite cache for Matrix thread events and individual event lookups."""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING, Any

import aiosqlite

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path


class EventCache:
    """Persist raw Matrix events for thread-history and reply-chain reconstruction."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Open the SQLite database and create the cache schema."""
        async with self._lock:
            if self._db is not None:
                return

            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            self._db = await aiosqlite.connect(self._db_path)
            await self._db.execute("PRAGMA journal_mode=WAL")
            await self._db.execute("PRAGMA busy_timeout=5000")
            await self._db.execute(
                """
                CREATE TABLE IF NOT EXISTS thread_events (
                    room_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    origin_server_ts INTEGER NOT NULL,
                    event_json TEXT NOT NULL,
                    PRIMARY KEY (room_id, event_id)
                )
                """,
            )
            await self._db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_thread_events_room_thread_ts
                ON thread_events(room_id, thread_id, origin_server_ts)
                """,
            )
            await self._db.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    room_id TEXT NOT NULL,
                    event_json TEXT NOT NULL,
                    cached_at REAL NOT NULL
                )
                """,
            )
            await self._db.commit()

    async def close(self) -> None:
        """Close the SQLite connection when the cache is no longer needed."""
        async with self._lock:
            if self._db is None:
                return
            await self._db.close()
            self._db = None

    async def get_thread_events(self, room_id: str, thread_id: str) -> list[dict[str, Any]] | None:
        """Return cached events for one thread sorted by timestamp."""
        async with self._lock:
            db = self._require_db()
            cursor = await db.execute(
                """
                SELECT event_json
                FROM thread_events
                WHERE room_id = ? AND thread_id = ?
                ORDER BY origin_server_ts ASC, event_id ASC
                """,
                (room_id, thread_id),
            )
            rows = await cursor.fetchall()
            await cursor.close()
            if not rows:
                return None
            return [json.loads(row[0]) for row in rows]

    async def get_latest_ts(self, room_id: str, thread_id: str) -> int | None:
        """Return the latest cached server timestamp for one thread."""
        async with self._lock:
            db = self._require_db()
            cursor = await db.execute(
                """
                SELECT MAX(origin_server_ts)
                FROM thread_events
                WHERE room_id = ? AND thread_id = ?
                """,
                (room_id, thread_id),
            )
            row = await cursor.fetchone()
            await cursor.close()
            return None if row is None or row[0] is None else int(row[0])

    async def get_event(self, event_id: str) -> dict[str, Any] | None:
        """Return one cached event payload by event ID."""
        async with self._lock:
            db = self._require_db()
            cursor = await db.execute(
                """
                SELECT event_json
                FROM events
                WHERE event_id = ?
                """,
                (event_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
            return None if row is None else json.loads(row[0])

    async def store_event(self, event_id: str, room_id: str, event_data: dict[str, Any]) -> None:
        """Insert or replace one individually cached Matrix event."""
        await self.store_events_batch([(event_id, room_id, event_data)])

    async def store_events_batch(self, events: list[tuple[str, str, dict[str, Any]]]) -> None:
        """Insert or replace one batch of individually cached Matrix events."""
        if not events:
            return

        cached_at = time.time()
        async with self._lock:
            db = self._require_db()
            await db.executemany(
                """
                INSERT OR REPLACE INTO events(event_id, room_id, event_json, cached_at)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (
                        event_id,
                        room_id,
                        json.dumps(
                            normalize_event_source_for_cache(event_data, event_id=event_id),
                            separators=(",", ":"),
                        ),
                        cached_at,
                    )
                    for event_id, room_id, event_data in events
                ],
            )
            await db.commit()

    async def store_events(self, room_id: str, thread_id: str, events: list[dict[str, Any]]) -> None:
        """Insert or replace one batch of thread events."""
        if not events:
            return

        cached_at = time.time()
        async with self._lock:
            db = self._require_db()
            serialized_events = [
                (
                    _event_id(event),
                    _event_timestamp(event),
                    json.dumps(event, separators=(",", ":")),
                )
                for event in events
            ]
            await db.executemany(
                """
                INSERT OR REPLACE INTO thread_events(room_id, thread_id, event_id, origin_server_ts, event_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        room_id,
                        thread_id,
                        event_id,
                        origin_server_ts,
                        event_json,
                    )
                    for event_id, origin_server_ts, event_json in serialized_events
                ],
            )
            await db.executemany(
                """
                INSERT OR REPLACE INTO events(event_id, room_id, event_json, cached_at)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (
                        event_id,
                        room_id,
                        event_json,
                        cached_at,
                    )
                    for event_id, _origin_server_ts, event_json in serialized_events
                ],
            )
            await db.commit()

    async def invalidate_thread(self, room_id: str, thread_id: str) -> None:
        """Delete cached events for one thread."""
        async with self._lock:
            db = self._require_db()
            cursor = await db.execute(
                """
                SELECT event_id
                FROM thread_events
                WHERE room_id = ? AND thread_id = ?
                """,
                (room_id, thread_id),
            )
            event_ids = [str(row[0]) for row in await cursor.fetchall()]
            await cursor.close()
            await db.execute(
                """
                DELETE FROM thread_events
                WHERE room_id = ? AND thread_id = ?
                """,
                (room_id, thread_id),
            )
            if event_ids:
                await db.executemany(
                    """
                    DELETE FROM events
                    WHERE event_id = ?
                    """,
                    [(event_id,) for event_id in event_ids],
                )
            await db.commit()

    async def get_latest_timestamp(self, room_id: str, thread_id: str) -> int | None:
        """Compatibility wrapper for older cache call sites."""
        return await self.get_latest_ts(room_id, thread_id)

    async def store_thread_events(self, room_id: str, thread_id: str, events: list[dict[str, Any]]) -> None:
        """Compatibility wrapper for older cache call sites."""
        await self.store_events(room_id, thread_id, events)

    async def append_event(self, room_id: str, thread_id: str, event: dict[str, Any]) -> bool:
        """Append one event when the thread already has cached data."""
        if await self.get_latest_ts(room_id, thread_id) is None:
            return False
        await self.store_events(room_id, thread_id, [event])
        return True

    async def get_thread_id_for_event(self, room_id: str, event_id: str) -> str | None:
        """Return the cached thread ID for one event."""
        async with self._lock:
            db = self._require_db()
            cursor = await db.execute(
                """
                SELECT thread_id
                FROM thread_events
                WHERE room_id = ? AND event_id = ?
                """,
                (room_id, event_id),
            )
            row = await cursor.fetchone()
            await cursor.close()
            return None if row is None else str(row[0])

    async def redact_event(
        self,
        room_id: str,
        event_id: str,
        *,
        thread_id: str | None = None,
        redaction_event: dict[str, Any] | None = None,
    ) -> bool:
        """Delete one cached event after a redaction."""
        del redaction_event

        cached_thread_id = thread_id or await self.get_thread_id_for_event(room_id, event_id)

        async with self._lock:
            db = self._require_db()
            deleted_thread_rows = 0
            if cached_thread_id is not None:
                cursor = await db.execute(
                    """
                    DELETE FROM thread_events
                    WHERE room_id = ? AND thread_id = ? AND event_id = ?
                    """,
                    (room_id, cached_thread_id, event_id),
                )
                deleted_thread_rows = 0 if cursor.rowcount is None else int(cursor.rowcount)
                await cursor.close()
            cursor = await db.execute(
                """
                DELETE FROM events
                WHERE event_id = ?
                """,
                (event_id,),
            )
            deleted_event_rows = 0 if cursor.rowcount is None else int(cursor.rowcount)
            await cursor.close()
            await db.commit()
        return deleted_thread_rows > 0 or deleted_event_rows > 0

    def _require_db(self) -> aiosqlite.Connection:
        if self._db is None:
            msg = "EventCache has not been initialized"
            raise RuntimeError(msg)
        return self._db


def _event_id(event: dict[str, Any]) -> str:
    event_id = event.get("event_id")
    if isinstance(event_id, str) and event_id:
        return event_id
    msg = "Cached Matrix event is missing event_id"
    raise ValueError(msg)


def _event_timestamp(event: dict[str, Any]) -> int:
    timestamp = event.get("origin_server_ts")
    if isinstance(timestamp, int) and not isinstance(timestamp, bool):
        return timestamp
    msg = f"Cached Matrix event {_event_id(event)} is missing origin_server_ts"
    raise ValueError(msg)


def normalize_event_source_for_cache(
    event_source: Mapping[str, Any],
    *,
    event_id: str | None = None,
    sender: str | None = None,
    origin_server_ts: int | None = None,
) -> dict[str, Any]:
    """Normalize one raw Matrix event payload for persistent cache storage."""
    source = dict(event_source)
    if "event_id" not in source and isinstance(event_id, str):
        source["event_id"] = event_id
    if "sender" not in source and isinstance(sender, str):
        source["sender"] = sender
    if (
        "origin_server_ts" not in source
        and isinstance(origin_server_ts, int)
        and not isinstance(origin_server_ts, bool)
    ):
        source["origin_server_ts"] = origin_server_ts
    return source
