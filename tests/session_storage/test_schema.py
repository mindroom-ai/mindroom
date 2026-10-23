"""A conversation database keeps the table names Agno 3 gave it."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING

import pytest

from mindroom.session_storage.schema import TableNames, run_log_sql, schema_sql, table_names
from tests.session_storage.goldens import golden_stores

if TYPE_CHECKING:
    from pathlib import Path

    from tests.session_storage.goldens import GoldenStore


def test_a_databases_tables_are_all_named_after_its_storage_name() -> None:
    """``<name>.db`` holds ``<name>_sessions`` and the tables beside it; the pairing has this one owner."""
    names = TableNames("code")

    assert names.sessions == "code_sessions"
    assert names.runs == "code_sessions_runs"
    assert names.summaries == "code_sessions_summaries"
    assert names.removed_runs == "code_sessions_removed_runs"
    assert names.usage == "code_sessions_usage"


def test_the_schema_creates_exactly_the_tables_the_names_give_it() -> None:
    """The DDL of each owner creates the table ``TableNames`` names, so no table is named in two places."""
    names = TableNames("code")
    with closing(sqlite3.connect(":memory:")) as connection:
        for statement in schema_sql(names):
            connection.execute(statement)
        tables = table_names(connection)

    assert tables == {names.sessions, names.runs, names.summaries, names.removed_runs, names.usage}


@pytest.mark.parametrize("store", golden_stores(), ids=lambda store: store.label)
def test_the_pairing_is_the_one_every_database_agno_wrote_follows(store: GoldenStore, tmp_path: Path) -> None:
    """Each golden file ``<name>.db`` holds the sessions table ``TableNames(<name>)`` names, and its runs table."""
    with closing(sqlite3.connect(store.restore(tmp_path))) as connection:
        tables = table_names(connection)

    assert {store.names.sessions, store.names.runs} <= tables


@pytest.mark.parametrize(
    ("storage_name", "runs"),
    [("code", "code_sessions_runs"), ("agno", "agno_runs")],
)
def test_the_runs_table_keeps_the_name_agno_gave_it(storage_name: str, runs: str) -> None:
    """Agno 3 names the runs table beside its default ``agno_sessions`` table ``agno_runs``, else ``<sessions>_runs``."""
    names = TableNames(storage_name)
    with closing(sqlite3.connect(":memory:")) as connection:
        for statement in run_log_sql(names):
            connection.execute(statement)
        tables = table_names(connection)

    assert names.runs == runs
    assert tables == {names.sessions, runs}
