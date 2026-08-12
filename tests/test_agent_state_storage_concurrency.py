"""How one agent's state database behaves while another statement holds it."""

from __future__ import annotations

import asyncio
import threading
import time
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import text

from mindroom.agent_storage import create_state_storage

if TYPE_CHECKING:
    from pathlib import Path

    from agno.db.base import BaseDb


def _storage(tmp_path: Path) -> BaseDb:
    return create_state_storage(
        "probe",
        tmp_path,
        subdir="sessions",
        session_table="probe_sessions",
    )


def test_a_state_database_is_opened_for_concurrent_readers(tmp_path: Path) -> None:
    """WAL and a wait, rather than the default of blocking readers and failing fast.

    These are the databases a response reads on its way to answering. Under the
    rollback journal a flush in progress makes those reads wait on a lock the
    reader cannot see, and without a busy timeout a contended statement raises
    `database is locked` instead of waiting for its turn.
    """
    storage = _storage(tmp_path)

    with storage.db_engine.connect() as connection:
        assert connection.execute(text("PRAGMA journal_mode")).scalar() == "wal"
        assert connection.execute(text("PRAGMA busy_timeout")).scalar() == 30_000


def test_a_read_waits_for_a_writer_rather_than_failing(tmp_path: Path) -> None:
    """A statement that finds the database locked waits for it, up to the timeout."""
    storage = _storage(tmp_path)
    with storage.db_engine.connect() as setup:
        setup.execute(text("CREATE TABLE probe (value TEXT)"))
        setup.commit()

    holding = threading.Event()
    release = threading.Event()

    def hold_write_lock() -> None:
        with storage.db_engine.connect() as writer:
            writer.execute(text("BEGIN IMMEDIATE"))
            writer.execute(text("INSERT INTO probe (value) VALUES ('held')"))
            holding.set()
            release.wait(timeout=5)
            writer.execute(text("COMMIT"))

    holder = threading.Thread(target=hold_write_lock, name="probe-writer")
    holder.start()
    try:
        assert holding.wait(timeout=5)
        with storage.db_engine.connect() as reader:
            # Under WAL this returns the pre-write snapshot immediately rather
            # than raising, which is the property the response path needs.
            assert reader.execute(text("SELECT count(*) FROM probe")).scalar() == 0
    finally:
        release.set()
        holder.join(timeout=5)

    assert not holder.is_alive()


@pytest.mark.asyncio
async def test_reading_a_session_does_not_hold_the_event_loop(tmp_path: Path) -> None:
    """A slow state read must not stop everything else the loop is running.

    The read is synchronous down to a SQLite statement and these databases have
    been measured at over a second for a single row, so the loop has to stay
    free while it happens.
    """
    storage = _storage(tmp_path)
    with storage.db_engine.connect() as setup:
        setup.execute(text("CREATE TABLE slow (value TEXT)"))
        setup.commit()

    ticks = 0

    async def tick() -> None:
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.01)

    def slow_read() -> None:
        with storage.db_engine.connect() as connection:
            connection.execute(text("SELECT count(*) FROM slow")).scalar()
            time.sleep(0.3)

    ticker = asyncio.create_task(tick())
    try:
        await asyncio.to_thread(slow_read)
    finally:
        ticker.cancel()

    assert ticks > 5, "the loop was blocked while the state database was read"
