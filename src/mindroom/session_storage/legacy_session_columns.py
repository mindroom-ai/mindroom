"""Sessions tables of the layouts before version 4, converted once: each row named its owner and held its state.

Before layout version 4, every session row named its kind and owner (Agno's ``session_type``, ``agent_id``, and
``team_id``) and kept ``session_state`` inside the ``session_data`` object beside the session's usage totals. Version 4
records the database's owner once, in its meta row, and gives the state a column of its own. The conversion of an
Agno-layout database (``legacy_agno3_conversion``) ends here too, and so does a database at version 2, whose agent rows
first take their agent's name (``legacy_agent_session_owners``).
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

from mindroom.history.types import HistoryScope
from mindroom.logging_config import get_logger
from mindroom.session_storage.legacy_agent_session_owners import adopt_agent_session_owners
from mindroom.session_storage.legacy_conversion_backup import leave_write_ahead_log, write_conversion_backup
from mindroom.session_storage.schema import (
    LOG_VERSION,
    SESSION_COLUMNS,
    TableNames,
    index_name,
    meta_version,
    quote_identifier,
    table_names,
)
from mindroom.session_storage.store import begun, decoded_mapping

if TYPE_CHECKING:
    import sqlite3
    from contextlib import ExitStack
    from pathlib import Path

logger = get_logger(__name__)

# The version whose agent rows name their agent (``legacy_agent_session_owners``).
_AGENT_OWNERS_VERSION = 3
# The suffix of the backup a database below layout version 4 gets before its conversion.
BACKUP_SUFFIX = ".pre-storage-v4"


# LEGACY_COMPAT: Conversation databases below layout version 4, whose meta row names no owner.
# Legacy format: A database in the current layout whose `*_log_meta` row records version 2 or 3 and has no
# `owner_kind` and `owner_id` columns; its session rows name their owner instead.
# Last legacy release: v2026.10.676; replacement: the next release, layout version 4, whose meta row records the
# agent, team scope, or system usage ledger that owns the database.
# Handling: Every store open and the startup sweep, which know the owner, convert such a database once in one write
# transaction, after its backup to `<name>.db.pre-storage-v4`: at version 2 an agent's rows first take its name, then
# the sessions table converts (`convert_session_columns`) and the meta row records the owner and version 4. The sweep
# takes a team database's owner from the team its rows name whose database has this one's name (`team_owner`), and
# leaves one no row names to its scope's next store open. Two openers converting at once are serialized by the write
# lock, and the second finds the database converted.
# Coverage: tests/session_storage/test_legacy_session_columns.py.
def upgrade_layout(
    connection: sqlite3.Connection,
    path: Path,
    names: TableNames,
    descriptors: ExitStack,
    owner: HistoryScope,
) -> None:
    """Bring the database at ``path``, in the current layout at an older version, to ``LOG_VERSION`` as ``owner``'s.

    In one ``BEGIN IMMEDIATE``, after the byte-for-byte backup beside it (``write_conversion_backup``), so a crash or an
    error leaves the file as it was and the next open converts it again. A file in write-ahead-log mode returns to the
    rollback journal first, and the conversion is logged once it commits. ``descriptors`` takes the backup's descriptor
    of the file, which the caller closes after ``connection``.
    """
    leave_write_ahead_log(connection, path)
    started = time.monotonic()
    with begun(connection, write=True):
        # Asked again under the lock: another opener may have converted the file while this one waited, and backing up
        # the converted file would replace the original's backup.
        version = meta_version(connection, names)
        if version >= LOG_VERSION:
            return
        # The copy takes the file's size, and the conversion's rollback journal at most as much again.
        write_conversion_backup(path, descriptors, suffix=BACKUP_SUFFIX, size_factor=2, copied_bytes=0)
        if version < _AGENT_OWNERS_VERSION and owner.kind == "agent":
            adopt_agent_session_owners(connection, names, owner.scope_id)
        convert_session_columns(connection, names)
        meta = quote_identifier(names.log_meta)
        for column in ("owner_kind", "owner_id"):
            connection.execute(f"ALTER TABLE {meta} ADD COLUMN {column} TEXT")
        connection.execute(
            f"UPDATE {meta} SET version = ?, owner_kind = ?, owner_id = ? WHERE id = 1",  # noqa: S608
            (LOG_VERSION, owner.kind, owner.scope_id),
        )
        runs, messages = stored_counts(connection, names)
    # The dropped columns free too little of the file to be worth compacting.
    logger.info(
        "conversation_converted",
        path=str(path),
        runs=runs,
        messages=messages,
        compacted=False,
        duration_seconds=round(time.monotonic() - started, 3),
    )


def stored_counts(connection: sqlite3.Connection, names: TableNames) -> tuple[int, int]:
    """Return how many runs and stored messages the database holds, which its conversion's record reports."""
    return connection.execute(
        f"SELECT (SELECT count(*) FROM {quote_identifier(names.log_runs)}), "  # noqa: S608
        f"(SELECT count(*) FROM {quote_identifier(names.log_messages)})",
    ).fetchone()


# LEGACY_COMPAT: Session rows that name their kind and owner and keep their state inside `session_data`.
# Legacy format: The sessions table's `session_type`, `agent_id`, and `team_id` columns and its `session_type` index,
# and `session_state` as a key of each row's `session_data` object, beside the session's usage totals.
# Last legacy release: v2026.10.676; replacement: the next release, whose sessions table holds `session_state` in a
# column of its own and leaves the owner to the meta row.
# Handling: In the caller's write transaction, each row's state moves to the `session_state` column, and the rest of a
# readable `session_data` object stays, or NULL when nothing is left; a row whose `session_data` is not an object keeps
# its bytes and gets no state, as a load read it. The three columns and the index are then dropped, so a session's
# owner is its database's and a load no longer returns nothing for a row of another kind, such as the agent row a team
# bot's interrupted first reply created. An older release started on the converted file finds its required columns
# missing and archives the sessions directory. The schema preflight does not require `session_state` of a database
# that still has to convert.
# Coverage: tests/session_storage/test_legacy_session_columns.py.
def convert_session_columns(connection: sqlite3.Connection, names: TableNames) -> None:
    """Give each session row its state in a column of its own and drop every sessions column version 4 does not keep."""
    sessions = quote_identifier(names.sessions)
    connection.execute(f"ALTER TABLE {sessions} ADD COLUMN session_state JSON")
    rows = connection.execute(f"SELECT session_id, session_data FROM {sessions}").fetchall()  # noqa: S608
    for session_id, raw_session_data in rows:
        session_data = decoded_mapping(raw_session_data)
        if session_data is None or "session_state" not in session_data:
            continue
        state = session_data.pop("session_state")
        connection.execute(
            f"UPDATE {sessions} SET session_state = ?, session_data = ? WHERE session_id = ?",  # noqa: S608
            (json.dumps(state), json.dumps(session_data) if session_data else None, session_id),
        )
    _drop_unkept_session_columns(connection, names)


# LEGACY_COMPAT: Sessions-table columns only Agno wrote.
# Legacy format: `workflow_id`, `agent_data`, `team_data`, `workflow_data`, and `summary` on every Agno sessions table,
# the unused `created_at` index, and, on files Agno 2 created, its cleared `runs` blob column.
# Last legacy release: v2026.10.608 for the five columns and the index; replacement: the next release, the columns
# storage v2 keeps. The `runs` column: v2026.9.11 (agno 2.6.12); replacement: v2026.9.12 (agno 3.0.5) created
# sessions tables without it.
# Handling: Every column outside `SESSION_COLUMNS` is dropped, and the index, so an older release started on the
# converted file finds its required columns missing and archives the sessions directory instead of writing into it.
# Coverage: tests/session_storage/test_legacy_agno3_conversion.py::test_a_converted_database_holds_only_the_v2_layout.
def _drop_unkept_session_columns(connection: sqlite3.Connection, names: TableNames) -> None:
    sessions = quote_identifier(names.sessions)
    for column in ("created_at", "session_type"):
        connection.execute(f"DROP INDEX IF EXISTS {quote_identifier(index_name(names.sessions, column))}")
    for column in _columns(connection, names):
        if column not in SESSION_COLUMNS:
            connection.execute(f"ALTER TABLE {sessions} DROP COLUMN {quote_identifier(column)}")


def _columns(connection: sqlite3.Connection, names: TableNames) -> list[str]:
    return [row[0] for row in connection.execute("SELECT name FROM pragma_table_info(?)", (names.sessions,))]


def team_owner(connection: sqlite3.Connection, names: TableNames) -> HistoryScope | None:
    """Return the team scope a team's database below version 4 belongs to, for the startup sweep, which lacks it.

    It is the scope one of its session rows names, a team row by its team or a team bot's agent row by its agent,
    whose database has this one's name. None when no row names it, such as in a database without session rows, which
    its scope's next store open converts instead.
    """
    if names.sessions not in table_names(connection):
        return None
    named = connection.execute(
        f"SELECT DISTINCT CASE WHEN session_type = 'team' THEN team_id ELSE agent_id END "  # noqa: S608
        f"FROM {quote_identifier(names.sessions)}",
    )
    for (scope_id,) in named:
        if isinstance(scope_id, str) and scope_id:
            scope = HistoryScope(kind="team", scope_id=scope_id)
            if TableNames.of_team(scope).storage_name == names.storage_name:
                return scope
    return None


__all__ = [
    "BACKUP_SUFFIX",
    "convert_session_columns",
    "stored_counts",
    "team_owner",
    "upgrade_layout",
]
