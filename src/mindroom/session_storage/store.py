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
from dataclasses import dataclass
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
from mindroom.llm.usage import TOKEN_FIELDS, ModelMetrics, RunMetrics, SessionMetrics
from mindroom.session_storage.codec import run_from_row, run_type
from mindroom.session_storage.lanes import run_session_storage_operation
from mindroom.session_storage.legacy_agno2_runs import decode_session_json
from mindroom.session_storage.schema import schema_sql, table_names
from mindroom.session_storage.types import ConversationSession, SessionFields, session_state_snapshot
from mindroom.usage.storage import project_usage, quote_identifier, usage_upsert_sql

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Collection, Iterable, Iterator, Mapping
    from pathlib import Path

    from mindroom.llm.runs import AnyRunRecord
    from mindroom.session_storage.schema import TableNames
    from mindroom.session_storage.types import Refusal, SaveOutcome, SessionKind
    from mindroom.usage.storage import IndependentUsageKind

# How long an operation waits for another connection's write lock on the database, its migrations included.
BUSY_TIMEOUT_SECONDS = 30.0
_NON_VISIBLE_STATUS_SQL = ", ".join(f"'{status}'" for status in NON_VISIBLE_RUN_STATUS_VALUES)


def connect_conversation_database(path: Path, *, timeout: float = BUSY_TIMEOUT_SECONDS) -> sqlite3.Connection:
    """Open a connection to the conversation database at ``path``, for the store and the migrations that open it.

    ``timeout`` is how long a statement waits for another connection's lock; a caller that would rather skip a
    locked database than wait passes less.
    """
    # No implicit transactions: every operation begins its own, so its reads cannot straddle a commit.
    connection = sqlite3.connect(path, timeout=timeout, isolation_level=None)
    # Deleting a session row cascades to its runs, summary rows, and usage.
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


@contextmanager
def _begun(connection: sqlite3.Connection, *, write: bool) -> Iterator[None]:
    """Run the block as one transaction on ``connection``, committing when it exits normally.

    A write takes the write lock first. A block that fails or is interrupted rolls back at once rather than
    leaving that to a close: a cursor still alive keeps the connection, and with it the write lock, open past its close.
    """
    connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    connection.commit()


def migrate_database(
    path: Path,
    *,
    needed: Callable[[sqlite3.Connection], bool],
    apply: Callable[[sqlite3.Connection], None],
    timeout: float = BUSY_TIMEOUT_SECONDS,
) -> None:
    """Run a one-time migration of the database at ``path`` in one write transaction, when ``needed`` says it is due.

    ``needed`` reads the schema, so a migrated database costs one read. It is asked again once the write lock is
    held, because another opener may have migrated the database while this one waited. A migration that fails or
    is interrupted changes nothing, so the next opener runs it again.
    """
    with closing(connect_conversation_database(path, timeout=timeout)) as connection:
        if needed(connection):
            with _begun(connection, write=True):
                if needed(connection):
                    apply(connection)


@dataclass(frozen=True, slots=True)
class ConversationStore:
    """One conversation database; it holds no connection, so there is nothing to close.

    ``prompt_roles`` are message roles, such as ``system``, that stored runs leave out unless they are paused.
    Tables are created by the first write. Migrations that recognize an older database by a missing summary-row
    or usage table must run before that write.
    """

    path: Path
    names: TableNames
    prompt_roles: frozenset[str] = frozenset()

    @contextmanager
    def transaction(self, *, write: bool) -> Iterator[sqlite3.Connection]:
        """Run one transaction on its own connection, committing when the block exits normally.

        A write takes the write lock first, so rules that read the log before writing see what they change,
        and creates any missing table. A read is one snapshot.
        """
        with closing(connect_conversation_database(self.path)) as connection, _begun(connection, write=write):
            if write:
                for statement in schema_sql(self.names):
                    connection.execute(statement)
            yield connection

    def read_session_fields(self, session_id: str) -> SessionFields | None:
        """Read one session row's metadata and ``session_state`` without its runs, or None when the row is missing."""
        with self.transaction(write=False) as connection:
            row = self._session_row(connection, session_id)
        if row is None:
            return None
        return SessionFields(metadata=decoded_mapping(row[4]), session_state=_session_state(row[3]))

    def load_session(self, session_id: str, kind: SessionKind) -> ConversationSession | None:
        """Load one session with the runs a reply reasons over, or None when it is missing or of another kind.

        The runs are those after the owner scope's latest summary row, every paused, cancelled, or errored run,
        and their member runs, in log order; a run folded mid-turn loads without the messages its segments hold.
        """
        with self.transaction(write=False) as connection:
            loaded = self._read_load(connection, session_id, kind)
        if loaded is None:
            return None
        _, agent_id, team_id, session_data, metadata, created_at, updated_at = loaded.row
        state = _session_state(session_data)
        return ConversationSession(
            session_id=session_id,
            kind=kind,
            owner_id=(team_id if kind == "team" else agent_id) or "",
            metadata=decoded_mapping(metadata),
            session_state=state,
            stored_session_state=copy.deepcopy(state),
            runs=loaded.runs(),
            summary=loaded.summary,
            created_at=created_at,
            updated_at=updated_at,
        )

    def _read_load(self, connection: sqlite3.Connection, session_id: str, kind: SessionKind) -> _Load | None:
        """Read what a load of session ``session_id`` returns in the caller's transaction, its runs still encoded."""
        names = self.names
        tables = table_names(connection)
        row = self._session_row(connection, session_id)
        if row is None or row[0] != kind:
            return None
        stored_kind, agent_id, team_id = row[:3]
        summaries = quote_identifier(names.summaries) if names.summaries in tables else None
        run_rows = _loaded_run_rows(connection, names, summaries, session_id) if names.runs in tables else []
        return _Load(
            row=row,
            run_rows=run_rows,
            folded=_folded_message_ids(connection, summaries, session_id, [run_id for run_id, _, _ in run_rows]),
            summary=_replay_summary(connection, summaries, session_id, owner_scope_key(stored_kind, agent_id, team_id)),
        )

    @contextmanager
    def log_transaction(
        self,
        session_id: str,
        *,
        session: ConversationSession | None = None,
    ) -> Iterator[sqlite3.Connection]:
        """Run one write transaction that changes session ``session_id``'s log: its runs, summary rows, or removed runs.

        Once it commits, ``session``, when given, holds the runs and summary a load returns, read in the same
        transaction, so no other writer's change lands between the write and what the session holds.
        """
        with self.transaction(write=True) as connection:
            yield connection
            loaded = self._read_load(connection, session_id, session.kind) if session is not None else None
        if session is not None:
            _adopt_log(session, loaded)

    def reload_log(self, session: ConversationSession) -> None:
        """Make ``session``'s runs and summary what a load returns now, after another writer changed its log."""
        with self.transaction(write=False) as connection:
            loaded = self._read_load(connection, session.session_id, session.kind)
        _adopt_log(session, loaded)

    def _session_row(self, connection: sqlite3.Connection, session_id: str) -> tuple[Any, ...] | None:
        if self.names.sessions not in table_names(connection):
            return None
        return connection.execute(
            "SELECT session_type, agent_id, team_id, session_data, metadata, created_at, updated_at "  # noqa: S608
            f"FROM {quote_identifier(self.names.sessions)} WHERE session_id = ?",
            (session_id,),
        ).fetchone()

    def ensure_session_row(self, session: ConversationSession) -> None:
        """Create ``session``'s row unless one exists, keeping a row another writer created meanwhile."""
        with self.transaction(write=True) as connection:
            created = _ensure_session_row(connection, self.names, session)
        _record_stored_state(session, created)

    def save_run(
        self,
        session: ConversationSession,
        run: AnyRunRecord,
        *,
        record_usage: bool,
        state: dict[str, Any] | None = None,
    ) -> SaveOutcome:
        """Save one run of ``session`` under the conversation log's insertion rules, creating the session row if needed.

        A run a cut removed, or a member of one, writes no row (``"removed"``). A visible run behind its scope's
        latest summary row, or a member of one, stays as stored (``"frozen"``). A run new to the session, or saved
        visible over a stored paused, cancelled, or errored row, takes the next position after every run and
        summary row. With ``record_usage``, the run's usage replaces its earlier snapshot in the usage ledger,
        which outlives removed runs, and the session totals change by the difference, in the same transaction.
        A frozen run's usage is final and changes neither. The first requester a written run names is stored on a
        session row that has none; no other write of the store names a requester.

        With ``state``, a ``session_state_snapshot`` of the state the run ran with, the run and the state commit in
        one transaction, so a failure leaves neither. The state is written only when the row does not already hold it.

        Once the save commits, ``session.runs`` holds the run as the store does (see ``_reconcile``).
        """
        (saved,) = self._save_encoded(session, [_encoded(run)], record_usage=record_usage, state=state)
        return "written" if isinstance(saved.row, _WrittenRow) else saved.row

    def save_runs(
        self,
        session: ConversationSession,
        runs: Iterable[AnyRunRecord],
        *,
        state: dict[str, Any] | None = None,
    ) -> None:
        """Write conversation-only changes to runs of ``session`` in one transaction, then reconcile its copies.

        Conversation rewrites leave the usage ledger and session totals untouched. With ``state``, a
        ``session_state_snapshot``, the session state commits in the same transaction (see ``save_run``). Once the
        transaction commits, ``session.runs`` holds each run as the store does (see ``_reconcile``), so a removed run
        leaves the session and a frozen one keeps the session's copy. Callers therefore edit a copy and hand the copy
        here: a refused save must leave the session's copy as stored. A call with neither runs nor ``state`` writes
        nothing.
        """
        runs = list(runs)
        if not runs and state is None:
            return
        loaded = {id(existing) for existing in session.runs}
        if any(id(run) in loaded for run in runs):
            msg = "save_runs received a run object loaded from the session; edit a copy instead"
            raise ValueError(msg)
        self._save_encoded(session, [_encoded(run) for run in runs], record_usage=False, state=state)

    def _save_encoded(
        self,
        session: ConversationSession,
        runs: list[tuple[dict[str, Any], SessionKind]],
        *,
        record_usage: bool,
        state: dict[str, Any] | None,
    ) -> list[_SavedRun]:
        """Save ``runs``, each a stored dict and its row type, and ``state`` in one transaction, then reconcile."""
        with self.transaction(write=True) as connection:
            created = _ensure_session_row(connection, self.names, session)
            saved = [
                self._save_run(connection, session.session_id, data, row_type=row_type, record_usage=record_usage)
                for data, row_type in runs
            ]
            stored = self._write_state(connection, session, created, state)
        _record_stored_state(session, stored)
        _reconcile(session, saved)
        return saved

    def save_session_state(self, session: ConversationSession, state: dict[str, Any]) -> None:
        """Write ``state`` as the session's ``session_state``, creating the row if needed, and leave the rest alone.

        ``state`` is a ``session_state_snapshot`` of the run's state, which a caller outside the database's lane takes
        before awaiting it, as the run's tools change the live state in place meanwhile. It is recorded as
        ``session.stored_session_state``, so the caller must not change it.
        """
        with self.transaction(write=True) as connection:
            _ensure_session_row(connection, self.names, session)
            _write_session_state(connection, self.names, session.session_id, state)
        _record_stored_state(session, state)

    def _write_state(
        self,
        connection: sqlite3.Connection,
        session: ConversationSession,
        created: dict[str, Any] | None,
        state: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """Write ``state`` into ``session``'s row in the caller's write transaction unless the row already holds it.

        The row holds ``created`` when this transaction created it, and otherwise what ``session`` last knew it to
        hold, which every committed write of the field records. Returns the state this transaction left in the row,
        for the caller to record once it commits, or None when it left the row's state alone.
        """
        if state is None:
            return created
        held = session.stored_session_state if created is None else created
        if state == held:
            return created
        _write_session_state(connection, self.names, session.session_id, state)
        return state

    def update_metadata(
        self,
        session: ConversationSession,
        update: Callable[[dict[str, Any] | None], dict[str, Any] | None],
    ) -> None:
        """Apply ``update`` to the metadata ``session``'s row holds, under the write lock.

        ``update`` receives a private copy of the stored metadata, or of the session's own when no row exists yet,
        and returns the metadata to store, so a change never restores what another writer dropped. Only a change
        writes, creating the row if needed. Once it commits, ``session.metadata`` is what the row holds.
        """
        sessions = quote_identifier(self.names.sessions)
        created = None
        with self.transaction(write=True) as connection:
            stored = connection.execute(
                f"SELECT metadata FROM {sessions} WHERE session_id = ?",  # noqa: S608
                (session.session_id,),
            ).fetchone()
            current = decoded_mapping(stored[0]) if stored is not None else session.metadata
            updated = update(copy.deepcopy(current))
            if updated != current:
                created = _ensure_session_row(connection, self.names, session)
                connection.execute(
                    f"UPDATE {sessions} SET metadata = ?, updated_at = ? WHERE session_id = ?",  # noqa: S608
                    (json.dumps(updated), int(time()), session.session_id),
                )
        _record_stored_state(session, created)
        session.metadata = updated

    def delete_run_subtrees(
        self,
        run_ids: Collection[str],
        *,
        session_id: str | None = None,
        session: ConversationSession | None = None,
    ) -> set[str]:
        """Delete ``run_ids`` with every member run below them, within session ``session_id`` when given.

        Returns the deleted ids. Once the delete commits, ``session``, a loaded copy of that session, holds none of
        them, nor any run below them.
        """
        with self.transaction(write=True) as connection:
            deleted = delete_run_subtrees_in_transaction(connection, self.names, run_ids, session_id=session_id)
        if session is not None:
            session.runs = runs_without(session.runs, deleted)
        return deleted

    def get_run(self, run_id: str) -> AnyRunRecord | None:
        """Return one stored run, in whatever session, as stored."""
        return next(iter(self._stored_runs("run_id = ?", run_id)), None)

    def list_runs(self, session_id: str) -> list[AnyRunRecord]:
        """Return the session's whole run log as stored, member runs included, in log order."""
        return self._stored_runs("session_id = ? ORDER BY run_index, created_at, run_id", session_id)

    def _stored_runs(self, condition: str, value: str) -> list[AnyRunRecord]:
        """Decode the stored runs whose rows match ``condition`` on ``value``, skipping rows of other run types."""
        with self.transaction(write=False) as connection:
            if self.names.runs not in table_names(connection):
                return []
            rows = connection.execute(
                f"SELECT run_type, CAST(run_data AS TEXT) FROM {quote_identifier(self.names.runs)} WHERE {condition}",  # noqa: S608
                (value,),
            ).fetchall()
        return [run for run in (run_from_row(*row) for row in rows) if run is not None]

    def list_updated_sessions(self, kind: SessionKind, owner_id: str, *, since: int) -> list[tuple[str, int]]:
        """Return ``(session_id, updated_at)`` of ``owner_id``'s sessions updated after ``since``, oldest first.

        A session's update time moves when its row is written: a state save, a metadata change, or a run save that
        records usage.
        """
        owner_column = "team_id" if kind == "team" else "agent_id"
        with self.transaction(write=False) as connection:
            if self.names.sessions not in table_names(connection):
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
    ) -> _SavedRun:
        """Save one run from its stored dict (``record.to_dict()``), under the insertion rules."""
        if data["status"] != RunStatus.paused.value and self.prompt_roles:
            # Prompts are rebuilt from config on every request; a paused run keeps them until it continues.
            data["messages"] = [message for message in data["messages"] if message["role"] not in self.prompt_roles]
        row = _write_run_row(connection, self.names, data, row_type=row_type, session_id=session_id)
        saved = _SavedRun(data["run_id"], row_type, row)
        if isinstance(row, _WrittenRow):
            _adopt_requester(connection, self.names, session_id, data.get("user_id"))
        if not record_usage or row == "frozen":
            return saved
        now = int(time())
        created_at, updated_at = (
            (row.created_at, row.updated_at) if isinstance(row, _WrittenRow) else (data.get("created_at") or now, now)
        )
        snapshot = project_usage({**data, "created_at": created_at, "updated_at": updated_at})
        _record_run_usage(connection, self.names, session_id, data["run_id"], snapshot)
        return saved


@dataclass(frozen=True, slots=True)
class _Load:
    """What one load of a session read: its row, its runs as stored, the messages its folds hold, and its summary."""

    row: tuple[Any, ...]
    run_rows: list[tuple[str, str, str]]
    folded: dict[str, set[str]]
    summary: ScopeSummary | None

    def runs(self) -> list[AnyRunRecord]:
        """Decode the runs, each folded run without the messages its segments hold."""
        runs: list[AnyRunRecord] = []
        for run_id, stored_type, run_data in self.run_rows:
            run = run_from_row(stored_type, run_data)
            if run is None:
                continue
            if run_id in self.folded:
                run.messages = [message for message in run.messages if message.id not in self.folded[run_id]]
            runs.append(run)
        return runs


def _encoded(run: AnyRunRecord) -> tuple[dict[str, Any], SessionKind]:
    """Return ``run``'s stored dict and the ``run_type`` of its row."""
    return run.to_dict(), run_type(run)


def _adopt_log(session: ConversationSession, loaded: _Load | None) -> None:
    """Make ``session``'s runs and summary those of ``loaded``, or none when the session is no longer stored."""
    session.runs = loaded.runs() if loaded is not None else []
    session.summary = loaded.summary if loaded is not None else None


@dataclass(frozen=True, slots=True)
class _WrittenRow:
    """One run row as an upsert left it: its timestamps, its ``run_data`` text, and whether it now ends the log."""

    created_at: int
    updated_at: int
    run_data: str
    ends_log: bool


@dataclass(frozen=True, slots=True)
class _SavedRun:
    """One run save: the run, its row type, and the row it wrote or why it wrote none."""

    run_id: str
    row_type: SessionKind
    row: _WrittenRow | Refusal


def _reconcile(session: ConversationSession, saved: Iterable[_SavedRun]) -> None:
    """Make ``session``'s copies of the saved runs what the store holds.

    A written run's copy is its row decoded afresh, never the record the caller handed over: it replaces the
    session's copy in place, or goes last when the row took the log's next position (a new run, or one saved visible
    over its paused, cancelled, or errored row). A written run the session holds no copy of also goes last, even
    when its row kept an earlier place, so only then can the session's order differ from a load's. Member runs keep
    their own copies. A removed run leaves the session with every run below it. A frozen run's row did not change,
    so neither does the session's copy, nor its absence when the session holds none.
    """
    runs = list(session.runs)
    for save in saved:
        if save.row == "removed":
            runs = runs_without(runs, [save.run_id])
        elif isinstance(save.row, _WrittenRow):
            # Unlike a load, this keeps any message a mid-turn fold's segment holds: a fold rewrites its run's live
            # messages when the segment row commits, so a record handed over after the fold holds none of them.
            stored = run_from_row(save.row_type, save.row.run_data)
            assert stored is not None
            place = next((index for index, run in enumerate(runs) if run.run_id == save.run_id), None)
            if place is None or save.row.ends_log:
                runs = [*(run for run in runs if run.run_id != save.run_id), stored]
            else:
                runs[place] = stored
    # A new list, so a reader of the old one is unaffected. Only the store assigns a session's runs and summary, and
    # async callers run its writes in the database's lane one at a time, so no write's assignment overtakes another's.
    session.runs = runs


def runs_without(runs: Iterable[AnyRunRecord], run_ids: Iterable[str]) -> list[AnyRunRecord]:
    """Return ``runs`` minus ``run_ids`` and every run descending from them through ``parent_run_id``."""
    run_list = list(runs)
    removed = {run_id for run_id in run_ids if run_id}
    while below := {run.run_id for run in run_list if run.parent_run_id and run.parent_run_id in removed} - removed:
        removed |= below
    return [run for run in run_list if run.run_id not in removed and run.parent_run_id not in removed]


def persist_to(store: ConversationStore, session: ConversationSession) -> Callable[[AnyRunRecord], Awaitable[None]]:
    """Return the callback a run hands its terminal record to: save it to ``store`` with its usage, in the database's lane.

    The record and ``session.session_state``, which the run's tools change in place, are copied on the caller's task
    before the lane is awaited, so what lands is what was handed over. The state is written back, in the record's
    own transaction, when it differs from ``session.stored_session_state``, which every write of the field records,
    so a turn whose runs or checkpoints change the state and restore it stores it restored.
    The save reconciles ``session.runs`` with what it stored (see ``_reconcile``) in the lane, where the session's
    saves run one at a time; it completes even when the caller is cancelled, as the lane's operations do.
    """

    async def persist(record: AnyRunRecord) -> None:
        encoded = _encoded(record)
        await write_with_session_state(
            store,
            session,
            lambda opened, state: opened._save_encoded(session, [encoded], record_usage=True, state=state),
        )

    return persist


async def write_with_session_state[Result](
    store: ConversationStore,
    session: ConversationSession,
    write: Callable[[ConversationStore, dict[str, Any]], Result],
) -> Result:
    """Run ``write`` in ``store``'s lane with a copy of ``session``'s state taken now, on the caller's task.

    The run's tools change the live state in place while the lane is busy, so the copy is taken before the lane is
    awaited. ``write`` hands it to the run save it makes (``state=``), which commits it with the run.
    """
    state = session_state_snapshot(session.session_state)
    return await run_session_storage_operation(lambda: store, lambda opened: write(opened, state))


def decoded_mapping(raw_value: object, *, null: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Return a stored JSON object column, ``null`` for SQL or JSON null, or None when unreadable or not an object.

    An odd row therefore never fails a load.
    """
    try:
        value = decode_session_json(raw_value)
    except (TypeError, ValueError):
        return None
    if value is None:
        return null
    return cast("dict[str, Any]", value) if isinstance(value, dict) else None


def _session_state(raw_session_data: object) -> dict[str, Any]:
    """Return the ``session_state`` object inside a row's ``session_data``, or an empty one."""
    state = (decoded_mapping(raw_session_data) or {}).get("session_state")
    return state if isinstance(state, dict) else {}


def _ensure_session_row(
    connection: sqlite3.Connection,
    names: TableNames,
    session: ConversationSession,
) -> dict[str, Any] | None:
    """Create ``session``'s row unless one exists, keeping a row another writer created meanwhile.

    Returns the copy of ``session.session_state`` a created row holds, for the caller to record as
    ``session.stored_session_state`` once its transaction commits, or None when the row existed.
    """
    owner_column = "team_id" if session.kind == "team" else "agent_id"
    # One encode reads the live state, as ``session_state_snapshot`` does, and the created row's copy decodes from it.
    session_data = json.dumps({"session_state": session.session_state})
    cursor = connection.execute(
        f"INSERT INTO {quote_identifier(names.sessions)} (session_id, session_type, {owner_column}, "  # noqa: S608
        "session_data, metadata, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(session_id) DO NOTHING",
        (
            session.session_id,
            session.kind,
            session.owner_id,
            session_data,
            # No metadata is stored as the JSON text ``null``, never SQL NULL, kept as main behaves.
            json.dumps(session.metadata),
            session.created_at,
            session.created_at,
        ),
    )
    return json.loads(session_data)["session_state"] if cursor.rowcount else None


def _adopt_requester(
    connection: sqlite3.Connection,
    names: TableNames,
    session_id: str,
    requester_id: str | None,
) -> None:
    """Give a session row that has no requester the requester of a run saved into it; no other write names one.

    A conversation's first run names its requester, but the row is often created before that run is saved, by a
    write that knows no requester, such as a fold or a helper's usage, so a created row has none. The first requester
    a run names stays.
    """
    if requester_id:
        connection.execute(
            f"UPDATE {quote_identifier(names.sessions)} SET user_id = ? WHERE session_id = ? AND user_id IS NULL",  # noqa: S608
            (requester_id, session_id),
        )


def _record_stored_state(session: ConversationSession, state: dict[str, Any] | None) -> None:
    """Record ``state``, the session state a committed write left in the row, as ``session.stored_session_state``."""
    if state is not None:
        session.stored_session_state = state


def _write_session_state(
    connection: sqlite3.Connection,
    names: TableNames,
    session_id: str,
    state: dict[str, Any],
) -> None:
    """Replace the ``session_state`` of an existing session row, leaving the rest of its ``session_data`` alone."""
    sessions = quote_identifier(names.sessions)
    (stored,) = connection.execute(
        f"SELECT session_data FROM {sessions} WHERE session_id = ?",  # noqa: S608
        (session_id,),
    ).fetchone()
    connection.execute(
        f"UPDATE {sessions} SET session_data = ?, updated_at = ? WHERE session_id = ?",  # noqa: S608
        (json.dumps({**(decoded_mapping(stored) or {}), "session_state": state}), int(time()), session_id),
    )


def _write_run_row(
    connection: sqlite3.Connection,
    names: TableNames,
    data: dict[str, Any],
    *,
    row_type: str,
    session_id: str,
) -> _WrittenRow | Refusal:
    """Upsert one run row under the conversation log's rules; return the row as written, or why none was."""
    run_id, parent_run_id = data["run_id"], data.get("parent_run_id")
    runs, summaries, removed = (quote_identifier(name) for name in (names.runs, names.summaries, names.removed_runs))
    if connection.execute(f"SELECT 1 FROM {removed} WHERE run_id IN (?, ?)", (run_id, parent_run_id)).fetchone():  # noqa: S608
        return "removed"
    if _is_folded(connection, runs, summaries, run_id, parent_run_id):
        return "frozen"
    (next_index,) = connection.execute(
        f"SELECT MAX(COALESCE((SELECT MAX(run_index) FROM {runs} WHERE session_id = ?), -1), "  # noqa: S608
        f"COALESCE((SELECT MAX(after_run_index) FROM {summaries} WHERE session_id = ?), -1)) + 1",
        (session_id, session_id),
    ).fetchone()
    now = int(time())
    run_data = json.dumps(data)
    # A run saved visible over its stored paused, cancelled, or errored row was finished after the turns it waited
    # for, so it moves after them; any other update keeps the run's place.
    created_at, updated_at, run_index = connection.execute(
        f"INSERT INTO {runs} AS run (run_id, session_id, run_type, agent_id, team_id, user_id, parent_run_id, "  # noqa: S608
        "status, run_index, run_data, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(run_id) DO UPDATE SET status = excluded.status, run_data = excluded.run_data, "
        "user_id = excluded.user_id, parent_run_id = excluded.parent_run_id, updated_at = excluded.updated_at, "
        f"run_index = CASE WHEN run.parent_run_id IS NULL AND run.status IN ({_NON_VISIBLE_STATUS_SQL}) "
        f"AND excluded.parent_run_id IS NULL AND excluded.status NOT IN ({_NON_VISIBLE_STATUS_SQL}) "
        "THEN excluded.run_index ELSE COALESCE(run.run_index, excluded.run_index) END "
        "RETURNING created_at, updated_at, run_index",
        (
            run_id,
            session_id,
            row_type,
            data.get("agent_id"),
            data.get("team_id"),
            data.get("user_id"),
            parent_run_id,
            data["status"],
            next_index,
            run_data,
            data.get("created_at") or now,
            now,
        ),
    ).fetchone()
    return _WrittenRow(created_at, updated_at, run_data, ends_log=run_index == next_index)


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


# TODO(simplify): the session totals in `session_data` repeat what the usage ledger's run snapshots record.
# Report undated totals from the ledger and stop writing them, once a migration keeps pre-ledger history.
# Operators would notice in admin and private usage totals, and stored data.
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
    session_data = decoded_mapping(raw_session_data, null={})
    if session_data is not None:
        stored_totals = session_data.get("session_metrics")
        totals = SessionMetrics.from_dict(stored_totals) if isinstance(stored_totals, dict) else SessionMetrics()
        totals.count_run(
            _counted_usage(snapshot),
            replacing=None if previous is None else _counted_usage(decoded_mapping(previous[0]) or {}),
        )
        session_data["session_metrics"] = totals.to_dict()
    # An unreadable row keeps its bytes, so the ledger alone records this run; its update time moves either way.
    connection.execute(
        f"UPDATE {sessions} SET session_data = COALESCE(?, session_data), updated_at = ? WHERE session_id = ?",  # noqa: S608
        (None if session_data is None else json.dumps(session_data), int(time()), session_id),
    )


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
    "BUSY_TIMEOUT_SECONDS",
    "ConversationStore",
    "connect_conversation_database",
    "decoded_mapping",
    "delete_run_subtrees_in_transaction",
    "migrate_database",
    "persist_to",
    "runs_without",
    "write_with_session_state",
]
