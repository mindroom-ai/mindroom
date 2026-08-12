"""How one agent's state database behaves while another statement holds it."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

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


def test_a_state_database_keeps_rollback_journal_and_waits_for_locks(tmp_path: Path) -> None:
    """Network-compatible rollback journaling retains a long lock timeout."""
    storage = _storage(tmp_path)

    with storage.db_engine.connect() as connection:
        assert connection.execute(text("PRAGMA journal_mode")).scalar() == "delete"
        assert connection.execute(text("PRAGMA busy_timeout")).scalar() == 30_000
    storage.close()


def test_a_read_waits_for_a_writer_rather_than_failing(tmp_path: Path) -> None:
    """A statement that finds the database locked waits for it, up to the timeout."""
    storage = _storage(tmp_path)
    with storage.db_engine.connect() as setup:
        setup.execute(text("CREATE TABLE probe (value TEXT)"))
        setup.commit()

    holding = threading.Event()
    release = threading.Event()
    reading = threading.Event()
    finished = threading.Event()
    result: list[int] = []
    errors: list[Exception] = []

    def hold_write_lock() -> None:
        with storage.db_engine.connect() as writer:
            writer.execute(text("BEGIN EXCLUSIVE"))
            writer.execute(text("INSERT INTO probe (value) VALUES ('held')"))
            holding.set()
            release.wait(timeout=5)
            writer.execute(text("COMMIT"))

    def read_after_lock() -> None:
        try:
            reading.set()
            with storage.db_engine.connect() as reader:
                result.append(reader.execute(text("SELECT count(*) FROM probe")).scalar_one())
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
    storage.close()
