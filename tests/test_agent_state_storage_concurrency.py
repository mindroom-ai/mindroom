"""How one agent's state database behaves while another statement holds it."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import closing
from typing import TYPE_CHECKING

from mindroom.session_storage.agent_storage import create_state_storage

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.session_storage.store import ConversationStore


def _storage(tmp_path: Path) -> ConversationStore:
    return create_state_storage("probe", tmp_path)


def test_a_state_database_keeps_rollback_journal_and_waits_for_locks(tmp_path: Path) -> None:
    """Network-compatible rollback journaling retains a long lock timeout."""
    storage = _storage(tmp_path)

    with storage.transaction(write=False) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        assert connection.execute("PRAGMA busy_timeout").fetchone() == (30_000,)


def test_a_read_waits_for_a_writer_rather_than_failing(tmp_path: Path) -> None:
    """A statement that finds the database locked waits for it, up to the timeout."""
    storage = _storage(tmp_path)
    with closing(sqlite3.connect(storage.path)) as setup, setup:
        setup.execute("CREATE TABLE probe (value TEXT)")

    holding = threading.Event()
    release = threading.Event()
    reading = threading.Event()
    finished = threading.Event()
    result: list[int] = []
    errors: list[Exception] = []

    def hold_write_lock() -> None:
        with closing(sqlite3.connect(storage.path, isolation_level=None)) as writer:
            writer.execute("BEGIN EXCLUSIVE")
            writer.execute("INSERT INTO probe (value) VALUES ('held')")
            holding.set()
            release.wait(timeout=5)
            writer.execute("COMMIT")

    def read_after_lock() -> None:
        try:
            reading.set()
            with storage.transaction(write=False) as reader:
                (count,) = reader.execute("SELECT count(*) FROM probe").fetchone()
                result.append(count)
        except Exception as error:  # pragma: no cover - asserted below
            errors.append(error)
        finally:
            finished.set()

    holder = threading.Thread(target=hold_write_lock, name="probe-writer")
    reader = threading.Thread(target=read_after_lock, name="probe-reader")
    holder.start()
    try:
        assert holding.wait(timeout=5)
        reader.start()
        assert reading.wait(timeout=5)
        assert not finished.wait(timeout=0.1), "reader did not wait for the exclusive writer"
    finally:
        release.set()
        holder.join(timeout=5)
        reader.join(timeout=5)

    assert not holder.is_alive()
    assert not reader.is_alive()
    assert errors == []
    assert result == [1]
