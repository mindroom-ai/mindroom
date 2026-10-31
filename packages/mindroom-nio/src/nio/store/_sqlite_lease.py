"""Opening-time coordination for ordinary and durable SQLite stores."""

from __future__ import annotations

import os
import sqlite3
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

from peewee import SqliteDatabase

from ..exceptions import LocalProtocolError


class FileLease:
    """Lifetime sidecar lock; live database path replacement is unsupported."""

    def __init__(self, database_path: Path, *, exclusive: bool = True) -> None:
        try:
            import fcntl
        except ImportError as error:
            raise LocalProtocolError(
                "store filesystem ownership requires fcntl support"
            ) from error
        self.pid = os.getpid()
        self.fd = os.open(
            f"{database_path.resolve()}.ingest.lock", os.O_CREAT | os.O_RDWR, 0o600
        )
        try:
            mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            fcntl.flock(self.fd, mode | fcntl.LOCK_NB)
        except BaseException as error:
            self.close()
            if isinstance(error, BlockingIOError):
                raise LocalProtocolError(
                    "store lifetime lease is already held"
                ) from error
            raise

    def assert_open(self) -> None:
        if self.fd < 0 or self.pid != os.getpid():
            raise LocalProtocolError(
                "store lease is closed or belongs to another process"
            )

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


def check_ordinary_database(connection: sqlite3.Connection) -> None:
    marker = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name COLLATE NOCASE IN ('NioIngestMeta', 'NioDurableMeta')"
    ).fetchone()
    if marker is not None:
        if marker[0].lower() == "niodurablemeta":
            raise LocalProtocolError("this database requires a durable sync session")
        raise LocalProtocolError(
            "unmerged ingestion format requires explicit replacement"
        )


# A failed final close (for example GC on another thread) must retain exclusion
# until process exit. Supported explicit closes normally release immediately.
_DEFERRED_LEASES: list[FileLease] = []


class _LeasedConnection(sqlite3.Connection):
    def __init__(self, *args: Any, lease: FileLease, **kwargs: Any) -> None:
        self._lease: FileLease | None = lease
        super().__init__(*args, **kwargs)

    def close(self) -> None:
        super().close()
        if self._lease is not None:
            self._lease.close()
            self._lease = None

    def __del__(self) -> None:
        try:
            self.close()
        except sqlite3.Error:
            lease = getattr(self, "_lease", None)
            if lease is not None:
                _DEFERRED_LEASES.append(lease)
                self._lease = None


class LeasedSqliteDatabase(SqliteDatabase):
    """One shared lease per physical connection, checked again on reconnect."""

    if TYPE_CHECKING:
        _timeout: float

        def _add_conn_hooks(self, connection: sqlite3.Connection) -> None: ...

    def _connect(self) -> sqlite3.Connection:
        lease = FileLease(Path(self.database), exclusive=False)
        try:
            connection = sqlite3.connect(
                self.database,
                timeout=self._timeout,
                isolation_level=None,
                **{
                    **self.connect_params,
                    "factory": partial(_LeasedConnection, lease=lease),
                },
            )
        except BaseException:
            lease.close()
            raise
        try:
            # Check before hooks: even PRAGMA writes must not touch an adopted DB.
            check_ordinary_database(connection)
            self._add_conn_hooks(connection)
        except BaseException:
            connection.close()
            raise
        return connection
