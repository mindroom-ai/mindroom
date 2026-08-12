"""Read a candidate database's journal identity without changing the database.

Asking a database who it is by opening the store answers the question and
destroys the evidence in the same breath: both backends create or migrate the
whole schema on open, so a database that is then *refused* has already been
written to. An identity-only database points at nothing and still comes back
carrying every table this install uses.

So the question is asked before the store exists. These probes connect, read
``journal_identity`` if it is there, and leave the database exactly as they
found it -- including the case where there is no database at all, which is
precisely the "has never been used" answer the binding check needs.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

_IDENTITY_TABLE = "journal_identity"
_GENERATION_QUERY = f"SELECT generation FROM {_IDENTITY_TABLE}"  # noqa: S608 - a module constant, not input


def probe_sqlite_generation(database_path: Path) -> str | None:
    """Return an existing SQLite journal's generation, creating nothing.

    ``None`` covers all three ways a database can have no identity to report:
    the file is absent, the file exists but holds no schema, or the schema is
    there and the singleton row is not. They are the same fact to the caller --
    nothing has ever used this database -- and telling them apart would only
    invite a branch that creates one of them.
    """
    if not database_path.exists():
        return None
    # Read-only, so that a probe cannot be the thing that creates the file it
    # is asking about, and cannot leave a schema behind in a database it is
    # about to refuse.
    connection = sqlite3.connect(f"{database_path.as_uri()}?mode=ro", uri=True)
    try:
        located = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
            (_IDENTITY_TABLE,),
        ).fetchone()
        if located is None:
            return None
        row = connection.execute(_GENERATION_QUERY).fetchone()
    finally:
        connection.close()
    return None if row is None else str(row[0])


def probe_postgres_generation(database_url: str) -> str | None:
    """Return an existing PostgreSQL journal's generation, creating nothing."""
    import psycopg  # noqa: PLC0415 - psycopg ships with the optional postgres extra

    with psycopg.connect(database_url, autocommit=True) as connection, connection.cursor() as cursor:
        # Resolved through the connection's own search path, so a DSN pinned to
        # one schema is asked about that schema and not about a namesake table
        # somewhere else in the database.
        cursor.execute("SELECT to_regclass(%s)", (_IDENTITY_TABLE,))
        located = cursor.fetchone()
        if located is None or located[0] is None:
            return None
        cursor.execute(_GENERATION_QUERY)
        row = cursor.fetchone()
    return None if row is None else str(row[0])
