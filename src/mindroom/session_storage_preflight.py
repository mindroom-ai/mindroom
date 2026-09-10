"""Preflight and retained archives for owned, reconstructable session databases."""

from __future__ import annotations

import sqlite3
import stat
from contextlib import closing, contextmanager
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from agno.db.sqlite.schemas import get_table_schema_definition

from mindroom.durable_write import fsync_directory_durable
from mindroom.file_locks import advisory_file_lock
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

logger = get_logger(__name__)

_REQUIRED_SESSION_COLUMNS = frozenset(
    name for name in get_table_schema_definition("sessions") if not name.startswith("_")
)


# Legacy format: Owned Agno session databases may lack columns required by the installed schema.
# Last legacy release: schema-based boundary with no single release cutoff or safe row conversion.
# Handling: Archive the reconstructable sessions directory before creating a current store; preserve its bytes.
# Coverage: tests/test_agent_session_storage_recovery.py::test_incompatible_session_schema_is_archived_before_real_session_use.
@contextmanager
def session_storage_preflight(
    state_root: Path,
    *,
    storage_name: str,
    session_table: str,
    timeout_seconds: float,
) -> Iterator[None]:
    """Inspect and, if necessary, archive one owned sessions directory before use."""
    db_dir = state_root / "sessions"
    db_file = db_dir / f"{storage_name}.db"
    if not storage_name or db_file.parent != db_dir:
        msg = "Session storage name must identify a file directly inside its sessions directory"
        raise ValueError(msg)

    with advisory_file_lock(state_root / ".sessions-recovery.lock"):
        if _existing_session_database(db_dir, db_file):
            try:
                columns = _session_columns(db_file, session_table, timeout_seconds=timeout_seconds, mode="ro")
            except sqlite3.OperationalError as error:
                if error.sqlite_errorcode != sqlite3.SQLITE_READONLY_ROLLBACK:
                    raise
                # A hot rollback journal needs SQLite's native writable recovery
                # before even a metadata query. The failed probe is already closed.
                columns = _session_columns(db_file, session_table, timeout_seconds=timeout_seconds, mode="rw")
            if columns is not None and (missing := _REQUIRED_SESSION_COLUMNS - columns):
                _archive_sessions(db_dir, missing)
        yield


def _existing_session_database(db_dir: Path, db_file: Path) -> bool:
    try:
        directory_mode = db_dir.lstat().st_mode
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(directory_mode):
        msg = "Cannot recover a session directory that is not an owned real directory"
        raise ValueError(msg)
    try:
        file_mode = db_file.lstat().st_mode
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(file_mode):
        msg = "Cannot recover a session database that is not an owned regular file"
        raise ValueError(msg)
    return True


def _session_columns(
    db_file: Path,
    session_table: str,
    *,
    timeout_seconds: float,
    mode: Literal["ro", "rw"],
) -> frozenset[str] | None:
    uri = f"{db_file.absolute().as_uri()}?mode={mode}"
    with closing(sqlite3.connect(uri, uri=True, timeout=timeout_seconds)) as connection:
        schema_entry = connection.execute("SELECT type FROM sqlite_schema WHERE name = ?", (session_table,)).fetchone()
        if schema_entry is None:
            return None
        if schema_entry[0] != "table":
            msg = "Expected session table name belongs to a different SQLite schema object"
            raise ValueError(msg)
        return frozenset(
            row[0] for row in connection.execute("SELECT name FROM pragma_table_info(?)", (session_table,))
        )


def _archive_sessions(db_dir: Path, missing_columns: frozenset[str]) -> None:
    archive = db_dir.with_name(f"sessions.incompatible-{uuid4().hex}")
    try:
        archive.lstat()
    except FileNotFoundError:
        pass
    else:
        msg = f"Session archive already exists: {archive}"
        raise FileExistsError(msg)

    mode = stat.S_IMODE(db_dir.stat().st_mode)
    db_dir.rename(archive)
    fsync_directory_durable(db_dir.parent)
    db_dir.mkdir(mode=mode)
    db_dir.chmod(mode)
    fsync_directory_durable(db_dir)
    fsync_directory_durable(db_dir.parent)
    logger.warning(
        "session_schema_incompatible_archived",
        archive=str(archive),
        missing_columns=sorted(missing_columns),
    )
