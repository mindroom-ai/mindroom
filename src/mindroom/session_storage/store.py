"""The conversation store: one SQLite database of sessions, their run log, summary rows, and usage ledger.

Every operation runs in one transaction on its own short-lived connection and leaves the journal mode alone, so
databases keep SQLite's default rollback journal. A write takes the write lock before it reads anything its rules
depend on, and a load reads the session row, its runs, and its summary from one snapshot.
"""

from __future__ import annotations

import copy
import json
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from time import time
from typing import TYPE_CHECKING, Any, cast

from mindroom.history.summary_rows_schema import (
    NON_VISIBLE_RUN_STATUS_VALUES,
    latest_row_index_sql,
    loads_sql,
    owner_scope_key,
)
from mindroom.history.types import ScopeSummary
from mindroom.llm.runs import RunStatus
from mindroom.llm.usage import ModelMetrics, RunMetrics, SessionMetrics
from mindroom.session_storage.codec import run_from_row, run_type
from mindroom.session_storage.lanes import run_session_storage_operation
from mindroom.session_storage.legacy_agno2_runs import decode_session_json, has_blob_runs, move_blob_runs_to_rows
from mindroom.session_storage.schema import run_log_sql, schema_sql
from mindroom.session_storage.types import ConversationSession, SessionFields
from mindroom.usage.storage import TOKEN_FIELDS, project_usage, quote_identifier, usage_upsert_sql

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Collection, Iterable, Iterator, Mapping
    from pathlib import Path

    from mindroom.llm.runs import AnyRunRecord
    from mindroom.session_storage.schema import TableNames
    from mindroom.session_storage.types import SaveOutcome, SessionField, SessionKind
    from mindroom.usage.storage import IndependentUsageKind

_BUSY_TIMEOUT_SECONDS = 30.0
_NON_VISIBLE_STATUS_SQL = ", ".join(f"'{status}'" for status in NON_VISIBLE_RUN_STATUS_VALUES)


def _connect(path: Path) -> sqlite3.Connection:
    # No implicit transactions: every operation begins its own, so its reads cannot straddle a commit.
    connection = sqlite3.connect(path, timeout=_BUSY_TIMEOUT_SECONDS, isolation_level=None)
    # Deleting a session row cascades to its runs, summary rows, and usage.
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def open_conversation_store(
    path: Path,
    names: TableNames,
    *,
    prompt_roles: frozenset[str] = frozenset(),
) -> ConversationStore:
    """Open the conversation database at ``path``, moving runs Agno 2 kept in session blobs into run rows once.

    ``prompt_roles`` are message roles, such as ``system``, that stored runs leave out unless they are paused.
    Tables are created by the first write. Migrations that recognize an older database by a missing summary-row
    or usage table must run before that write.
    """
    with closing(_connect(path)) as connection:
        if has_blob_runs(connection, names):
            connection.execute("BEGIN IMMEDIATE")
            try:
                # Another opener may have moved them while this one waited for the write lock.
                if has_blob_runs(connection, names):
                    for statement in run_log_sql(names):
                        connection.execute(statement)
                    move_blob_runs_to_rows(connection, names)
            except BaseException:
                connection.rollback()
                raise
            connection.commit()
    return ConversationStore(path=path, names=names, prompt_roles=prompt_roles)


@dataclass(frozen=True, slots=True)
class ConversationStore:
    """One conversation database; it holds no connection, so there is nothing to close."""

    path: Path
    names: TableNames
    prompt_roles: frozenset[str] = frozenset()

    @contextmanager
    def transaction(self, *, write: bool) -> Iterator[sqlite3.Connection]:
        """Run one transaction on its own connection, committing when the block exits normally.

        A write takes the write lock first, so rules that read the log before writing see what they change,
        and creates any missing table. A read is one snapshot.
        """
        with closing(_connect(self.path)) as connection:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                if write:
                    for statement in schema_sql(self.names):
                        connection.execute(statement)
                yield connection
            except BaseException:
                connection.rollback()
                raise
            connection.commit()

    def read_session_fields(self, session_id: str) -> SessionFields | None:
        """Read one session row's metadata and ``session_state`` without its runs, or None when the row is missing."""
        with self.transaction(write=False) as connection:
            row = self._session_row(connection, session_id)
        if row is None:
            return None
        return SessionFields(metadata=_decoded_mapping(row[5]), session_state=_session_state(row[4]))

    def load_session(self, session_id: str, kind: SessionKind) -> ConversationSession | None:
        """Load one session with the runs a reply reasons over, or None when it is missing or of another kind.

        The runs are those after the owner scope's latest summary row, every paused, cancelled, or errored run,
        and their member runs, in log order; a run folded mid-turn loads without the messages its segments hold.
        """
        names = self.names
        with self.transaction(write=False) as connection:
            tables = _tables(connection)
            row = self._session_row(connection, session_id)
            if row is None or row[0] != kind:
                return None
            stored_kind, agent_id, team_id, user_id, session_data, metadata, created_at, updated_at = row
            summaries = quote_identifier(names.summaries) if names.summaries in tables else None
            run_rows = _loaded_run_rows(connection, names, summaries, session_id) if names.runs in tables else []
            folded = _folded_message_ids(connection, summaries, session_id, [run_id for run_id, _, _ in run_rows])
            summary = _replay_summary(
                connection,
                summaries,
                session_id,
                owner_scope_key(stored_kind, agent_id, team_id),
            )
        runs: list[AnyRunRecord] = []
        for run_id, stored_type, run_data in run_rows:
            run = run_from_row(stored_type, run_data)
            if run is None:
                continue
            if run_id in folded:
                run.messages = [message for message in run.messages if message.id not in folded[run_id]]
            runs.append(run)
        return ConversationSession(
            session_id=session_id,
            kind=kind,
            owner_id=(team_id if kind == "team" else agent_id) or "",
            user_id=user_id,
            metadata=_decoded_mapping(metadata),
            session_state=_session_state(session_data),
            runs=runs,
            summary=summary,
            created_at=created_at,
            updated_at=updated_at,
        )

    def _session_row(self, connection: sqlite3.Connection, session_id: str) -> tuple[Any, ...] | None:
        if self.names.sessions not in _tables(connection):
            return None
        return connection.execute(
            "SELECT session_type, agent_id, team_id, user_id, session_data, metadata, created_at, updated_at "  # noqa: S608
            f"FROM {quote_identifier(self.names.sessions)} WHERE session_id = ?",
            (session_id,),
        ).fetchone()

    def ensure_session_row(self, session: ConversationSession) -> None:
        """Create ``session``'s row unless one exists, keeping a row another writer created meanwhile."""
        with self.transaction(write=True) as connection:
            _ensure_session_row(connection, self.names, session)

    def save_run(self, session: ConversationSession, run: AnyRunRecord, *, record_usage: bool) -> SaveOutcome:
        """Save one run of ``session`` under the conversation log's insertion rules, creating the session row if needed.

        A run a cut removed, or a member of one, writes no row (``"removed"``). A visible run behind its scope's
        latest summary row, or a member of one, stays as stored (``"frozen"``). A run new to the session, or saved
        visible over a stored paused, cancelled, or errored row, takes the next position after every run and
        summary row. With ``record_usage``, the run's usage replaces its earlier snapshot in the usage ledger,
        which outlives removed runs, and the session totals change by the difference, in the same transaction.
        A frozen run's usage is final and changes neither.
        """
        return self._save_run_dict(session, run.to_dict(), row_type=run_type(run), record_usage=record_usage)

    def _save_run_dict(
        self,
        session: ConversationSession,
        data: dict[str, Any],
        *,
        row_type: SessionKind,
        record_usage: bool,
    ) -> SaveOutcome:
        with self.transaction(write=True) as connection:
            _ensure_session_row(connection, self.names, session)
            return self._save_run(connection, session.session_id, data, row_type=row_type, record_usage=record_usage)

    def save_runs(self, session: ConversationSession, runs: Iterable[AnyRunRecord]) -> None:
        """Write conversation-only changes to runs of ``session`` in one transaction and make them its copies.

        Conversation rewrites leave the usage ledger and session totals untouched. Only written runs replace the
        session's copies; a removed run leaves the session and a frozen one stays as stored. So callers edit a
        copy and hand the copy here: a refused save must leave the session's copy as stored.
        """
        runs = list(runs)
        if not runs:
            return
        loaded = {id(existing) for existing in session.runs}
        if any(id(run) in loaded for run in runs):
            msg = "save_runs received a run object loaded from the session; edit a copy instead"
            raise ValueError(msg)
        with self.transaction(write=True) as connection:
            _ensure_session_row(connection, self.names, session)
            outcomes = {
                run.run_id: self._save_run(
                    connection,
                    session.session_id,
                    run.to_dict(),
                    row_type=run_type(run),
                    record_usage=False,
                )
                for run in runs
            }
        written = {run.run_id: run for run in runs if outcomes[run.run_id] == "written"}
        loaded_ids = {existing.run_id for existing in session.runs}
        session.runs = [
            *(
                written.get(existing.run_id, existing)
                for existing in session.runs
                if outcomes.get(existing.run_id) != "removed"
            ),
            *(run for run_id, run in written.items() if run_id not in loaded_ids),
        ]

    def save_session_fields(self, session: ConversationSession, *fields: SessionField) -> None:
        """Write the named fields of ``session`` to its row, creating the row if needed, and leave the rest alone."""
        names = self.names
        with self.transaction(write=True) as connection:
            _ensure_session_row(connection, names, session)
            assignments: list[str] = ["updated_at = ?"]
            values: list[object] = [int(time())]
            if "metadata" in fields:
                assignments.append("metadata = ?")
                values.append(_json_or_null(session.metadata))
            if "session_state" in fields:
                (stored,) = connection.execute(
                    f"SELECT session_data FROM {quote_identifier(names.sessions)} WHERE session_id = ?",  # noqa: S608
                    (session.session_id,),
                ).fetchone()
                assignments.append("session_data = ?")
                values.append(json.dumps({**(_decoded_mapping(stored) or {}), "session_state": session.session_state}))
            connection.execute(
                f"UPDATE {quote_identifier(names.sessions)} SET {', '.join(assignments)} WHERE session_id = ?",  # noqa: S608
                (*values, session.session_id),
            )

    def delete_run_subtrees(self, run_ids: Collection[str], *, session_id: str | None = None) -> set[str]:
        """Delete ``run_ids`` with every member run below them, within one session when given; return the deleted ids."""
        with self.transaction(write=True) as connection:
            return delete_run_subtrees_in_transaction(connection, self.names, run_ids, session_id=session_id)

    def get_run(self, run_id: str) -> AnyRunRecord | None:
        """Return one stored run, in whatever session, as stored."""
        with self.transaction(write=False) as connection:
            if self.names.runs not in _tables(connection):
                return None
            row = connection.execute(
                f"SELECT run_type, CAST(run_data AS TEXT) FROM {quote_identifier(self.names.runs)} WHERE run_id = ?",  # noqa: S608
                (run_id,),
            ).fetchone()
        return None if row is None else run_from_row(*row)

    def list_runs(self, session_id: str) -> list[AnyRunRecord]:
        """Return the session's whole run log as stored, member runs included, in log order."""
        with self.transaction(write=False) as connection:
            if self.names.runs not in _tables(connection):
                return []
            rows = connection.execute(
                f"SELECT run_type, CAST(run_data AS TEXT) FROM {quote_identifier(self.names.runs)} "  # noqa: S608
                "WHERE session_id = ? ORDER BY run_index, created_at, run_id",
                (session_id,),
            ).fetchall()
        return [run for run in (run_from_row(*row) for row in rows) if run is not None]

    def list_updated_sessions(self, kind: SessionKind, owner_id: str, *, since: int) -> list[tuple[str, int]]:
        """Return ``(session_id, updated_at)`` of ``owner_id``'s sessions updated after ``since``, oldest first.

        A session's update time moves when its row is written: a field save, or a run save that records usage.
        """
        owner_column = "team_id" if kind == "team" else "agent_id"
        with self.transaction(write=False) as connection:
            if self.names.sessions not in _tables(connection):
                return []
            return connection.execute(
                "SELECT session_id, COALESCE(updated_at, created_at) AS updated "  # noqa: S608
                f"FROM {quote_identifier(self.names.sessions)} WHERE session_type = ? AND {owner_column} = ? "
                "AND COALESCE(updated_at, created_at) > ? ORDER BY updated, session_id",
                (kind, owner_id, since),
            ).fetchall()

    def save_independent_usage(
        self,
        *,
        session_id: str,
        usage_id: str,
        kind: IndependentUsageKind,
        requester_id: str | None,
        run: Mapping[str, object],
        initial_session: ConversationSession | None = None,
    ) -> None:
        """Upsert content-free helper usage, owned by a conversation and a requester, beside the run log.

        ``initial_session`` creates the conversation's row when no run has yet. Helper usage is not part of the
        session totals.
        """
        snapshot = project_usage(
            {
                **run,
                "run_id": usage_id,
                "user_id": requester_id,
                "metadata": None,
                "parent_run_id": None,
                "team_id": None,
            },
        )
        snapshot["kind"] = kind
        with self.transaction(write=True) as connection:
            if initial_session is not None:
                _ensure_session_row(connection, self.names, initial_session)
            connection.execute(usage_upsert_sql(self.names.sessions), (session_id, usage_id, json.dumps(snapshot)))

    def _save_run(
        self,
        connection: sqlite3.Connection,
        session_id: str,
        data: dict[str, Any],
        *,
        row_type: SessionKind,
        record_usage: bool,
    ) -> SaveOutcome:
        """Save one run from its stored dict (``record.to_dict()``), under the insertion rules."""
        if data["status"] != RunStatus.paused.value and self.prompt_roles:
            # Prompts are rebuilt from config on every request; a paused run keeps them until it continues.
            data["messages"] = [message for message in data["messages"] if message["role"] not in self.prompt_roles]
        stored = _write_run_row(connection, self.names, data, row_type=row_type, session_id=session_id)
        outcome: SaveOutcome = stored if isinstance(stored, str) else "written"
        if not record_usage or outcome == "frozen":
            return outcome
        now = int(time())
        created_at, updated_at = stored if not isinstance(stored, str) else (data.get("created_at") or now, now)
        snapshot = project_usage({**data, "created_at": created_at, "updated_at": updated_at})
        _record_run_usage(connection, self.names, session_id, data["run_id"], snapshot)
        return outcome


def persist_to(store: ConversationStore, session: ConversationSession) -> Callable[[AnyRunRecord], Awaitable[None]]:
    """Return the callback a run hands its terminal record to: save it to ``store`` with its usage, in the database's lane.

    The record is serialized on the caller's task before the lane is awaited, so what lands is the record as it was
    handed over. ``session.session_state``, which the run's tools change in place, is written back by field when it
    differs from what the session held when this callback was made, after the record and in the same lane.
    """
    loaded_state = copy.deepcopy(session.session_state)

    async def persist(record: AnyRunRecord) -> None:
        data = record.to_dict()
        row_type = run_type(record)
        state = copy.deepcopy(session.session_state) if session.session_state != loaded_state else None

        def operation(opened: ConversationStore) -> None:
            opened._save_run_dict(session, data, row_type=row_type, record_usage=True)
            if state is not None:
                opened.save_session_fields(replace(session, session_state=state), "session_state")

        await run_session_storage_operation(lambda: store, operation)

    return persist


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {name for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _decoded_mapping(raw_value: object) -> dict[str, Any] | None:
    """Return a stored JSON object column, or None when it is empty or unreadable, so one odd row never fails a load."""
    try:
        value = decode_session_json(raw_value)
    except (TypeError, ValueError):
        return None
    return cast("dict[str, Any]", value) if isinstance(value, dict) else None


def _session_state(raw_session_data: object) -> dict[str, Any]:
    """Return the ``session_state`` object inside a row's ``session_data``, or an empty one."""
    state = (_decoded_mapping(raw_session_data) or {}).get("session_state")
    return state if isinstance(state, dict) else {}


def _json_or_null(value: object) -> str | None:
    return None if value is None else json.dumps(value)


def _ensure_session_row(connection: sqlite3.Connection, names: TableNames, session: ConversationSession) -> None:
    """Create ``session``'s row unless one exists, keeping a row another writer created meanwhile."""
    owner_column = "team_id" if session.kind == "team" else "agent_id"
    connection.execute(
        f"INSERT INTO {quote_identifier(names.sessions)} (session_id, session_type, {owner_column}, user_id, "  # noqa: S608
        "session_data, metadata, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(session_id) DO NOTHING",
        (
            session.session_id,
            session.kind,
            session.owner_id,
            session.user_id,
            json.dumps({"session_state": session.session_state}),
            _json_or_null(session.metadata),
            session.created_at,
            session.created_at,
        ),
    )


def _write_run_row(
    connection: sqlite3.Connection,
    names: TableNames,
    data: dict[str, Any],
    *,
    row_type: str,
    session_id: str,
) -> tuple[int, int] | SaveOutcome:
    """Upsert one run row under the conversation log's rules; return its timestamps, or why no row was written."""
    run_id, parent_run_id = data["run_id"], data.get("parent_run_id")
    runs, summaries, removed = (quote_identifier(name) for name in (names.runs, names.summaries, names.removed_runs))
    if connection.execute(f"SELECT 1 FROM {removed} WHERE run_id IN (?, ?)", (run_id, parent_run_id)).fetchone():  # noqa: S608
        return "removed"
    if _is_folded(connection, runs, summaries, run_id, parent_run_id):
        return "frozen"
    (run_index,) = connection.execute(
        f"SELECT MAX(COALESCE((SELECT MAX(run_index) FROM {runs} WHERE session_id = ?), -1), "  # noqa: S608
        f"COALESCE((SELECT MAX(after_run_index) FROM {summaries} WHERE session_id = ?), -1)) + 1",
        (session_id, session_id),
    ).fetchone()
    now = int(time())
    # A run saved visible over its stored paused, cancelled, or errored row was finished after the turns it waited
    # for, so it moves after them; any other update keeps the run's place.
    return connection.execute(
        f"INSERT INTO {runs} AS run (run_id, session_id, run_type, agent_id, team_id, user_id, parent_run_id, "  # noqa: S608
        "status, run_index, run_data, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(run_id) DO UPDATE SET status = excluded.status, run_data = excluded.run_data, "
        "user_id = excluded.user_id, parent_run_id = excluded.parent_run_id, updated_at = excluded.updated_at, "
        f"run_index = CASE WHEN run.parent_run_id IS NULL AND run.status IN ({_NON_VISIBLE_STATUS_SQL}) "
        f"AND excluded.parent_run_id IS NULL AND excluded.status NOT IN ({_NON_VISIBLE_STATUS_SQL}) "
        "THEN excluded.run_index ELSE COALESCE(run.run_index, excluded.run_index) END "
        "RETURNING created_at, updated_at",
        (
            run_id,
            session_id,
            row_type,
            data.get("agent_id"),
            data.get("team_id"),
            data.get("user_id"),
            parent_run_id,
            data["status"],
            run_index,
            json.dumps(data),
            data.get("created_at") or now,
            now,
        ),
    ).fetchone()


def _is_folded(
    connection: sqlite3.Connection,
    runs: str,
    summaries: str,
    run_id: str,
    parent_run_id: str | None,
) -> bool:
    """Return whether a run, or the top-level run it belongs to, is visible and behind its scope's latest summary row.

    The chain starts at the stored run and at the parent the save names, so a member row seen for the first
    time is as final as the folded team run it belongs to.
    """
    return (
        connection.execute(
            "WITH RECURSIVE chain(run_id, parent_run_id, run_index, status, run_type, agent_id, team_id, session_id) "  # noqa: S608
            f"AS (SELECT run_id, parent_run_id, run_index, status, run_type, agent_id, team_id, session_id FROM {runs} "
            "WHERE run_id IN (?, ?) UNION SELECT run.run_id, run.parent_run_id, run.run_index, run.status, run.run_type, "
            f"run.agent_id, run.team_id, run.session_id FROM {runs} AS run JOIN chain ON run.run_id = chain.parent_run_id) "
            "SELECT 1 FROM chain AS top WHERE top.parent_run_id IS NULL "
            f"AND NOT {loads_sql('top', latest_row_index_sql(summaries, 'top'))}",
            (run_id, parent_run_id),
        ).fetchone()
        is not None
    )


def _record_run_usage(
    connection: sqlite3.Connection,
    names: TableNames,
    session_id: str,
    run_id: str,
    snapshot: dict[str, object],
) -> None:
    """Replace the run's usage snapshot and move the session totals by the difference from the snapshot it replaces.

    A run saved again when it continues after a pause, or at a checkpoint, so counts once in the totals. The totals
    count what the ledger records: token counters, in total and per model.
    """
    usage, sessions = quote_identifier(names.usage), quote_identifier(names.sessions)
    previous = connection.execute(
        f"SELECT usage_data FROM {usage} WHERE session_id = ? AND run_id = ?",  # noqa: S608
        (session_id, run_id),
    ).fetchone()
    connection.execute(usage_upsert_sql(names.sessions), (session_id, run_id, json.dumps(snapshot)))
    (raw_session_data,) = connection.execute(
        f"SELECT session_data FROM {sessions} WHERE session_id = ?",  # noqa: S608
        (session_id,),
    ).fetchone()
    session_data = _rewritable_session_data(raw_session_data)
    if session_data is not None:
        stored_totals = session_data.get("session_metrics")
        totals = SessionMetrics.from_dict(stored_totals) if isinstance(stored_totals, dict) else SessionMetrics()
        totals.count_run(
            _counted_usage(snapshot),
            replacing=None if previous is None else _counted_usage(_decoded_mapping(previous[0]) or {}),
        )
        session_data["session_metrics"] = totals.to_dict()
    # An unreadable row keeps its bytes, so the ledger alone records this run; its update time moves either way.
    connection.execute(
        f"UPDATE {sessions} SET session_data = COALESCE(?, session_data), updated_at = ? WHERE session_id = ?",  # noqa: S608
        (None if session_data is None else json.dumps(session_data), int(time()), session_id),
    )


def _rewritable_session_data(raw_value: object) -> dict[str, Any] | None:
    """Return a row's ``session_data`` object, empty for SQL or JSON null, or None when unreadable or not an object."""
    try:
        value = decode_session_json(raw_value)
    except (TypeError, ValueError):
        return None
    if value is None:
        return {}
    return cast("dict[str, Any]", value) if isinstance(value, dict) else None


def _counted_usage(snapshot: Mapping[str, object]) -> RunMetrics:
    """Return the usage a ledger snapshot records: its token counters, in total and for each model."""
    counted = RunMetrics()
    metrics = snapshot.get("metrics")
    if not isinstance(metrics, dict):
        return counted
    metrics = cast("dict[str, object]", metrics)
    _set_token_counts(counted, metrics)
    details = metrics.get("details")
    models = cast("dict[str, object]", details).get("model") if isinstance(details, dict) else None
    if isinstance(models, list):
        counted.details = {
            "model": [_counted_model(cast("dict[str, object]", entry)) for entry in models if isinstance(entry, dict)],
        }
    return counted


def _counted_model(entry: Mapping[str, object]) -> ModelMetrics:
    model_id, provider = entry.get("id"), entry.get("provider")
    model = ModelMetrics(
        id=model_id if isinstance(model_id, str) else "",
        provider=provider if isinstance(provider, str) else "",
    )
    _set_token_counts(model, entry)
    return model


def _set_token_counts(target: RunMetrics | ModelMetrics, metrics: Mapping[str, object]) -> None:
    # The ledger keeps an invalid counter as a sentinel for its reporter; only real counts add up.
    for name in TOKEN_FIELDS:
        value = metrics.get(name)
        if isinstance(value, int) and not isinstance(value, bool):
            setattr(target, name, value)


def delete_run_subtrees_in_transaction(
    connection: sqlite3.Connection,
    names: TableNames,
    run_ids: Collection[str],
    *,
    session_id: str | None,
) -> set[str]:
    """Delete ``run_ids`` and every run below them in the caller's write transaction; return the deleted ids.

    Team member runs are rows whose ``parent_run_id`` is the team run, so a deleted run takes its whole subtree.
    """
    runs = quote_identifier(names.runs)
    scope, scope_parameters = ("session_id = ? AND ", (session_id,)) if session_id is not None else ("", ())
    found = {run_id for run_id in run_ids if run_id}
    frontier = found
    while frontier:
        frontier = {
            run_id
            for (run_id,) in connection.execute(
                f"SELECT run_id FROM {runs} WHERE {scope}parent_run_id IN (SELECT value FROM json_each(?))",  # noqa: S608
                (*scope_parameters, json.dumps(sorted(frontier))),
            )
        } - found
        found |= frontier
    connection.execute(
        f"DELETE FROM {runs} WHERE {scope}run_id IN (SELECT value FROM json_each(?))",  # noqa: S608
        (*scope_parameters, json.dumps(sorted(found))),
    )
    return found


def _loaded_run_rows(
    connection: sqlite3.Connection,
    names: TableNames,
    summaries: str | None,
    session_id: str,
) -> list[tuple[str, str, str]]:
    """Return ``(run_id, run_type, run_data)`` of the runs a load returns, in log order."""
    runs = quote_identifier(names.runs)
    # A database that has not written a run since it gained a sessions table has no summary rows yet.
    after_latest = f"AND {loads_sql('run', latest_row_index_sql(summaries, 'run'))} " if summaries is not None else ""
    # The descendant step searches a materialized child set, which SQLite indexes automatically, instead of
    # scanning the session once per loaded run.
    return connection.execute(
        "WITH RECURSIVE children(run_id, parent_run_id) AS MATERIALIZED ("  # noqa: S608
        f"SELECT run_id, parent_run_id FROM {runs} WHERE session_id = ? AND parent_run_id IS NOT NULL), "
        f"loaded(run_id) AS (SELECT run.run_id FROM {runs} AS run "
        f"WHERE run.session_id = ? AND run.parent_run_id IS NULL {after_latest}"
        "UNION SELECT children.run_id FROM loaded JOIN children ON children.parent_run_id = loaded.run_id) "
        f"SELECT run.run_id, run.run_type, CAST(run.run_data AS TEXT) FROM {runs} AS run "
        "WHERE run.session_id = ? AND run.run_id IN (SELECT run_id FROM loaded) "
        "ORDER BY run.run_index, run.created_at, run.run_id",
        (session_id, session_id, session_id),
    ).fetchall()


def _folded_message_ids(
    connection: sqlite3.Connection,
    summaries: str | None,
    session_id: str,
    run_ids: list[str],
) -> dict[str, set[str]]:
    """Return, for each of ``run_ids`` that folded mid-turn, the ids of the messages its segments hold."""
    folded: dict[str, set[str]] = {}
    if summaries is None:
        return folded
    for run_id, message_id in connection.execute(
        f"SELECT row.segment_run_id, json_extract(message.value, '$.id') FROM {summaries} AS row, "  # noqa: S608
        "json_each(row.segment, '$.messages') AS message WHERE row.session_id = ? "
        "AND row.segment_run_id IN (SELECT value FROM json_each(?))",
        (session_id, json.dumps(run_ids)),
    ):
        if isinstance(message_id, str):
            folded.setdefault(run_id, set()).add(message_id)
    return folded


def _replay_summary(
    connection: sqlite3.Connection,
    summaries: str | None,
    session_id: str,
    owner_scope: str | None,
) -> ScopeSummary | None:
    """Return the text of the owner scope's latest summary row, which the session replays."""
    if summaries is None or owner_scope is None:
        return None
    latest = connection.execute(
        f"SELECT summary, created_at FROM {summaries} WHERE session_id = ? AND scope_key = ? "  # noqa: S608
        "ORDER BY id DESC LIMIT 1",
        (session_id, owner_scope),
    ).fetchone()
    if latest is None or latest[0] is None:
        return None
    return ScopeSummary(text=latest[0], updated_at=datetime.fromtimestamp(latest[1], UTC))


__all__ = [
    "ConversationStore",
    "delete_run_subtrees_in_transaction",
    "open_conversation_store",
    "persist_to",
]
