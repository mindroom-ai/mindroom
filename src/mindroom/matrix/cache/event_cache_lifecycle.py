"""Lifecycle and schema helpers for the SQLite-backed Matrix event cache."""

from __future__ import annotations

from typing import TYPE_CHECKING

import aiosqlite

from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from pathlib import Path


EVENT_CACHE_SCHEMA_VERSION = 7
_EVENT_CACHE_TABLES = (
    "thread_events",
    "events",
    "event_edits",
    "event_threads",
    "redacted_events",
    "thread_cache_state",
    "room_cache_state",
)
_EVENT_CACHE_RESET_TABLES = _EVENT_CACHE_TABLES
_REQUIRED_EVENT_CACHE_TABLES = frozenset(_EVENT_CACHE_TABLES)
logger = get_logger(__name__)


async def initialize_event_cache_db(db_path: Path) -> aiosqlite.Connection:
    """Open the SQLite database and ensure the event-cache schema exists."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(db_path)
    try:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA busy_timeout=5000")
        await reset_stale_cache_if_needed(db, db_path=db_path)
        await create_event_cache_schema(db)
        await db.commit()
    except Exception:
        await db.close()
        raise
    return db


async def create_event_cache_schema(db: aiosqlite.Connection) -> None:
    """Create the current cache schema in one SQLite connection."""
    await db.execute(
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
    await db.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_thread_events_room_thread_ts
        ON thread_events(room_id, thread_id, origin_server_ts)
        """,
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            event_id TEXT PRIMARY KEY,
            room_id TEXT NOT NULL,
            event_json TEXT NOT NULL,
            cached_at REAL NOT NULL
        )
        """,
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS event_edits (
            edit_event_id TEXT PRIMARY KEY,
            room_id TEXT NOT NULL,
            original_event_id TEXT NOT NULL,
            origin_server_ts INTEGER NOT NULL
        )
        """,
    )
    await db.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_event_edits_room_original_ts
        ON event_edits(room_id, original_event_id, origin_server_ts DESC, edit_event_id DESC)
        """,
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS event_threads (
            room_id TEXT NOT NULL,
            event_id TEXT NOT NULL,
            thread_id TEXT NOT NULL,
            PRIMARY KEY (room_id, event_id)
        )
        """,
    )
    await db.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_event_threads_room_thread
        ON event_threads(room_id, thread_id, event_id)
        """,
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS redacted_events (
            room_id TEXT NOT NULL,
            event_id TEXT NOT NULL,
            PRIMARY KEY (room_id, event_id)
        )
        """,
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS thread_cache_state (
            room_id TEXT NOT NULL,
            thread_id TEXT NOT NULL,
            validated_at REAL,
            invalidated_at REAL,
            invalidation_reason TEXT,
            PRIMARY KEY (room_id, thread_id)
        )
        """,
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS room_cache_state (
            room_id TEXT PRIMARY KEY,
            invalidated_at REAL,
            invalidation_reason TEXT
        )
        """,
    )
    await db.execute(f"PRAGMA user_version = {EVENT_CACHE_SCHEMA_VERSION}")


async def schema_version(db: aiosqlite.Connection) -> int:
    """Return the current SQLite schema version for this cache."""
    cursor = await db.execute("PRAGMA user_version")
    row = await cursor.fetchone()
    await cursor.close()
    return 0 if row is None else int(row[0])


async def existing_table_names(db: aiosqlite.Connection) -> set[str]:
    """Return the user-defined tables that currently exist in this SQLite DB."""
    cursor = await db.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
        """,
    )
    rows = await cursor.fetchall()
    await cursor.close()
    return {str(row[0]) for row in rows}


async def reset_stale_cache_if_needed(
    db: aiosqlite.Connection,
    *,
    db_path: Path,
) -> None:
    """Drop stale cache contents instead of migrating old cache schemas forward."""
    current_schema_version = await schema_version(db)
    current_table_names = await existing_table_names(db)
    if not current_table_names:
        return
    if current_schema_version == EVENT_CACHE_SCHEMA_VERSION and _REQUIRED_EVENT_CACHE_TABLES.issubset(
        current_table_names,
    ):
        return

    # This cache is advisory state. We intentionally do not support migration
    # of old cache schemas; stale cache contents are discarded and rebuilt
    # lazily through normal usage so the durable schema stays simple.
    logger.info(
        "Resetting stale Matrix event cache instead of migrating it",
        db_path=str(db_path),
        schema_version=current_schema_version,
        existing_tables=sorted(current_table_names),
    )
    await db.executescript(
        "\n".join(f"DROP TABLE IF EXISTS {table_name};" for table_name in _EVENT_CACHE_RESET_TABLES),
    )
    await db.execute("PRAGMA user_version = 0")
    await db.commit()
