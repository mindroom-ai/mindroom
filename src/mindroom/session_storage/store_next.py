"""The conversation store on storage v2: sessions, their runs and messages, summary rows, and the usage ledger.

Every operation runs in one transaction on its own short-lived connection and leaves the journal mode alone, so
databases keep SQLite's default rollback journal. A write takes the write lock before it reads anything its rules
depend on, and a load reads the session row, its runs, and its summary from one snapshot. Opening a database creates
every table it lacks, so no operation runs DDL.

A run is one ``*_log_runs`` row plus one ``*_log_messages`` row per stored message. Every reader gets a run's replay
view: its message rows that no mid-turn fold took, in order. A run's ``seq`` is its place in the log, handed out by
the meta row's counter, which only grows, so a deleted run's number is never handed out again.

It replaces the Agno-layout store in ``store.py`` at the cutover.
"""

from __future__ import annotations

import copy
import json
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from time import time
from typing import TYPE_CHECKING, Any, Literal, NamedTuple, cast

from mindroom.constants import (
    MATRIX_EVENT_ID_METADATA_KEY,
    MATRIX_RESPONSE_EVENT_ID_METADATA_KEY,
    MATRIX_SEEN_EVENT_IDS_METADATA_KEY,
    MATRIX_SOURCE_EVENT_IDS_METADATA_KEY,
    MATRIX_SOURCE_EVENT_REVISIONS_METADATA_KEY,
    MATRIX_TURN_DISCOVERY_EVENT_IDS_METADATA_KEY,
)
from mindroom.history.summary_rows_schema import NON_VISIBLE_RUN_STATUS_VALUES, owner_scope_key
from mindroom.history.types import ScopeSummary
from mindroom.llm.runs import RunStatus, TeamRunRecord
from mindroom.llm.usage import ModelMetrics, RunMetrics, SessionMetrics, TokenCounts
from mindroom.session_storage.codec import RunColumns, run_columns, run_record, run_type
from mindroom.session_storage.lanes import run_session_storage_operation
from mindroom.session_storage.legacy_agno2_runs import decode_session_json
from mindroom.session_storage.schema import TableNames
from mindroom.session_storage.types import ConversationSession, SessionFields, session_state_snapshot
from mindroom.usage.storage import project_usage, quote_identifier, usage_upsert_sql

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Collection, Iterable, Iterator, Mapping, Sequence
    from pathlib import Path

    from mindroom.llm.messages import Message
    from mindroom.llm.runs import AnyRunRecord, RunRecord
    from mindroom.session_storage.types import SessionKind
    from mindroom.usage.storage import IndependentUsageKind

# How long an operation waits for another connection's write lock on the database, its migrations included.
BUSY_TIMEOUT_SECONDS = 30.0
_NON_VISIBLE_STATUS_SQL = ", ".join(f"'{status}'" for status in NON_VISIBLE_RUN_STATUS_VALUES)
_RUN_COLUMNS = (
    "run.run_id, run.session_id, run.parent_run_id, run.kind, run.scope_key, run.status, run.record, run.tools, "
    "run.requirements, run.metadata"
)
# The run-row columns every full save writes; a first write also sets the ids and ``created_at``.
_WRITTEN_COLUMNS = (
    "parent_run_id",
    "kind",
    "scope_key",
    "status",
    "seq",
    "updated_at",
    "metadata",
    "record",
    "requirements",
    "tools",
)

# What a full save did: wrote the run, wrote nothing because a cut removed it, or wrote nothing because it is final.
type SaveOutcome = Literal["written", "removed", "final"]


def connect_conversation_database(path: Path, *, timeout: float = BUSY_TIMEOUT_SECONDS) -> sqlite3.Connection:
    """Open a connection to the conversation database at ``path``, for the store and the migrations that open it.

    ``timeout`` is how long a statement waits for another connection's lock; a caller that would rather skip a
    locked database than wait passes less.
    """
    # No implicit transactions: every operation begins its own, so its reads cannot straddle a commit.
    connection = sqlite3.connect(path, timeout=timeout, isolation_level=None)
    # Deleting a session row cascades to its runs, summary rows, and usage, and deleting a run to its messages.
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

    Stored runs never hold a message replayed from history, and leave out their system messages unless they are paused.
    """

    path: Path

    @property
    def names(self) -> TableNames:
        """The names of the database ``path`` holds."""
        return TableNames.of_database(self.path)

    @contextmanager
    def transaction(self, *, write: bool) -> Iterator[sqlite3.Connection]:
        """Run one transaction on its own connection, committing when the block exits normally.

        A write takes the write lock first, so rules that read the log before writing see what they change. A read
        is one snapshot.
        """
        with closing(connect_conversation_database(self.path)) as connection, _begun(connection, write=write):
            yield connection

    def read_session_fields(self, session_id: str) -> SessionFields | None:
        """Read one session row's metadata and ``session_state`` without its runs, or None when the row is missing."""
        with self.transaction(write=False) as connection:
            row = self._session_row(connection, session_id)
        if row is None:
            return None
        return SessionFields(metadata=decoded_mapping(row[4]), session_state=_session_state(row[3]))

    def has_session(self, session_id: str, kind: SessionKind) -> bool:
        """Return whether a ``kind`` session ``session_id`` is stored, the condition under which a load finds it."""
        with self.transaction(write=False) as connection:
            return self._session_row(connection, session_id, kind) is not None

    def load_session(self, session_id: str, kind: SessionKind) -> ConversationSession | None:
        """Load one session with the runs a reply reasons over, or None when it is missing or of another kind.

        The runs are every top-level run its scope's latest summary row does not cover, every paused, cancelled, or
        errored one wherever it sits, and the member runs of those, in ``seq`` order, each in its replay view.
        """
        with self.transaction(write=False) as connection:
            loaded = self._read_load(connection, session_id, kind)
        if loaded is None:
            return None
        _, agent_id, team_id, session_data, metadata, created_at = loaded.row
        state = _session_state(session_data)
        return ConversationSession(
            session_id=session_id,
            kind=kind,
            owner_id=(team_id if kind == "team" else agent_id) or "",
            metadata=decoded_mapping(metadata),
            session_state=state,
            stored_session_state=copy.deepcopy(state),
            runs=loaded.runs,
            summary=loaded.summary,
            created_at=created_at,
        )

    def _read_load(self, connection: sqlite3.Connection, session_id: str, kind: SessionKind) -> _Load | None:
        """Read what a load of session ``session_id`` returns, in the caller's transaction."""
        row = self._session_row(connection, session_id, kind)
        if row is None:
            return None
        stored_kind, agent_id, team_id = row[:3]
        names = self.names
        runs, summaries = quote_identifier(names.log_runs), quote_identifier(names.log_summaries)
        loaded = (
            f"run.session_id = ? AND ({_loads_sql('run', summaries)} OR run.parent_run_id IN "  # noqa: S608
            f"(SELECT top.run_id FROM {runs} AS top WHERE top.session_id = ? AND {_loads_sql('top', summaries)}))"
        )
        return _Load(
            row=row,
            runs=_read_runs(connection, names, loaded, (session_id, session_id)),
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

    def _session_row(
        self,
        connection: sqlite3.Connection,
        session_id: str,
        kind: SessionKind | None = None,
    ) -> tuple[Any, ...] | None:
        """Read session ``session_id``'s row, or None when it is missing or, given ``kind``, of another kind."""
        row = connection.execute(
            "SELECT session_type, agent_id, team_id, session_data, metadata, created_at "  # noqa: S608
            f"FROM {quote_identifier(self.names.sessions)} WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return None if row is None or (kind is not None and row[0] != kind) else row

    def save_run(
        self,
        session: ConversationSession,
        run: AnyRunRecord,
        *,
        record_usage: bool,
        state: dict[str, Any] | None = None,
    ) -> SaveOutcome:
        """Save ``run`` of ``session`` whole, creating the session row if needed, unless a cut removed it or it is final.

        A run a cut removed, or a member of one, writes nothing (``"removed"``). A run whose stored status is
        visible, or a member whose team run's stored status is, is final and writes nothing (``"final"``): a stale
        paused or cancelled copy never makes a finished run pending again. Otherwise the run's row and its replay
        view become the record's (``"written"``); a new run, or a top-level run saved visible over its paused,
        cancelled, or errored row, takes the next ``seq``, so it replays after the turns it waited for. The first
        requester a written run names is stored on a session row that has none.

        With ``record_usage``, the run's usage snapshot replaces its earlier one in the usage ledger, which outlives
        removed runs, and the session totals move by the difference; a final run's usage is final and changes
        neither. With ``state``, a ``session_state_snapshot`` of the state the run ran with, the run and the state
        commit together, the state only when the row does not already hold it. Once the save commits,
        ``session.runs`` holds the run as the store does (see ``_mirror``).
        """
        return self._save(
            session,
            run.to_dict(),
            run_type(run),
            interrupted=False,
            record_usage=record_usage,
            state=state,
        )

    def save_interrupted_record(
        self,
        session: ConversationSession,
        record: AnyRunRecord,
        *,
        state: dict[str, Any] | None = None,
    ) -> Literal["written", "removed"]:
        """Save an interrupted turn's record as ``save_run`` does, over a final run too, and without its usage.

        It is what the user saw or heard of a turn whose failure followed its terminal save, so it replaces the
        answer the run stored; a run that was visible keeps its ``seq``, so replay order does not change.
        """
        outcome = self._save(
            session,
            record.to_dict(),
            run_type(record),
            interrupted=True,
            record_usage=False,
            state=state,
        )
        return cast("Literal['written', 'removed']", outcome)

    def _save(
        self,
        session: ConversationSession,
        data: dict[str, Any],
        kind: SessionKind,
        *,
        interrupted: bool,
        record_usage: bool,
        state: dict[str, Any] | None,
    ) -> SaveOutcome:
        """Save one run from its stored dict and ``state`` in one transaction, then mirror it in ``session``."""
        with self.transaction(write=True) as connection:
            created = ensure_session_row(connection, self.names, session)
            saved = _write_run(connection, self.names, session.session_id, data, kind, interrupted=interrupted)
            if record_usage and saved.outcome != "final":
                snapshot = project_usage({**data, "created_at": saved.created_at, "updated_at": saved.updated_at})
                _record_run_usage(connection, self.names, session.session_id, data["run_id"], snapshot)
            stored = self._write_state(connection, session, created, state)
        _record_stored_state(session, stored)
        _mirror(session, saved)
        return saved.outcome

    def patch_run_metadata(
        self,
        session_id: str,
        run_id: str,
        changes: Mapping[str, Any],
        *,
        session: ConversationSession | None = None,
    ) -> bool:
        """Set ``changes``' top-level keys in the metadata of stored run ``run_id``, final or not, and keep the rest.

        When the run is a visible top-level run a summary covers, the event ids its metadata now names join that
        summary row's event sets, so redacting a reply linked after the summary still finds the summary. Returns
        whether the run is stored. ``session``, when given, then holds a changed copy of the run in a new list; a
        loaded run object is never changed in place, since loaded runs are shared.
        """
        runs = quote_identifier(self.names.log_runs)
        with self.transaction(write=True) as connection:
            row = connection.execute(
                f"SELECT parent_run_id, scope_key, status, seq, metadata FROM {runs} "  # noqa: S608
                "WHERE session_id = ? AND run_id = ?",
                (session_id, run_id),
            ).fetchone()
            if row is None:
                return False
            parent_run_id, scope_key, status, seq, stored = row
            metadata = {**(decoded_mapping(stored) or {}), **changes}
            encoded = json.dumps(metadata)
            connection.execute(
                f"UPDATE {runs} SET metadata = ?, updated_at = ? WHERE run_id = ?",  # noqa: S608
                (encoded, int(time()), run_id),
            )
            if parent_run_id is None and status not in NON_VISIBLE_RUN_STATUS_VALUES:
                _join_covering_summary(connection, self.names, session_id, scope_key, seq, metadata)
        if session is not None:
            session.runs = [
                replace(run, metadata=json.loads(encoded)) if run.run_id == run_id else run for run in session.runs
            ]
        return True

    def rewrite_run_messages(
        self,
        session_id: str,
        kind: SessionKind,
        edit: Callable[[list[AnyRunRecord]], list[AnyRunRecord]],
        *,
        session: ConversationSession | None = None,
    ) -> None:
        """Apply ``edit`` to the runs a load of the session returns, under the write lock, and store their messages.

        ``edit`` gets fresh copies and returns the runs whose messages to store; only runs the load held store, and
        only their messages, under the save's message rules, so no other writer's status, metadata, or messages are
        lost and no run a summary covers changes. Once it commits, ``session``, when given, holds what a load returns.
        """
        with self.log_transaction(session_id, session=session) as connection:
            loaded = self._read_load(connection, session_id, kind)
            runs = loaded.runs if loaded is not None else []
            statuses = {run.run_id: run.status for run in runs}
            for run in edit(runs):
                if run.run_id in statuses:
                    _set_visible_messages(
                        connection,
                        self.names,
                        run.run_id,
                        [message.to_dict() for message in run.messages],
                        keep_system=statuses[run.run_id] is RunStatus.paused,
                    )

    def delete_runs(
        self,
        run_ids: Collection[str],
        *,
        session_id: str,
        session: ConversationSession | None = None,
    ) -> set[str]:
        """Delete session ``session_id``'s runs ``run_ids`` with their member runs and messages; return the deleted ids.

        It records no removed run and keeps every summary row, so it suits discarding a run, not cutting history.
        Once it commits, ``session``, when given, holds none of the runs nor their members.
        """
        with self.transaction(write=True) as connection:
            deleted = delete_runs_in_transaction(connection, self.names, run_ids, session_id=session_id)
        if session is not None:
            session.runs = runs_without(session.runs, run_ids)
        return deleted

    def get_run(self, run_id: str) -> AnyRunRecord | None:
        """Return one stored run, in whatever session, in its replay view; a paused team run comes with its members."""
        with self.transaction(write=False) as connection:
            return next(iter(_read_runs(connection, self.names, "run.run_id = ?", (run_id,))), None)

    def list_runs(self, session_id: str) -> list[AnyRunRecord]:
        """Return every stored run of the session, member runs included, in ``seq`` order, each in its replay view."""
        with self.transaction(write=False) as connection:
            return _read_runs(connection, self.names, "run.session_id = ?", (session_id,))

    def save_session_state(self, session: ConversationSession, state: dict[str, Any]) -> None:
        """Write ``state`` as the session's ``session_state``, creating the row if needed, and leave the rest alone.

        ``state`` is a ``session_state_snapshot`` of the run's state, which a caller outside the database's lane takes
        before awaiting it, as the run's tools change the live state in place meanwhile. It is recorded as
        ``session.stored_session_state``, so the caller must not change it.
        """
        with self.transaction(write=True) as connection:
            ensure_session_row(connection, self.names, session)
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
                created = ensure_session_row(connection, self.names, session)
                connection.execute(
                    f"UPDATE {sessions} SET metadata = ?, updated_at = ? WHERE session_id = ?",  # noqa: S608
                    (json.dumps(updated), int(time()), session.session_id),
                )
        _record_stored_state(session, created)
        session.metadata = updated

    def list_updated_sessions(self, agent_id: str, *, since: int) -> list[tuple[str, int]]:
        """Return ``(session_id, updated_at)`` of agent ``agent_id``'s sessions updated after ``since``, oldest first.

        A session's update time moves when its row is written: a state save, a metadata change, or a run save that
        records its usage, as a run's terminal save through ``persist_to`` does.
        """
        with self.transaction(write=False) as connection:
            return connection.execute(
                "SELECT session_id, COALESCE(updated_at, created_at) AS updated "  # noqa: S608
                f"FROM {quote_identifier(self.names.sessions)} WHERE session_type = 'agent' AND agent_id = ? "
                "AND COALESCE(updated_at, created_at) > ? ORDER BY updated, session_id",
                (agent_id, since),
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
                ensure_session_row(connection, self.names, initial_session)
            connection.execute(usage_upsert_sql(self.names.sessions), (session_id, usage_id, json.dumps(snapshot)))


@dataclass(frozen=True, slots=True)
class _Load:
    """What one load of a session read: its row, its runs, and its summary."""

    row: tuple[Any, ...]
    runs: list[AnyRunRecord]
    summary: ScopeSummary | None


def _adopt_log(session: ConversationSession, loaded: _Load | None) -> None:
    """Make ``session``'s runs and summary those of ``loaded``, or none when the session is no longer stored."""
    session.runs = loaded.runs if loaded is not None else []
    session.summary = loaded.summary if loaded is not None else None


@dataclass(frozen=True, slots=True)
class _Saved:
    """One full save: its outcome and, for a written run, its row's timestamps, its move, and the run read back."""

    run_id: str
    outcome: SaveOutcome
    created_at: int = 0
    updated_at: int = 0
    moved: bool = False
    stored: AnyRunRecord | None = None


class _StoredRow(NamedTuple):
    """What a save reads of a run's stored row, or of its team run's, before writing."""

    parent_run_id: str | None
    status: str
    seq: int
    created_at: int


class _RunRow(NamedTuple):
    """One run row as read, its message rows aside."""

    run_id: str
    session_id: str
    parent_run_id: str | None
    columns: RunColumns


def _mirror(session: ConversationSession, saved: _Saved) -> None:
    """Make ``session``'s copy of a saved run what the store holds, in a new list.

    A removed run leaves with its member runs. A written run's copy is its row read back, never the record handed
    over: it replaces the session's copy, or goes last when the run took the next ``seq`` or the session held no copy.
    A final run's copy, or its absence, stays.
    """
    runs = list(session.runs)
    if saved.outcome == "removed":
        runs = runs_without(runs, [saved.run_id])
    elif saved.stored is not None:
        place = next((index for index, run in enumerate(runs) if run.run_id == saved.run_id), None)
        if place is None or saved.moved:
            runs = [*(run for run in runs if run.run_id != saved.run_id), saved.stored]
        else:
            runs[place] = saved.stored
    # A new list, so a reader of the old one is unaffected. Only the store assigns a session's runs and summary, and
    # async callers run its writes in the database's lane one at a time, so no write's assignment overtakes another's.
    session.runs = runs


def runs_without(runs: Iterable[AnyRunRecord], run_ids: Iterable[str]) -> list[AnyRunRecord]:
    """Return ``runs`` minus ``run_ids`` and their member runs; team members are agents, so none has members."""
    removed = set(run_ids)
    return [run for run in runs if run.run_id not in removed and run.parent_run_id not in removed]


def persist_to(store: ConversationStore, session: ConversationSession) -> Callable[[AnyRunRecord], Awaitable[None]]:
    """Return the callback a run hands its terminal record to: save it to ``store`` with its usage, in the database's lane.

    The record saves as ``ConversationStore.save_run`` saves it with ``record_usage``.
    The record and ``session.session_state``, which the run's tools change in place, are copied on the caller's task
    before the lane is awaited, so what lands is what was handed over. The state is written back, in the record's
    own transaction, when it differs from ``session.stored_session_state``, which every write of the field records,
    so a turn whose runs or checkpoints change the state and restore it stores it restored.
    The save mirrors itself in ``session.runs`` in the lane, where the session's saves run one at a time; it completes
    even when the caller is cancelled, as the lane's operations do.
    """

    async def persist(record: AnyRunRecord) -> None:
        data, kind = record.to_dict(), run_type(record)
        await write_with_session_state(
            store,
            session,
            lambda opened, state: opened._save(
                session,
                data,
                kind,
                interrupted=False,
                record_usage=True,
                state=state,
            ),
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


def ensure_session_row(
    connection: sqlite3.Connection,
    names: TableNames,
    session: ConversationSession,
) -> dict[str, Any] | None:
    """Create ``session``'s row in the caller's write transaction unless one exists, keeping one another writer made.

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


def next_seq(connection: sqlite3.Connection, names: TableNames) -> int:
    """Hand out the database's next run ``seq`` in the caller's write transaction; the counter never goes back."""
    (seq,) = connection.execute(
        f"UPDATE {quote_identifier(names.log_meta)} SET next_seq = next_seq + 1 WHERE id = 1 RETURNING next_seq - 1",  # noqa: S608
    ).fetchone()
    return seq


def _write_run(
    connection: sqlite3.Connection,
    names: TableNames,
    session_id: str,
    data: dict[str, Any],
    kind: SessionKind,
    *,
    interrupted: bool,
) -> _Saved:
    """Write one run from its stored dict (``record.to_dict()``) under the save rules (``ConversationStore.save_run``).

    An interrupted record (``interrupted``) is never final.
    """
    run_id, parent_run_id = data["run_id"], data.get("parent_run_id")
    runs = quote_identifier(names.log_runs)
    now = int(time())
    if connection.execute(
        f"SELECT 1 FROM {quote_identifier(names.log_removed)} WHERE session_id = ? AND run_id IN (?, ?)",  # noqa: S608
        (session_id, run_id, parent_run_id),
    ).fetchone():
        return _Saved(run_id, "removed", created_at=data.get("created_at") or now, updated_at=now)
    stored = _stored_row(connection, runs, session_id, run_id)
    team_run = _stored_row(connection, runs, session_id, parent_run_id) if parent_run_id is not None else None
    if team_run is not None and team_run.parent_run_id is not None:
        msg = f"Run {run_id!r} names member run {parent_run_id!r} as its team run; team members have no members"
        raise ValueError(msg)
    if not interrupted and _is_visible(team_run if parent_run_id is not None else stored):
        return _Saved(run_id, "final")
    columns = run_columns(data, kind)
    if stored is None:
        moved, created_at = (
            True,
            _insert_run(connection, runs, session_id, data, columns, next_seq(connection, names), now),
        )
    else:
        # A run saved visible over its paused, cancelled, or errored row finished after the turns it waited for.
        moved = (
            parent_run_id is None
            and stored.parent_run_id is None
            and stored.status in NON_VISIBLE_RUN_STATUS_VALUES
            and columns.status not in NON_VISIBLE_RUN_STATUS_VALUES
        )
        created_at = stored.created_at
        connection.execute(
            f"UPDATE {runs} SET {', '.join(f'{column} = ?' for column in _WRITTEN_COLUMNS)} WHERE run_id = ?",  # noqa: S608
            (
                *_written_values(parent_run_id, columns, next_seq(connection, names) if moved else stored.seq, now),
                run_id,
            ),
        )
    _set_visible_messages(
        connection,
        names,
        run_id,
        data["messages"],
        keep_system=columns.status == RunStatus.paused.value,
    )
    _adopt_requester(connection, names, session_id, data.get("user_id"))
    [written] = _read_runs(connection, names, "run.run_id = ?", (run_id,))
    return _Saved(run_id, "written", created_at=created_at, updated_at=now, moved=moved, stored=written)


def _insert_run(
    connection: sqlite3.Connection,
    runs: str,
    session_id: str,
    data: Mapping[str, Any],
    columns: RunColumns,
    seq: int,
    now: int,
) -> int:
    """Insert a run's first row from its stored dict and columns, written at ``now``; return its ``created_at``."""
    created_at = data.get("created_at") or now
    connection.execute(
        f"INSERT INTO {runs} (run_id, session_id, created_at, {', '.join(_WRITTEN_COLUMNS)}) "  # noqa: S608
        f"VALUES (?, ?, ?, {', '.join('?' for _ in _WRITTEN_COLUMNS)})",
        (data["run_id"], session_id, created_at, *_written_values(data.get("parent_run_id"), columns, seq, now)),
    )
    return created_at


def _written_values(parent_run_id: str | None, columns: RunColumns, seq: int, now: int) -> tuple[object, ...]:
    """Return the values of ``_WRITTEN_COLUMNS``, in order, for a row written at ``now``."""
    return (
        parent_run_id,
        columns.kind,
        columns.scope_key,
        columns.status,
        seq,
        now,
        columns.metadata,
        columns.record,
        columns.requirements,
        columns.tools,
    )


def _stored_row(connection: sqlite3.Connection, runs: str, session_id: str, run_id: str) -> _StoredRow | None:
    row = connection.execute(
        f"SELECT parent_run_id, status, seq, created_at FROM {runs} WHERE session_id = ? AND run_id = ?",  # noqa: S608
        (session_id, run_id),
    ).fetchone()
    return None if row is None else _StoredRow(*row)


def _is_visible(row: _StoredRow | None) -> bool:
    """Whether a stored run replays as model history: top-level and not paused, cancelled, or errored."""
    return row is not None and row.parent_run_id is None and row.status not in NON_VISIBLE_RUN_STATUS_VALUES


def _set_visible_messages(
    connection: sqlite3.Connection,
    names: TableNames,
    run_id: str,
    messages: Sequence[Mapping[str, Any]],
    *,
    keep_system: bool,
) -> None:
    """Make run ``run_id``'s replay view ``messages``, stored message dicts in order, rewriting only changed rows.

    A message replayed from history is never stored, and a system message only with ``keep_system``, for a paused
    run's continuation. A folded row stays as it is, so a message a fold took never replays again; an unfolded row
    the list no longer holds is deleted. A list holding one message id twice raises, rather than keeping one copy.
    """
    kept = [
        message
        for message in messages
        if not message.get("from_history") and (keep_system or message["role"] != "system")
    ]
    ids = [message["id"] for message in kept]
    if len(set(ids)) < len(ids):
        msg = f"Run {run_id!r} holds a message id twice"
        raise ValueError(msg)
    table = quote_identifier(names.log_messages)
    stored = {
        message_id: (position, folded_by, data)
        for message_id, position, folded_by, data in connection.execute(
            f"SELECT message_id, ord, folded_by, data FROM {table} WHERE run_id = ?",  # noqa: S608
            (run_id,),
        )
    }
    writes: list[tuple[str, str, int, str]] = []
    for position, message in enumerate(kept):
        data = json.dumps(message)
        held = stored.get(message["id"])
        if held is None or (held[1] is None and (held[0], held[2]) != (position, data)):
            writes.append((run_id, message["id"], position, data))
    connection.executemany(
        f"INSERT INTO {table} (run_id, message_id, ord, data) VALUES (?, ?, ?, ?) "  # noqa: S608
        "ON CONFLICT(run_id, message_id) DO UPDATE SET ord = excluded.ord, data = excluded.data",
        writes,
    )
    connection.execute(
        f"DELETE FROM {table} WHERE run_id = ? AND folded_by IS NULL "  # noqa: S608
        "AND message_id NOT IN (SELECT value FROM json_each(?))",
        (run_id, json.dumps(ids)),
    )


def _read_runs(
    connection: sqlite3.Connection,
    names: TableNames,
    condition: str,
    parameters: tuple[object, ...],
) -> list[AnyRunRecord]:
    """Decode the runs whose rows meet ``condition``, in ``seq`` order, each in its replay view.

    A paused team run comes with the member runs its requirements name, in first-mention order, as its
    ``member_responses``, which a team resume reads; each is a copy of its own, apart from the member's own record.
    """
    rows = _run_rows(connection, names, f"{condition} ORDER BY run.seq", parameters)
    messages = _visible_messages(connection, names, [row.run_id for row in rows])
    records = [_decoded(row, messages) for row in rows]
    named = {
        record.run_id: _named_members(record)
        for record in records
        if isinstance(record, TeamRunRecord) and record.status is RunStatus.paused
    }
    by_id = {row.run_id: row for row in rows}
    if missing := {member_id for member_ids in named.values() for member_id in member_ids} - by_id.keys():
        extra = _run_rows(
            connection,
            names,
            "run.run_id IN (SELECT value FROM json_each(?))",
            (json.dumps(sorted(missing)),),
        )
        by_id |= {row.run_id: row for row in extra}
        messages |= _visible_messages(connection, names, [row.run_id for row in extra])
    for record in records:
        if isinstance(record, TeamRunRecord) and record.run_id in named:
            record.member_responses = [
                cast("RunRecord", _decoded(by_id[member_id], messages))
                for member_id in named[record.run_id]
                if member_id in by_id and by_id[member_id].parent_run_id == record.run_id
            ]
    return records


def _run_rows(
    connection: sqlite3.Connection,
    names: TableNames,
    condition: str,
    parameters: tuple[object, ...],
) -> list[_RunRow]:
    return [
        _RunRow(run_id, session_id, parent_run_id, RunColumns(cast("SessionKind", kind), *columns))
        for run_id, session_id, parent_run_id, kind, *columns in connection.execute(
            f"SELECT {_RUN_COLUMNS} FROM {quote_identifier(names.log_runs)} AS run WHERE {condition}",  # noqa: S608
            parameters,
        )
    ]


def _visible_messages(connection: sqlite3.Connection, names: TableNames, run_ids: list[str]) -> dict[str, list[str]]:
    """Return the unfolded message rows' JSON of each of ``run_ids``, in order."""
    messages: dict[str, list[str]] = {}
    for run_id, data in connection.execute(
        f"SELECT run_id, data FROM {quote_identifier(names.log_messages)} WHERE folded_by IS NULL "  # noqa: S608
        "AND run_id IN (SELECT value FROM json_each(?)) ORDER BY run_id, ord",
        (json.dumps(run_ids),),
    ):
        messages.setdefault(run_id, []).append(data)
    return messages


def _decoded(row: _RunRow, messages: Mapping[str, list[str]]) -> AnyRunRecord:
    # Each decode parses the JSON afresh, so no two records share a message's dicts or lists.
    message_dicts = [json.loads(data) for data in messages.get(row.run_id, ())]
    return run_record(row.run_id, row.session_id, row.parent_run_id, row.columns, message_dicts)


def _named_members(team_run: TeamRunRecord) -> list[str]:
    """Return the member runs a paused team run's requirements name, in first-mention order."""
    named = (requirement.member_run_id for requirement in team_run.requirements or ())
    return list(dict.fromkeys(member_id for member_id in named if member_id is not None))


def _loads_sql(alias: str, summaries: str) -> str:
    """Return the SQL condition under which run ``alias`` loads as a top-level run.

    A paused, cancelled, or errored run loads wherever it sits; any other loads unless its scope's latest summary
    row, a barrier included, covers it. Every run of a scope without summary rows loads.
    """
    return (
        f"({alias}.parent_run_id IS NULL AND ({alias}.status IN ({_NON_VISIBLE_STATUS_SQL}) OR {alias}.seq > COALESCE("  # noqa: S608
        f"(SELECT row.through FROM {summaries} AS row WHERE row.session_id = {alias}.session_id "
        f"AND row.scope_key = {alias}.scope_key ORDER BY row.id DESC LIMIT 1), 0)))"
    )


def _replay_summary(
    connection: sqlite3.Connection,
    summaries: str,
    session_id: str,
    owner_scope: str | None,
) -> ScopeSummary | None:
    """Return the text of the owner scope's latest summary row, which the session replays; a barrier has none."""
    if owner_scope is None:
        return None
    latest = connection.execute(
        f"SELECT summary, created_at FROM {summaries} WHERE session_id = ? AND scope_key = ? "  # noqa: S608
        "ORDER BY id DESC LIMIT 1",
        (session_id, owner_scope),
    ).fetchone()
    if latest is None or latest[0] is None:
        return None
    return ScopeSummary(text=latest[0], updated_at=datetime.fromtimestamp(latest[1], UTC))


def _join_covering_summary(
    connection: sqlite3.Connection,
    names: TableNames,
    session_id: str,
    scope_key: str,
    seq: int,
    metadata: Mapping[str, Any],
) -> None:
    """Join a covered visible run's event ids to the event sets of the summary row that covers it, if one does.

    That row is the first summary of the scope after its latest barrier whose ``through`` reaches the run's ``seq``.
    A run behind the latest barrier is retired, so no row covers it.
    """
    summaries = quote_identifier(names.log_summaries)
    barrier = connection.execute(
        f"SELECT id, through FROM {summaries} WHERE session_id = ? AND scope_key = ? AND summary IS NULL "  # noqa: S608
        "ORDER BY id DESC LIMIT 1",
        (session_id, scope_key),
    ).fetchone()
    if barrier is not None and seq <= barrier[1]:
        return
    covering = connection.execute(
        f"SELECT id, event_ids, seen_event_ids FROM {summaries} WHERE session_id = ? AND scope_key = ? "  # noqa: S608
        "AND summary IS NOT NULL AND id > ? AND through >= ? ORDER BY id LIMIT 1",
        (session_id, scope_key, barrier[0] if barrier is not None else 0, seq),
    ).fetchone()
    if covering is None:
        return
    row_id, event_ids, seen = covering
    seen_ids = seen_event_ids(metadata)
    connection.execute(
        f"UPDATE {summaries} SET event_ids = ?, seen_event_ids = ? WHERE id = ?",  # noqa: S608
        (
            json.dumps(sorted(set(json.loads(event_ids)) | seen_ids | source_event_ids(metadata))),
            json.dumps(sorted(set(json.loads(seen)) | seen_ids)),
            row_id,
        ),
    )


def source_event_ids(metadata: object) -> set[str]:
    """Return the Matrix event ids a run answered, from its metadata."""
    if not isinstance(metadata, dict):
        return set()
    metadata = cast("dict[str, Any]", metadata)
    event_ids: set[str] = set()
    event_id = metadata.get(MATRIX_EVENT_ID_METADATA_KEY)
    if isinstance(event_id, str) and event_id:
        event_ids.add(event_id)
    for key in (MATRIX_SOURCE_EVENT_IDS_METADATA_KEY, MATRIX_TURN_DISCOVERY_EVENT_IDS_METADATA_KEY):
        raw_ids = metadata.get(key)
        if isinstance(raw_ids, list):
            event_ids.update(candidate for candidate in raw_ids if isinstance(candidate, str) and candidate)
    return event_ids


def seen_event_ids(metadata: object) -> set[str]:
    """Return the Matrix event ids a run already represents, from its metadata."""
    if not isinstance(metadata, dict):
        return set()
    metadata = cast("dict[str, Any]", metadata)
    event_ids: set[str] = set()
    raw_seen_ids = metadata.get(MATRIX_SEEN_EVENT_IDS_METADATA_KEY)
    if isinstance(raw_seen_ids, list):
        event_ids.update(event_id for event_id in raw_seen_ids if isinstance(event_id, str) and event_id)
    raw_revisions = metadata.get(MATRIX_SOURCE_EVENT_REVISIONS_METADATA_KEY)
    if isinstance(raw_revisions, dict):
        event_ids.update(
            revision[1]
            for revision in raw_revisions.values()
            if isinstance(revision, list | tuple) and len(revision) == 2 and isinstance(revision[1], str)
        )
    response_event_id = metadata.get(MATRIX_RESPONSE_EVENT_ID_METADATA_KEY)
    if isinstance(response_event_id, str) and response_event_id:
        event_ids.add(response_event_id)
    return event_ids


@dataclass(frozen=True, slots=True)
class FoldWrite:
    """What a mid-turn fold stores of its running run, beside the fold's summary row.

    ``session`` names the conversation, and is the row to create when it is missing; ``record`` is the live running
    record; ``messages`` are the messages the fold took; ``member_run_ids`` are the member runs that ended before it.
    """

    session: ConversationSession
    record: AnyRunRecord
    messages: Sequence[Message]
    member_run_ids: Collection[str]


def store_fold(connection: sqlite3.Connection, names: TableNames, fold: FoldWrite, summary_id: int) -> None:
    """Store what fold summary ``summary_id`` took out of its running run, in the caller's write transaction.

    The running run takes the next ``seq``, so it sorts after the summary. A run without a row yet, such as one that
    has not paused or checkpointed, gets one: cancelled, with its live scalars and metadata and no tools or
    requirements, so a crash leaves a run that does not replay. Each folded message's row is flagged with the summary,
    a stored one keeping its data, and so is each named member run's row, so neither replays again. The caller
    creates the session row (``ensure_session_row``) before inserting the summary row.
    """
    session_id, record = fold.session.session_id, fold.record
    runs = quote_identifier(names.log_runs)
    seq = next_seq(connection, names)
    if not connection.execute(
        f"UPDATE {runs} SET seq = ? WHERE session_id = ? AND run_id = ?",  # noqa: S608
        (seq, session_id, record.run_id),
    ).rowcount:
        data = {**record.to_dict(), "status": RunStatus.cancelled.value, "tools": None, "requirements": None}
        _insert_run(connection, runs, session_id, data, run_columns(data, run_type(record)), seq, int(time()))
    connection.executemany(
        f"INSERT INTO {quote_identifier(names.log_messages)} (run_id, message_id, ord, folded_by, data) "  # noqa: S608
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT(run_id, message_id) DO UPDATE SET folded_by = COALESCE(folded_by, excluded.folded_by)",
        [
            (record.run_id, message.id, position, summary_id, json.dumps(message.to_dict()))
            for position, message in enumerate(fold.messages)
        ],
    )
    connection.execute(
        f"UPDATE {runs} SET folded_by = ? WHERE session_id = ? AND parent_run_id = ? AND folded_by IS NULL "  # noqa: S608
        "AND run_id IN (SELECT value FROM json_each(?))",
        (summary_id, session_id, record.run_id, json.dumps(sorted(fold.member_run_ids))),
    )


def delete_runs_in_transaction(
    connection: sqlite3.Connection,
    names: TableNames,
    run_ids: Collection[str],
    *,
    session_id: str,
) -> set[str]:
    """Delete session ``session_id``'s runs ``run_ids`` and their member runs in the caller's write transaction.

    Their message rows go with them. Returns the ids of the deleted runs.
    """
    selected = json.dumps(sorted(run_ids))
    return {
        run_id
        for (run_id,) in connection.execute(
            f"DELETE FROM {quote_identifier(names.log_runs)} WHERE session_id = ? "  # noqa: S608
            "AND (run_id IN (SELECT value FROM json_each(?)) OR parent_run_id IN (SELECT value FROM json_each(?))) "
            "RETURNING run_id",
            (session_id, selected, selected),
        ).fetchall()
    }


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
    metrics = snapshot.get("metrics")
    if not isinstance(metrics, dict):
        return RunMetrics()
    metrics = cast("dict[str, object]", metrics)
    counted = RunMetrics(tokens=TokenCounts.from_mapping(metrics, _counted_value))
    details = metrics.get("details")
    models = cast("dict[str, object]", details).get("model") if isinstance(details, dict) else None
    if isinstance(models, list):
        counted.details = {
            "model": [_counted_model(cast("dict[str, object]", entry)) for entry in models if isinstance(entry, dict)],
        }
    return counted


def _counted_model(entry: Mapping[str, object]) -> ModelMetrics:
    model_id, provider = entry.get("id"), entry.get("provider")
    return ModelMetrics(
        tokens=TokenCounts.from_mapping(entry, _counted_value),
        id=model_id if isinstance(model_id, str) else "",
        provider=provider if isinstance(provider, str) else "",
    )


def _counted_value(value: object) -> int:
    # The ledger keeps an invalid counter as a sentinel for its reporter; only real counts add up.
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


__all__ = [
    "BUSY_TIMEOUT_SECONDS",
    "ConversationStore",
    "FoldWrite",
    "SaveOutcome",
    "connect_conversation_database",
    "decoded_mapping",
    "delete_runs_in_transaction",
    "ensure_session_row",
    "migrate_database",
    "next_seq",
    "persist_to",
    "runs_without",
    "seen_event_ids",
    "source_event_ids",
    "store_fold",
    "write_with_session_state",
]
