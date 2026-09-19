"""Private Agno SQLite bindings and run-persistence repairs."""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, cast

from agno.db.sqlite import SqliteDb
from sqlalchemy import event

from mindroom import usage_archive

if TYPE_CHECKING:
    from collections.abc import Iterator

    from agno.run.agent import RunOutput
    from agno.run.team import TeamRunOutput
    from agno.run.workflow import WorkflowRunOutput
    from sqlalchemy import Engine, Table
    from sqlalchemy.orm import Session

_CONNECT_LISTENER_NAME = "_set_sqlite_pragmas"


# AGNO_COMPAT: SQLite journaling lacks a public configuration hook.
# Reason: SqliteDb unconditionally installs a WAL connect listener without a public
# journal-mode option. The owner must replace it to retain its chosen pragma policy.
# Upstream issue: No matching configurable SQLite pragma issue identified.
# Upstream PR: None identified for this extension point.
# Remove when: SqliteDb exposes configurable pragmas; preserve owner journaling and
# foreign-key behavior without inspecting SQLAlchemy's listener registry.
# Coverage: tests/test_agent_storage_runs.py::test_legacy_runs_blob_is_merged_into_reads_and_deletions_stick.
def remove_default_pragmas(engine: Engine) -> None:
    """Remove exactly the Agno pragma listener before the owner installs its own."""
    connect_listeners = cast("Any", engine.pool.dispatch).connect.listeners
    listeners = [listener for listener in connect_listeners if listener.__name__ == _CONNECT_LISTENER_NAME]
    if len(listeners) != 1:
        msg = f"Expected exactly one Agno {_CONNECT_LISTENER_NAME} connect listener, found {len(listeners)}"
        raise RuntimeError(msg)
    event.remove(engine, "connect", listeners[0])


# AGNO_COMPAT: Run insertion can reorder surviving stored runs.
# Reason: Agno accepts an in-memory run position that can precede surviving stored
# indexes after deletion. Its MAX+1 path is only used when the supplied index is None.
# Upstream issue: https://github.com/agno-agi/agno/issues/9936
# Upstream PR: https://github.com/agno-agi/agno/pull/9938
# Remove when: The pinned adapter appends new runs after existing indexes even when
# given a shortened session-list position; keep owner prompt sanitization and
# the archive-aware index floor and resurrected-ID position.
# Coverage: tests/test_agent_storage_runs.py::test_runs_appended_after_deleting_leading_runs_sort_after_the_survivors.
def upsert_run_at_end(
    db: SqliteDb,
    run: RunOutput | TeamRunOutput | WorkflowRunOutput | dict[str, Any],
    *,
    session_id: str,
    user_id: str | None,
) -> None:
    """Append atomically after live and archived indexes, retaining resurrected IDs' order."""
    run_id = cast("dict[str, Any]", run).get("run_id") if isinstance(run, dict) else run.run_id
    index = usage_archive.next_run_index(db, run_id, session_id)
    SqliteDb.upsert_run(db, run=run, session_id=session_id, user_id=user_id, run_index=cast("Any", index))


# AGNO_COMPAT: Run deletion and legacy-blob cleanup are not atomic.
# Reason: Agno deletes run rows and scrubs legacy blobs in separate transactions,
# swallows scrub failures, and skips legacy-only databases without a runs table.
# Upstream issue: https://github.com/agno-agi/agno/issues/9934
# Upstream PR: https://github.com/agno-agi/agno/pull/9939
# Remove when: The public adapter atomically deletes rows and legacy blobs, including
# legacy-only databases; retain owner descendant discovery and legacy-format policy.
# Coverage: tests/test_agent_storage_runs.py.
@contextmanager
def run_deletion_transaction(db: SqliteDb) -> Iterator[tuple[Session, Table | None, Table | None]]:
    """Keep the owner's run deletion and legacy scrub inside one Agno transaction."""
    runs_table = db._get_table(table_type="runs")
    sessions_table = db._get_table(table_type="sessions")
    with db.Session() as session, session.begin():
        yield session, runs_table, sessions_table


# AGNO_COMPAT: Session-cache diagnostics require private counters.
# Reason: Agno exposes no cache statistics; diagnostics currently read its private
# per-session run map. Sampling, aggregation and storage lifetime belong to the owner.
# Upstream issue: No matching public session-cache statistics issue identified.
# Upstream PR: None identified for this observability API.
# Remove when: Public cache counters expose the same session/run counts.
# Coverage: tests/test_agent_storage_runs.py.
def cached_run_counts(db: SqliteDb) -> tuple[int, int]:
    """Count this adapter's cached sessions/runs without retaining their contents."""
    sessions = db._run_object_cache._per_session
    return len(sessions), sum(len(runs) for runs in sessions.values())
