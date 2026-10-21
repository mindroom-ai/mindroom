"""A conversation database keeps the table names Agno 3 gave it."""

from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest

from mindroom.session_storage.schema import TableNames, run_log_sql


@pytest.mark.parametrize(
    ("sessions", "runs"),
    [("code_sessions", "code_sessions_runs"), ("agno_sessions", "agno_runs")],
)
def test_the_runs_table_keeps_the_name_agno_gave_it(sessions: str, runs: str) -> None:
    """Agno 3 names the runs table beside its default ``agno_sessions`` table ``agno_runs``, else ``<sessions>_runs``."""
    names = TableNames(sessions)
    with closing(sqlite3.connect(":memory:")) as connection:
        for statement in run_log_sql(names):
            connection.execute(statement)
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}

    assert names.runs == runs
    assert tables == {sessions, runs}
