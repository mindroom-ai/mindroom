"""Shared SQLite WAL-mode entry and explicit transaction helpers."""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

_WAL_RETRY_SECONDS = 0.05


def enter_wal(
    connection: sqlite3.Connection,
    *,
    timeout_seconds: float = 10.0,
    retry_seconds: float = _WAL_RETRY_SECONDS,
) -> None:
    """Switch *connection* to WAL journal mode, retrying only ``SQLITE_BUSY`` up to *timeout_seconds*.

    Entering WAL is a journal-mode change that SQLite refuses immediately with ``SQLITE_BUSY`` while
    another opener holds the database rather than invoking ``busy_timeout``.
    """
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            connection.execute("PRAGMA journal_mode = WAL")
        except sqlite3.OperationalError as error:
            if error.sqlite_errorcode != sqlite3.SQLITE_BUSY or time.monotonic() >= deadline:
                raise
            time.sleep(retry_seconds)
        else:
            return


@contextmanager
def begun(connection: sqlite3.Connection, *, write: bool) -> Iterator[None]:
    """Run the block as one transaction on *connection*, committing on normal exit.

    A write takes the write lock first (``BEGIN IMMEDIATE``). A block that fails or is interrupted rolls
    back immediately rather than leaving that to close.
    """
    connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    connection.commit()
