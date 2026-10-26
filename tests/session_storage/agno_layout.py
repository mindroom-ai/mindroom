"""Conversation databases in the Agno layout releases before storage v2 wrote, for the tests of their conversion."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING

from mindroom.llm.runs import TeamRunRecord
from mindroom.session_storage.legacy_agno3_schema import AgnoTableNames, agno_runs_sql
from mindroom.usage.storage import quote_identifier, usage_table_sql

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.session_storage.types import ConversationSession


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
            connection.executemany(
                f"INSERT INTO {quote_identifier(names.runs)} (run_id, session_id, run_type, agent_id, team_id, "  # noqa: S608
                "user_id, parent_run_id, status, run_index, run_data, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        run.run_id,
                        session.session_id,
                        "team" if isinstance(run, TeamRunRecord) else "agent",
                        None if isinstance(run, TeamRunRecord) else run.agent_id,
                        run.team_id if isinstance(run, TeamRunRecord) else None,
                        run.user_id,
                        run.parent_run_id,
                        run.status.value,
                        run_index,
                        json.dumps(run.to_dict()),
                        run.created_at or session.created_at,
                        run.created_at or session.created_at,
                    )
                    for run_index, run in enumerate(session.runs)
                ],
            )
    return path
