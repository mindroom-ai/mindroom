"""Rename an agent inside an Agno session store: its tables, agent ids, team ids and history scope keys.

A session store is a SQLite database ``<name>.db`` holding the tables ``<name>_sessions`` and
``<name>_sessions_*`` (runs, usage, summaries, removed runs, legacy compactions). An agent's own store and the
store of a team history scope it belongs to both name the agent: in agent ids, in the runs its members wrote,
and, when the store's name or history scope derives from the agent, in table names, team ids and scope keys.
Names match exactly, never as substrings, so renaming ``code`` leaves ``code_team`` alone.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, cast

from mindroom.agent_rename.sqlite_read import quote_identifier
from mindroom.agent_rename.steps import MovePath, SqliteEdit
from mindroom.agent_rename.workers import DestinationExistsError
from mindroom.constants import (
    MINDROOM_COMPACTION_METADATA_KEY,
    MINDROOM_MATRIX_HISTORY_METADATA_KEY,
    PENDING_COMPACTION_SCOPE_KEYS_SESSION_STATE_KEY,
)

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Iterable
    from pathlib import Path

    from mindroom.agent_rename.steps import Step
    from mindroom.history.types import HistoryScope

# A crashed writer can leave commits or a rollback journal beside a database; SQLite finds them by name.
_SQLITE_SIDE_FILES = ("-journal", "-wal", "-shm")
# Agno's default schema-version table (agno/db/base.py), which MindRoom does not rename.
_SCHEMA_VERSIONS_TABLE = "agno_schema_versions"
# The stored head of a CREATE INDEX statement and the index name it names.
_INDEX_HEAD = re.compile(
    r'CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?("(?:[^"]|"")*"|\[[^\]]*\]|`(?:[^`]|``)*`|[^\s(]+)',
    re.IGNORECASE,
)


@dataclass(frozen=True)
class StoreRename:
    """What renaming the agent changes in one session store.

    The agent id ``old_name`` always becomes ``new_name``. When the store's history scope changes with the agent,
    ``old_scope`` becomes ``new_scope``: its scope key, and for a team scope the team id, which is the scope id.
    Every value renamed names the agent verbatim, as agent names hold no character that JSON escapes.
    """

    old_name: str
    new_name: str
    old_scope: HistoryScope | None = None
    new_scope: HistoryScope | None = None

    def scope_keys(self) -> tuple[str, str] | None:
        """Return the old and new history scope keys, or ``None`` when the store keeps its scope."""
        if self.old_scope is None or self.new_scope is None:
            return None
        return self.old_scope.key, self.new_scope.key

    def team_ids(self) -> tuple[str, str] | None:
        """Return the old and new team ids, or ``None`` when no team scope of the store changes."""
        if self.old_scope is None or self.new_scope is None or self.old_scope.kind != "team":
            return None
        return self.old_scope.scope_id, self.new_scope.scope_id


def database_steps(
    old_directory: Path,
    new_directory: Path,
    *,
    old_stem: str,
    new_stem: str,
    edit: Callable[[sqlite3.Connection], None],
    edit_label: str,
    move_label: str,
) -> list[Step]:
    """Edit ``<old_stem>.db`` in the moved directory, then rename it to ``<new_stem>.db``.

    ``old_directory`` is where the database is now, ``new_directory`` where an earlier step moves it.
    The steps read ``<edit_label> <database>`` and ``Move <move_label> <database> to <renamed database>``.
    A crashed writer can leave a ``-wal`` file with commits or a hot ``-journal`` beside the database.
    The edit's own connection replays or rolls back and removes them, so only the database file is renamed,
    and undoing the edit restores a backup that already holds what they held.
    A stale side file under the new name would be read as the renamed database's, so it refuses.
    """
    old_file, new_file = f"{old_stem}.db", f"{new_stem}.db"
    if not os.path.lexists(old_directory / old_file):
        return []
    for suffix in ("", *_SQLITE_SIDE_FILES):
        if os.path.lexists(old_directory / f"{new_file}{suffix}"):
            msg = f"Cannot rename {old_directory / old_file}: {old_directory / f'{new_file}{suffix}'} already exists."
            raise DestinationExistsError(msg)
    source, destination = new_directory / old_file, new_directory / new_file
    return [
        SqliteEdit(source, edit, f"{edit_label} {source}"),
        MovePath(source, destination, f"Move {move_label} {source} to {destination}"),
    ]


def rename_session_database(
    connection: sqlite3.Connection,
    *,
    old_table: str,
    new_table: str,
    rename: StoreRename,
    other_tables: tuple[str, ...] = (),
) -> None:
    """Rename the tables named ``old_table`` or ``old_table_*`` with their indexes, then the agent inside them.

    With ``new_table`` equal to ``old_table`` the tables keep their names. ``other_tables`` are tables whose
    names stay but whose rows name the agent too.
    """
    # Renaming a table then also renames it in other tables' foreign keys, indexes and triggers.
    connection.execute("PRAGMA legacy_alter_table = OFF")
    tables = [
        name
        for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        if name == old_table or name.startswith(f"{old_table}_")
    ]
    renamed = {name: new_table + name.removeprefix(old_table) for name in tables}
    if new_table != old_table:
        for old, new in renamed.items():
            connection.execute(f"ALTER TABLE {quote_identifier(old)} RENAME TO {quote_identifier(new)}")
        _rename_indexes(connection, renamed.values(), old_table=old_table, new_table=new_table)
        if _table_exists(connection, _SCHEMA_VERSIONS_TABLE):
            for old, new in renamed.items():
                connection.execute(
                    f"UPDATE {_SCHEMA_VERSIONS_TABLE} SET table_name = ? WHERE table_name = ?",  # noqa: S608
                    (new, old),
                )
    for table in (*renamed.values(), *(name for name in other_tables if _table_exists(connection, name))):
        _rename_in_table(connection, table, rename)


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    query = "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?"
    return connection.execute(query, (name,)).fetchone() is not None


def _rename_indexes(connection: sqlite3.Connection, tables: Iterable[str], *, old_table: str, new_table: str) -> None:
    """Recreate the indexes of ``tables`` whose names start with the old table name under the new one.

    Agno names indexes ``idx_<table>_<columns>`` and ``<table>_<name>``, MindRoom ``<table>_<name>``.
    SQLite's own indexes have no SQL and take the new name with their table.
    """
    rows = connection.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL "
        "AND tbl_name IN (SELECT value FROM json_each(?))",
        (json.dumps(list(tables)),),
    ).fetchall()
    for name, sql in rows:
        new_name = next(
            (
                head + new_table + name.removeprefix(head + old_table)
                for head in ("idx_", "")
                if name.startswith(head + old_table)
            ),
            None,
        )
        if new_name is None:
            continue
        connection.execute(f"DROP INDEX {quote_identifier(name)}")
        connection.execute(_with_index_name(sql, name, new_name))


def _with_index_name(sql: str, name: str, new_name: str) -> str:
    """Return the stored ``CREATE INDEX`` statement of index ``name`` creating ``new_name`` instead."""
    match = _INDEX_HEAD.match(sql)
    token = match.group(1) if match is not None else ""
    if token[:1] in {'"', "`"}:
        token = token[1:-1].replace(token[0] * 2, token[0])
    elif token[:1] == "[":
        token = token[1:-1]
    if match is None or token != name:
        msg = f"Cannot read the definition of index {name}: {sql}"
        raise RuntimeError(msg)
    return sql[: match.start(1)] + quote_identifier(new_name) + sql[match.end(1) :]


def _rename_in_table(connection: sqlite3.Connection, table: str, rename: StoreRename) -> None:
    """Set the agent ids, team ids, history scope keys and the matching fields of JSON columns from old to new."""
    quoted = quote_identifier(table)
    columns = {name for (name,) in connection.execute("SELECT name FROM pragma_table_info(?)", (table,))}
    scope_keys, team_ids = rename.scope_keys(), rename.team_ids()
    column_renames = {
        "agent_id": (rename.old_name, rename.new_name),
        "scope_key": scope_keys,
        "team_id": team_ids,
    }
    for column, values in column_renames.items():
        if column in columns and values is not None:
            connection.execute(
                f"UPDATE {quoted} SET {quote_identifier(column)} = ? WHERE {quote_identifier(column)} = ?",  # noqa: S608
                (values[1], values[0]),
            )
    renames: dict[str, Callable[[object], object]] = {
        "agent_data": partial(_renamed_field, field="agent_id", values=(rename.old_name, rename.new_name)),
        "team_data": partial(_renamed_field, field="team_id", values=team_ids),
        "run_data": partial(_renamed_run, rename=rename),
        # The Agno 2 run list that the open-time migration turns into run rows (legacy_session_storage.py).
        "runs": partial(_renamed_runs, rename=rename),
        # A mid-turn fold's segment holds the member responses it took out of its run (history/summary_rows.py).
        "segment": partial(_renamed_run, rename=rename),
        # MindRoom's usage snapshot of a run keeps its team id (usage/storage.py's project_usage).
        "usage_data": partial(_renamed_field, field="team_id", values=team_ids),
        # MindRoom keys per-scope history state by scope key (history/storage.py).
        "metadata": partial(_renamed_scope_states, scope_keys=scope_keys),
        "session_data": partial(_renamed_pending_compactions, scope_keys=scope_keys),
    }
    for column, rename_value in renames.items():
        if column not in columns:
            continue
        quoted_column = quote_identifier(column)
        # Every value renamed names the agent verbatim, so only values containing its name can change.
        rows = connection.execute(
            f"SELECT rowid, {quoted_column} FROM {quoted} WHERE instr({quoted_column}, ?) > 0",  # noqa: S608
            (rename.old_name,),
        ).fetchall()
        for rowid, raw in rows:
            text = _renamed_json(raw, rename_value)
            if text is not None:
                connection.execute(f"UPDATE {quoted} SET {quoted_column} = ? WHERE rowid = ?", (text, rowid))  # noqa: S608


def _renamed_json(raw: object, rename: Callable[[object], object]) -> str | None:
    """Return the JSON text with ``rename`` applied, encoded as often as before, or ``None`` when nothing changes.

    Older releases stored some session values as the JSON text of their JSON text.
    Values that are not JSON are left alone, as MindRoom cannot read them either.
    """
    if not isinstance(raw, str):
        return None
    try:
        value = json.loads(raw)
        nested = isinstance(value, str)
        if nested:
            value = json.loads(value)
    except (RecursionError, ValueError):
        return None
    renamed = rename(value)
    if renamed == value:
        return None
    # Agno writes its JSON columns through SQLAlchemy's default serializer, json.dumps.
    text = json.dumps(renamed)
    return json.dumps(text) if nested else text


def _renamed_field(value: object, *, field: str, values: tuple[str, str] | None) -> object:
    """Return ``value`` with its ``field`` set to the new value when it is a mapping whose ``field`` is the old one."""
    if values is not None and isinstance(value, dict) and cast("dict[str, object]", value).get(field) == values[0]:
        return {**cast("dict[str, object]", value), field: values[1]}
    return value


def _renamed_run(value: object, *, rename: StoreRename) -> object:
    """Rename the agent and team ids Agno 3.0.9 writes into a run and into the runs and events nested in it.

    A run names its agent in ``agent_id`` or its team in ``team_id``, and its requirements name the member that
    raised them in ``member_agent_id``. A team run nests its members' runs in ``member_responses``, and both kinds
    nest their events, which carry the same fields; those are renamed the same way.
    """
    run = _renamed_field(value, field="agent_id", values=(rename.old_name, rename.new_name))
    run = _renamed_field(run, field="team_id", values=rename.team_ids())
    if not isinstance(run, dict):
        return run
    renamed = dict(cast("dict[str, object]", run))
    for key in ("events", "member_responses"):
        items = renamed.get(key)
        if isinstance(items, list):
            renamed[key] = [_renamed_run(item, rename=rename) for item in cast("list[object]", items)]
    requirements = renamed.get("requirements")
    if isinstance(requirements, list):
        renamed["requirements"] = [
            _renamed_field(item, field="member_agent_id", values=(rename.old_name, rename.new_name))
            for item in cast("list[object]", requirements)
        ]
    return renamed


def _renamed_runs(value: object, *, rename: StoreRename) -> object:
    if not isinstance(value, list):
        return value
    return [_renamed_run(run, rename=rename) for run in cast("list[object]", value)]


def _renamed_scope_states(value: object, *, scope_keys: tuple[str, str] | None) -> object:
    """Rename the old scope among the per-scope states MindRoom keeps in session metadata."""
    if scope_keys is None or not isinstance(value, dict):
        return value
    old, new = scope_keys
    renamed = dict(cast("dict[str, object]", value))
    for key in (MINDROOM_COMPACTION_METADATA_KEY, MINDROOM_MATRIX_HISTORY_METADATA_KEY):
        entry = renamed.get(key)
        states = cast("dict[str, object]", entry).get("states") if isinstance(entry, dict) else None
        if isinstance(states, dict):
            renamed[key] = {
                **cast("dict[str, object]", entry),
                "states": {new if scope == old else scope: state for scope, state in states.items()},
            }
    return renamed


def _renamed_pending_compactions(value: object, *, scope_keys: tuple[str, str] | None) -> object:
    """Rename the old scope in the compactions MindRoom queues in Agno's ``session_state``; nothing else there."""
    session_state = cast("dict[str, object]", value).get("session_state") if isinstance(value, dict) else None
    pending = (
        cast("dict[str, object]", session_state).get(PENDING_COMPACTION_SCOPE_KEYS_SESSION_STATE_KEY)
        if isinstance(session_state, dict)
        else None
    )
    if scope_keys is None or not isinstance(pending, list):
        return value
    old, new = scope_keys
    renamed_pending = [new if scope == old else scope for scope in cast("list[object]", pending)]
    return {
        **cast("dict[str, object]", value),
        "session_state": {
            **cast("dict[str, object]", session_state),
            PENDING_COMPACTION_SCOPE_KEYS_SESSION_STATE_KEY: renamed_pending,
        },
    }
