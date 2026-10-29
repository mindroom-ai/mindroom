"""Conversation databases in the Agno layout releases before storage v2 wrote, for the tests of their conversion."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING, Any

from mindroom.history.types import HistoryScope
from mindroom.session_storage.agent_storage import prepare_conversation_database
from mindroom.session_storage.legacy_agno3_schema import AgnoTableNames, agno_runs_sql
from mindroom.session_storage.schema import TableNames, table_names
from mindroom.session_storage.store import ConversationStore
from mindroom.session_storage.types import ConversationSession
from mindroom.usage.storage import SYSTEM_USAGE_OWNER, SYSTEM_USAGE_STORAGE_NAME, quote_identifier, usage_table_sql

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from pathlib import Path

    from mindroom.session_storage.types import SessionKind

# The session ``converted_store`` writes its runs into.
SESSION_ID = "session"
_TEAM_STORAGE_NAME = re.compile(r"team_(?P<scope_id>.+)_[0-9a-f]{12}")


def named_owner(names: TableNames) -> HistoryScope:
    """Return the owner a test database's name gives it: a team scope's ``team_<id>_<digest>``, else the agent it names.

    The system usage database is the system usage ledger's.
    """
    if names.storage_name == SYSTEM_USAGE_STORAGE_NAME:
        return SYSTEM_USAGE_OWNER
    if (match := _TEAM_STORAGE_NAME.fullmatch(names.storage_name)) is not None:
        scope = HistoryScope(kind="team", scope_id=match["scope_id"])
        assert TableNames.of_team(scope).storage_name == names.storage_name
        return scope
    return HistoryScope(kind="agent", scope_id=names.storage_name)


def recorded_owner(database: Path) -> HistoryScope:
    """Return the owner of a test database: the one its meta row records, else the team all its rows name, else its name's.

    Only a database converted to layout version 4 records its owner; one in an older layout names it on its rows.
    """
    names = TableNames.of_database(database)
    with closing(sqlite3.connect(database)) as connection:
        tables = table_names(connection)
        if names.log_meta in tables and "owner_id" in _columns(connection, names.log_meta):
            meta = quote_identifier(names.log_meta)
            [(kind, owner_id)] = connection.execute(f"SELECT owner_kind, owner_id FROM {meta}")  # noqa: S608
            return HistoryScope(kind=kind, scope_id=owner_id)
        if names.sessions in tables and "team_id" in _columns(connection, names.sessions):
            owners = connection.execute(
                f"SELECT DISTINCT session_type, team_id FROM {quote_identifier(names.sessions)}",  # noqa: S608
            ).fetchall()
            if len(owners) == 1 and owners[0][0] == "team" and isinstance(owners[0][1], str) and owners[0][1]:
                return HistoryScope(kind="team", scope_id=owners[0][1])
    return named_owner(names)


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {name for (name,) in connection.execute("SELECT name FROM pragma_table_info(?)", (table,))}


def write_agno_database(path: Path, *sessions: ConversationSession, usage: bool = True) -> Path:
    """Write ``sessions`` into a new Agno-layout database at ``path``, as a release before the summary rows left them.

    The file holds Agno 3's sessions and runs tables and, unless ``usage`` is false, the usage ledger, empty. Each
    session's row holds its state, its metadata, and its replayed summary in Agno's ``summary`` column; each of its
    runs is a row placed by ``run_index`` in list order, holding the run's stored dict, and dated by the run or, when it
    has no time, by its session.
    """
    names = AgnoTableNames.of_database(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as connection, connection:
        for statement in (*agno_runs_sql(names), *((usage_table_sql(names.sessions),) if usage else ())):
            connection.execute(statement)
        for session in sessions:
            summary = None if session.summary is None else json.dumps({"summary": session.summary.text})
            connection.execute(
                f"INSERT INTO {quote_identifier(names.sessions)} (session_id, session_type, "  # noqa: S608
                f"{'team_id' if session.kind == 'team' else 'agent_id'}, session_data, metadata, summary, created_at, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session.session_id,
                    session.kind,
                    session.owner_id,
                    json.dumps({"session_state": session.session_state}),
                    json.dumps(session.metadata),
                    summary,
                    session.created_at,
                    session.created_at,
                ),
            )
            _insert_runs(
                connection,
                names,
                session.session_id,
                (run.to_dict() for run in session.runs),
                created_at=session.created_at,
            )
    return path


def converted_store(
    path: Path,
    *runs: Mapping[str, Any],
    kind: SessionKind = "agent",
    owner_id: str = "code",
) -> ConversationStore:
    """Convert a new Agno-layout database at ``path`` holding ``runs`` and return its store.

    ``runs`` are stored run dicts as a release before storage v2 wrote them, the rows of one ``kind`` session of
    ``owner_id``, ``SESSION_ID``, placed by ``run_index`` in list order; a dict with ``member_responses`` is a team run.
    """
    write_agno_database(path, ConversationSession(session_id=SESSION_ID, kind=kind, owner_id=owner_id, created_at=1))
    with closing(sqlite3.connect(path)) as connection, connection:
        _insert_runs(connection, AgnoTableNames.of_database(path), SESSION_ID, runs, created_at=1)
    prepare_conversation_database(path, TableNames.of_database(path), HistoryScope(kind=kind, scope_id=owner_id))
    return ConversationStore(path)


def _insert_runs(
    connection: sqlite3.Connection,
    names: AgnoTableNames,
    session_id: str,
    runs: Iterable[Mapping[str, Any]],
    *,
    created_at: int,
) -> None:
    """Insert ``runs``, stored run dicts, as session ``session_id``'s rows, dated ``created_at`` when a run has no time."""
    connection.executemany(
        f"INSERT INTO {quote_identifier(names.runs)} (run_id, session_id, run_type, agent_id, team_id, "  # noqa: S608
        "user_id, parent_run_id, status, run_index, run_data, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                data["run_id"],
                session_id,
                "team" if "member_responses" in data else "agent",
                data.get("agent_id"),
                data.get("team_id"),
                data.get("user_id"),
                data.get("parent_run_id"),
                data["status"],
                run_index,
                json.dumps(data),
                data.get("created_at") or created_at,
                data.get("created_at") or created_at,
            )
            for run_index, data in enumerate(runs)
        ],
    )
