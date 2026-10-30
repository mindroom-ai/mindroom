"""The conversation store: one SQLite database of sessions, their runs and message rows, summary rows, and usage.

Every operation runs in one transaction on its own short-lived connection and leaves the journal mode alone, so
databases keep SQLite's default rollback journal. A write takes the write lock before it reads anything its rules
depend on, and a load reads from one snapshot. Opening a database (``create_state_storage``) converts it and creates
every table it lacks, so no operation runs DDL.

A run is a ``*_log_runs`` row plus a ``*_log_messages`` row per stored message and a ``*_log_tools`` row per tool
execution, so a save rewrites only the rows that changed, and every reader gets its replay view: the message rows no
mid-turn fold took, in order. ``seq`` places a run in the log; the meta row's counter hands it out and never goes
back, so a deleted run's number is never reused.
"""

from __future__ import annotations

import copy
import json
import sqlite3
from collections import defaultdict
from contextlib import closing, contextmanager
from contextvars import ContextVar
from dataclasses import astuple, dataclass, fields, replace
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
from mindroom.history.types import HistoryScope, ScopeSummary, owner_scope
from mindroom.llm.runs import RunStatus, TeamRunRecord
from mindroom.session_storage.codec import RunColumns, decode_json, run_columns, run_kind, run_record
from mindroom.session_storage.lanes import run_session_storage_operation
from mindroom.session_storage.schema import (
    NON_VISIBLE_RUN_STATUS_SQL,
    NON_VISIBLE_RUN_STATUS_VALUES,
    TableNames,
    quote_identifier,
    usage_upsert_sql,
)
from mindroom.session_storage.types import ConversationSession, session_state_snapshot
from mindroom.usage.storage import helper_usage_snapshot, run_usage_snapshot

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Collection, Iterable, Iterator, Mapping, Sequence
    from pathlib import Path

    from mindroom.llm.messages import Message
    from mindroom.llm.runs import AnyRunRecord, RunRecord
    from mindroom.llm.usage import HelperUsage
    from mindroom.session_storage.types import SessionKind
    from mindroom.usage.storage import IndependentUsageKind

# How long an operation waits for another connection's write lock on the database, its conversion included.
BUSY_TIMEOUT_SECONDS = 30.0
_NON_VISIBLE = frozenset(NON_VISIBLE_RUN_STATUS_VALUES)
# The run-row columns a run's stored dict fills: ``RunColumns``' fields, in their order.
_RECORD_COLUMNS = tuple(field.name for field in fields(RunColumns))

# What a full save did: wrote the run, wrote nothing because a cut removed it, or wrote nothing because it is final.
type SaveOutcome = Literal["written", "removed", "final"]


def connect_conversation_database(path: Path, *, timeout: float = BUSY_TIMEOUT_SECONDS) -> sqlite3.Connection:
    """Open a connection to the conversation database at ``path``, for the store and the conversion that opens it.

    ``timeout`` is how long a statement waits for another connection's lock; a caller that would rather skip a
    locked database than wait passes less.
    """
    # No implicit transactions: every operation begins its own, so its reads cannot straddle a commit.
    connection = sqlite3.connect(path, timeout=timeout, isolation_level=None)
    # Deleting a session row cascades to its runs, summary rows, and usage, and deleting a run to its message and tool
    # rows.
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def connect_by_name(path: Path, *, timeout: float = BUSY_TIMEOUT_SECONDS) -> sqlite3.Connection:
    """Connect to the file ``path`` names itself, never to a link's target, which ``sqlite3.connect`` would open or create.

    Opening a store, or converting its database at startup, connects this way. A link at the database or its sessions
    directory is refused before connecting. The name SQLite resolves for the file, every link followed, must then be
    ``path`` with only the directories above the sessions directory resolved, so a link swapped in at either place
    before SQLite resolves the path is refused before anything is written, though SQLite's open can have created an
    empty file at the link's target. Links above the sessions directory are followed, as the store follows them.
    """
    if path.parent.is_symlink() or path.is_symlink():
        msg = f"Refusing to open {path}: the conversation database or its directory is a link"
        raise ValueError(msg)
    expected = str(path.parent.parent.resolve() / path.parent.name / path.name)
    connection = connect_conversation_database(path, timeout=timeout)
    (opened,) = connection.execute("SELECT file FROM pragma_database_list WHERE name = 'main'").fetchone()
    if opened != expected:
        connection.close()
        msg = f"Refusing to open {path}: SQLite opened {opened} instead"
        raise ValueError(msg)
    return connection


@contextmanager
def begun(connection: sqlite3.Connection, *, write: bool) -> Iterator[None]:
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
        with closing(connect_conversation_database(self.path)) as connection, begun(connection, write=write):
            yield connection

    def read_session_metadata(self, session_id: str) -> dict[str, Any] | None:
        """Read one session row's metadata without its state or runs, or None when the row is missing or holds none."""
        with self.transaction(write=False) as connection:
            row = connection.execute(
                f"SELECT metadata FROM {quote_identifier(self.names.sessions)} WHERE session_id = ?",  # noqa: S608
                (session_id,),
            ).fetchone()
        return None if row is None else decoded_mapping(row[0])

    def load_session(self, session_id: str) -> ConversationSession | None:
        """Load one session with the runs a reply reasons over, or None when it is missing.

        The session's kind and owner are the database's owner. The runs are every top-level run its scope's latest
        summary row does not cover, every paused, cancelled, or errored one wherever it sits, and the member runs of
        those, in ``seq`` order, each in its replay view.
        """
        with self.transaction(write=False) as connection:
            loaded = self._read_load(connection, session_id)
        if loaded is None:
            return None
        session_state, metadata, created_at = loaded.row
        state = _session_state(session_state)
        return ConversationSession(
            session_id=session_id,
            kind=loaded.owner.kind,
            owner_id=loaded.owner.scope_id,
            metadata=decoded_mapping(metadata),
            session_state=state,
            stored_session_state=copy.deepcopy(state),
            runs=loaded.runs,
            summary=loaded.summary,
            created_at=created_at,
        )

    def _read_load(self, connection: sqlite3.Connection, session_id: str) -> _Load | None:
        """Read what a load of session ``session_id`` returns, in the caller's transaction."""
        names = self.names
        row = connection.execute(
            f"SELECT session_state, metadata, created_at FROM {quote_identifier(names.sessions)} "  # noqa: S608
            "WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        owner = database_owner(connection, names)
        runs, summaries = quote_identifier(names.log_runs), quote_identifier(names.log_summaries)
        loaded = (
            f"run.session_id = ? AND ({_loads_sql('run', summaries)} OR run.parent_run_id IN "  # noqa: S608
            f"(SELECT top.run_id FROM {runs} AS top WHERE top.session_id = ? AND {_loads_sql('top', summaries)}))"
        )
        return _Load(
            owner=owner,
            row=row,
            runs=_read_runs(connection, names, loaded, (session_id, session_id)),
            summary=_replay_summary(connection, summaries, session_id, owner.key),
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
            loaded = self._read_load(connection, session_id) if session is not None else None
        if session is not None:
            _adopt_log(session, loaded)

    def reload_log(self, session: ConversationSession) -> None:
        """Make ``session``'s runs and summary what a load returns now, after another writer changed its log."""
        with self.transaction(write=False) as connection:
            loaded = self._read_load(connection, session.session_id)
        _adopt_log(session, loaded)

    def save_run(
        self,
        session: ConversationSession,
        run: AnyRunRecord,
        *,
        record_usage: bool,
        state: dict[str, Any] | None = None,
    ) -> SaveOutcome:
        """Save ``run`` of ``session`` whole, creating the session row if needed, unless it is removed or final.

        A run a cut removed, or a member of one, writes nothing (``"removed"``). A run whose stored status is visible,
        or a member whose team run's is, writes nothing (``"final"``), so a stale copy never makes a finished run
        pending again. Otherwise the run's row and replay view become the record's (``"written"``): a new run, or a
        top-level run saved visible over its paused, cancelled, or errored row, takes the next ``seq``, so it replays
        after the turns it waited for, and the first requester a written run names goes on a row that has none.

        ``record_usage`` replaces the run's usage snapshot in the ledger, which outlives removed runs, and moves the
        session's update time; a final run's usage is not recorded. ``state``, a ``session_state_snapshot`` of the
        state the run ran with, commits with the run when the row does not hold it. ``session.runs`` then holds the run
        as the store does (``_mirror``).
        """
        usage = run_usage_snapshot(run) if record_usage else None
        return self._save(session, run.to_dict(), run_kind(run), usage=usage, state=state)

    def save_interrupted_record(
        self,
        session: ConversationSession,
        record: AnyRunRecord,
        *,
        state: dict[str, Any] | None = None,
    ) -> Literal["written", "removed"]:
        """Save an interrupted turn's record as ``save_run`` does, over a final run too, without its usage.

        It is what the user saw or heard of a turn whose failure followed its terminal save, so it replaces the stored
        answer; a visible run keeps its ``seq``, so replay order does not change.
        """
        kind = run_kind(record)
        outcome = self._save(session, record.to_dict(), kind, usage=None, state=state, interrupted=True)
        return cast("Literal['written', 'removed']", outcome)

    def _save(
        self,
        session: ConversationSession,
        data: dict[str, Any],
        kind: SessionKind,
        *,
        usage: dict[str, object] | None,
        state: dict[str, Any] | None,
        interrupted: bool = False,
    ) -> SaveOutcome:
        """Save one run from its stored dict, its ``usage`` snapshot when given, and ``state`` in one transaction.

        The run is then mirrored in ``session``.
        """
        with self.transaction(write=True) as connection:
            created = ensure_session_row(connection, self.names, session)
            saved = _write_run(connection, self.names, session.session_id, data, kind, interrupted=interrupted)
            # A run saved again when it continues after a pause, or at a checkpoint, replaces its snapshot, so it
            # counts once.
            recorded = False
            if usage is not None and saved.outcome != "final":
                recorded = True
                snapshot = {**usage, "created_at": saved.created_at, "updated_at": saved.updated_at}
                connection.execute(
                    usage_upsert_sql(self.names),
                    (session.session_id, data["run_id"], json.dumps(snapshot)),
                )
            # Background readers learn of recorded usage, and of a finished run new to the log or finished after the
            # turns it waited for, through the session's update time (``list_updated_sessions``).
            if recorded or (saved.moved and data["status"] not in _NON_VISIBLE):
                _move_update_time(connection, self.names, session.session_id, saved.updated_at)
            stored = self._write_state(connection, session, created, state)
        _record_stored_state(session, stored)
        _mirror(session, saved)
        if created is not None:
            report_created_row(session)
        return saved.outcome

    def _save_usage(
        self,
        session: ConversationSession,
        data: dict[str, Any],
        usage: dict[str, object],
        *,
        state: dict[str, Any] | None,
    ) -> None:
        """Record the ``usage`` snapshot of the run whose stored dict is ``data`` and commit ``state``, but no run.

        The run is no turn of the conversation, so no load ever replays it: any row it already holds, such as the
        cancelled one a mid-turn fold wrote, goes with its member runs and messages, while every summary row stays.
        Its usage counts as a stored run's does, the session row is created if needed, and the first requester the
        run names goes on a row that has none. ``session.runs`` then holds none of the run.
        """
        session_id, run_id, now = session.session_id, data["run_id"], int(time())
        with self.transaction(write=True) as connection:
            created = ensure_session_row(connection, self.names, session)
            delete_runs_in_transaction(connection, self.names, [run_id], session_id=session_id)
            _adopt_requester(connection, self.names, session_id, data.get("user_id"))
            snapshot = {**usage, "created_at": data.get("created_at") or now, "updated_at": now}
            connection.execute(usage_upsert_sql(self.names), (session_id, run_id, json.dumps(snapshot)))
            _move_update_time(connection, self.names, session_id, now)
            stored = self._write_state(connection, session, created, state)
        _record_stored_state(session, stored)
        session.runs = runs_without(session.runs, [run_id])
        if created is not None:
            report_created_row(session)

    def patch_run_metadata(
        self,
        session_id: str,
        run_id: str,
        changes: Mapping[str, Any],
        *,
        session: ConversationSession | None = None,
    ) -> bool:
        """Set ``changes``' top-level keys in stored run ``run_id``'s metadata and keep the rest, a final run's too.

        A visible top-level run's event ids also join the event sets of the summary row that covers it, so redacting a
        reply linked after that summary still cuts it. Returns whether the run is stored. ``session``, when given, then
        holds a changed copy of the run in a new list; loaded runs are shared, so none changes in place.
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
            connection.execute(f"UPDATE {runs} SET metadata = ? WHERE run_id = ?", (json.dumps(metadata), run_id))  # noqa: S608
            if parent_run_id is None and status not in _NON_VISIBLE:
                _join_covering_row(connection, self.names, session_id, scope_key, seq, metadata)
        if session is not None:
            session.runs = [
                replace(run, metadata=copy.deepcopy(metadata)) if run.run_id == run_id else run for run in session.runs
            ]
        return True

    def rewrite_run_messages(
        self,
        session_id: str,
        edit: Callable[[list[AnyRunRecord]], list[AnyRunRecord]],
        *,
        session: ConversationSession | None = None,
    ) -> None:
        """Apply ``edit`` to fresh copies of the runs a load returns, under the write lock, and store their messages.

        ``edit`` returns the runs it changed and changes no other. Only messages store, under the save's message rules,
        and only for runs the load returned, so no other writer's status, metadata, or messages are lost and no covered
        run changes. Once it commits, ``session``, when given, holds what a load returns: the load ``edit`` was handed
        when it stored nothing, which spares a second read.
        """
        with self.transaction(write=True) as connection:
            loaded = self._read_load(connection, session_id)
            runs = loaded.runs if loaded is not None else []
            statuses = {run.run_id: run.status.value for run in runs}
            stored = [(run, status) for run in edit(runs) if (status := statuses.get(run.run_id)) is not None]
            for run, status in stored:
                messages = [message.to_dict() for message in run.messages]
                set_visible_messages(connection, self.names, run.run_id, messages, status)
            if stored and session is not None:
                loaded = self._read_load(connection, session_id)
        if session is not None:
            _adopt_log(session, loaded)

    def get_run(self, run_id: str) -> AnyRunRecord | None:
        """Return one stored run, in whatever session, in its replay view; a paused team run comes with its members."""
        with self.transaction(write=False) as connection:
            return next(iter(_read_runs(connection, self.names, "run.run_id = ?", (run_id,))), None)

    def list_runs(self, session_id: str) -> list[AnyRunRecord]:
        """Return every stored run of the session, member runs included, in ``seq`` order, each in its replay view."""
        with self.transaction(write=False) as connection:
            return _read_runs(connection, self.names, "run.session_id = ?", (session_id,))

    def logged_runs(self, session_id: str) -> list[LoggedRun]:
        """Return the session's top-level runs with their places in its log, as ``logged_runs`` reads them."""
        with self.transaction(write=False) as connection:
            return logged_runs(connection, self.names, session_id)

    def log_end(self) -> int:
        """Return the last place the database's log handed out, or 0 before its first run; later saves place after it."""
        with self.transaction(write=False) as connection:
            (next_seq,) = connection.execute(
                f"SELECT next_seq FROM {quote_identifier(self.names.log_meta)} WHERE id = 1",  # noqa: S608
            ).fetchone()
        return next_seq - 1

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
        if created is not None:
            report_created_row(session)

    def list_updated_sessions(self, *, since: int) -> list[tuple[str, int]]:
        """Return ``(session_id, updated_at)`` of the sessions updated after ``since``, oldest first.

        A session's update time moves when its row is written: a changed state a run save carries, a metadata change,
        a run save that records its usage, as a run's terminal save through ``persist_to`` does, and a save that places
        a visible run at the log's end (a new one, or one saved visible over its paused, cancelled, or errored row), so a
        reader following ``seq`` learns of every run that finished, however it was saved.
        """
        with self.transaction(write=False) as connection:
            return connection.execute(
                "SELECT session_id, COALESCE(updated_at, created_at) AS updated "  # noqa: S608
                f"FROM {quote_identifier(self.names.sessions)} WHERE COALESCE(updated_at, created_at) > ? "
                "ORDER BY updated, session_id",
                (since,),
            ).fetchall()

    def save_independent_usage(
        self,
        *,
        session_id: str,
        usage_id: str,
        kind: IndependentUsageKind,
        requester_id: str | None,
        usage: HelperUsage,
        initial_session: ConversationSession | None = None,
    ) -> None:
        """Upsert helper usage as ``usage_id``, owned by a conversation and a requester, beside the run log.

        ``initial_session`` creates the conversation's row when no run has yet.
        """
        snapshot = helper_usage_snapshot(usage, usage_id=usage_id, kind=kind, requester_id=requester_id)
        created = None
        with self.transaction(write=True) as connection:
            if initial_session is not None and ensure_session_row(connection, self.names, initial_session) is not None:
                created = initial_session
            connection.execute(usage_upsert_sql(self.names), (session_id, usage_id, json.dumps(snapshot)))
        if created is not None:
            report_created_row(created)


class LoggedRun(NamedTuple):
    """A stored run and its place in its session's log."""

    seq: int
    run: AnyRunRecord


@dataclass(frozen=True, slots=True)
class _Load:
    """What one load of a session read: the database's owner, the session's row, its runs, and its summary."""

    owner: HistoryScope
    row: tuple[Any, ...]
    runs: list[AnyRunRecord]
    summary: ScopeSummary | None


def _adopt_log(session: ConversationSession, loaded: _Load | None) -> None:
    """Make ``session``'s runs and summary those of ``loaded``, or none when the session is no longer stored."""
    session.runs = loaded.runs if loaded is not None else []
    session.summary = loaded.summary if loaded is not None else None


@dataclass(frozen=True, slots=True)
class _Saved:
    """One full save: its outcome, the timestamps its usage takes, and a written run's move and read-back copy."""

    run_id: str
    outcome: SaveOutcome
    created_at: int = 0
    updated_at: int = 0
    moved: bool = False
    stored: AnyRunRecord | None = None


class _StoredRow(NamedTuple):
    """What a save reads of a run's stored row, or of its team run's."""

    parent_run_id: str | None
    status: str
    seq: int
    created_at: int


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


def _move_update_time(connection: sqlite3.Connection, names: TableNames, session_id: str, updated_at: int) -> None:
    """Set the session row's update time, through which background readers learn of news (``list_updated_sessions``)."""
    connection.execute(
        f"UPDATE {quote_identifier(names.sessions)} SET updated_at = ? WHERE session_id = ?",  # noqa: S608
        (updated_at, session_id),
    )


def logged_runs(connection: sqlite3.Connection, names: TableNames, session_id: str) -> list[LoggedRun]:
    """Return every top-level run of the session with its ``seq``, in ``seq`` order, each in its replay view.

    A run that finishes after the turns it waited for takes the next ``seq``, so it sorts after them.
    """
    condition, parameters = "run.session_id = ? AND run.parent_run_id IS NULL", (session_id,)
    seqs = connection.execute(
        f"SELECT seq FROM {quote_identifier(names.log_runs)} AS run WHERE {condition} ORDER BY seq",  # noqa: S608
        parameters,
    ).fetchall()
    runs = _read_runs(connection, names, condition, parameters)
    return [LoggedRun(run_seq, run) for (run_seq,), run in zip(seqs, runs, strict=True)]


def runs_without(runs: Iterable[AnyRunRecord], run_ids: Iterable[str]) -> list[AnyRunRecord]:
    """Return ``runs`` minus ``run_ids`` and their member runs; team members are agents, so none has members."""
    removed = set(run_ids)
    return [run for run in runs if run.run_id not in removed and run.parent_run_id not in removed]


def persist_to(
    store: ConversationStore,
    session: ConversationSession,
    *,
    replays: Callable[[AnyRunRecord], bool] | None = None,
) -> Callable[[AnyRunRecord], Awaitable[None]]:
    """Return the callback a run hands its terminal record to: save it to ``store`` with its usage, in the database's lane.

    A record saves as ``ConversationStore.save_run`` saves it with ``record_usage``, a turn later loads replay, unless
    ``replays`` rejects it: such a record is no turn, and only its usage is recorded (``_save_usage``).
    The record, with its usage snapshot, and ``session.session_state``, which the run's tools change in place, are
    copied on the caller's task before the lane is awaited, so what lands is what was handed over. The state is written
    back, in the record's own transaction, when it differs from ``session.stored_session_state``, which every write of
    the field records, so a turn whose runs or checkpoints change the state and restore it stores it restored.
    The save mirrors itself in ``session.runs`` in the lane, where the session's saves run one at a time; it completes
    even when the caller is cancelled, as the lane's operations do.
    """

    async def persist(record: AnyRunRecord) -> None:
        data, kind, usage = record.to_dict(), run_kind(record), run_usage_snapshot(record)
        turn = replays is None or replays(record)

        def save(opened: ConversationStore, state: dict[str, Any]) -> None:
            if turn:
                opened._save(session, data, kind, usage=usage, state=state)
            else:
                opened._save_usage(session, data, usage, state=state)

        await write_with_session_state(store, session, save)

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


def decoded_mapping(raw_value: object) -> dict[str, Any] | None:
    """Return a stored JSON object column, or None when it is null, unreadable, or not an object.

    An odd row therefore never fails a load or a conversion; JSON nested too deep to decode is unreadable too.
    """
    if not isinstance(raw_value, (str, bytes, bytearray)):
        return None
    try:
        value = decode_json(raw_value)
    except (RecursionError, ValueError):
        return None
    return cast("dict[str, Any]", value) if isinstance(value, dict) else None


def _session_state(raw_session_state: object) -> dict[str, Any]:
    """Return a row's ``session_state`` object, or an empty one when it holds none or no object."""
    return decoded_mapping(raw_session_state) or {}


def database_owner(connection: sqlite3.Connection, names: TableNames) -> HistoryScope:
    """Return the agent, team scope, or system usage ledger that owns the database, as its meta row records it."""
    kind, scope_id = connection.execute(
        f"SELECT owner_kind, owner_id FROM {quote_identifier(names.log_meta)} WHERE id = 1",  # noqa: S608
    ).fetchone()
    return HistoryScope(kind=kind, scope_id=scope_id)


def ensure_session_row(
    connection: sqlite3.Connection,
    names: TableNames,
    session: ConversationSession,
) -> dict[str, Any] | None:
    """Create ``session``'s row in the caller's write transaction unless one exists, keeping one another writer made.

    The row names no owner, since the database's owner owns every session in it. Returns the copy of
    ``session.session_state`` a created row holds, for the caller to record as ``session.stored_session_state`` once
    its transaction commits, or None when the row existed. A caller whose write created the row reports it once the
    write commits (``report_created_row``).
    """
    # One encode reads the live state, as ``session_state_snapshot`` does, and the created row's copy decodes from it.
    session_state = json.dumps(session.session_state)
    cursor = connection.execute(
        f"INSERT INTO {quote_identifier(names.sessions)} (session_id, session_state, metadata, created_at, "  # noqa: S608
        "updated_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT(session_id) DO NOTHING",
        (
            session.session_id,
            session_state,
            # No metadata is stored as the JSON text ``null``, never SQL NULL.
            json.dumps(session.metadata),
            session.created_at,
            session.created_at,
        ),
    )
    return json.loads(session_state) if cursor.rowcount else None


@dataclass(slots=True)
class _SessionRowWatch:
    """Whether a write ``watch_session_row`` watched created the row of session ``session_id`` in ``scope``."""

    scope: HistoryScope
    session_id: str
    created: bool = False


_SESSION_ROW_WATCH: ContextVar[_SessionRowWatch | None] = ContextVar("session_row_watch", default=None)


@contextmanager
def watch_session_row(scope: HistoryScope, session_id: str) -> Iterator[_SessionRowWatch]:
    """Watch the writes made in this block, and in the tasks and threads it starts, for one creating a session's row.

    The watch's ``created`` turns true once a write that created session ``session_id``'s row in ``scope`` commits.
    A write to a row that exists, one that rolls back, and one for another session or scope leave it alone.
    """
    watch = _SessionRowWatch(scope, session_id)
    token = _SESSION_ROW_WATCH.set(watch)
    try:
        yield watch
    finally:
        _SESSION_ROW_WATCH.reset(token)


def report_created_row(session: ConversationSession) -> None:
    """Tell the block watching this write that the write, now committed, created ``session``'s row."""
    watch = _SESSION_ROW_WATCH.get()
    if watch is not None and (watch.session_id, watch.scope) == (session.session_id, owner_scope(session)):
        watch.created = True


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
    """Replace the ``session_state`` of an existing session row."""
    connection.execute(
        f"UPDATE {quote_identifier(names.sessions)} SET session_state = ?, updated_at = ? WHERE session_id = ?",  # noqa: S608
        (json.dumps(state), int(time()), session_id),
    )


def _next_seq(connection: sqlite3.Connection, names: TableNames) -> int:
    """Hand out the database's next run ``seq`` in the caller's write transaction; the counter never goes back."""
    (seq,) = connection.execute(
        f"UPDATE {quote_identifier(names.log_meta)} SET next_seq = next_seq + 1 WHERE id = 1 RETURNING next_seq - 1",  # noqa: S608
    ).fetchone()
    return seq


def is_removed(
    connection: sqlite3.Connection,
    names: TableNames,
    session_id: str,
    run_id: str,
    parent_run_id: str | None,
) -> bool:
    """Return whether a cut removed run ``run_id``, or its team run ``parent_run_id``, from session ``session_id``."""
    removed = f"SELECT 1 FROM {quote_identifier(names.log_removed)} WHERE session_id = ? AND run_id IN (?, ?)"  # noqa: S608
    return connection.execute(removed, (session_id, run_id, parent_run_id)).fetchone() is not None


def _write_run(
    connection: sqlite3.Connection,
    names: TableNames,
    session_id: str,
    data: dict[str, Any],
    kind: SessionKind,
    *,
    interrupted: bool,
) -> _Saved:
    """Write one run from its stored dict by ``ConversationStore.save_run``'s rules; an interrupted one is not final."""
    run_id, parent_run_id, now = data["run_id"], data.get("parent_run_id"), int(time())
    if is_removed(connection, names, session_id, run_id, parent_run_id):
        return _Saved(run_id, "removed", data.get("created_at") or now, now)
    rows = {
        row[0]: _StoredRow(*row[1:])
        for row in connection.execute(
            f"SELECT run_id, parent_run_id, status, seq, created_at FROM {quote_identifier(names.log_runs)} "  # noqa: S608
            "WHERE session_id = ? AND run_id IN (?, ?)",
            (session_id, run_id, parent_run_id),
        )
    }
    stored, team_run = rows.get(run_id), rows.get(parent_run_id)
    if team_run is not None and team_run.parent_run_id is not None:
        msg = f"Run {run_id!r} names member run {parent_run_id!r} as its team run; team members have no members"
        raise ValueError(msg)
    # A top-level run, or a member's team run, whose stored status is visible is final; a member's own is not.
    judged = team_run if parent_run_id is not None else stored
    if not interrupted and judged is not None and judged.status not in _NON_VISIBLE:
        return _Saved(run_id, "final")
    if stored is None:
        moved, created_at, seq = True, data.get("created_at") or now, _next_seq(connection, names)
    else:
        # A top-level run saved visible over its paused, cancelled, or errored row finished after the turns it awaited.
        moved = parent_run_id is None and stored.status in _NON_VISIBLE and data["status"] not in _NON_VISIBLE
        created_at, seq = stored.created_at, _next_seq(connection, names) if moved else stored.seq
    upsert_run(connection, names, session_id, data, kind, seq=seq, created_at=created_at, updated_at=now)
    set_visible_messages(connection, names, run_id, data["messages"], data["status"])
    _adopt_requester(connection, names, session_id, data.get("user_id"))
    [written] = _read_runs(connection, names, "run.run_id = ?", (run_id,))
    return _Saved(run_id, "written", created_at, now, moved, written)


def upsert_run(
    connection: sqlite3.Connection,
    names: TableNames,
    session_id: str,
    data: Mapping[str, Any],
    kind: SessionKind,
    *,
    seq: int,
    created_at: int,
    updated_at: int,
) -> None:
    """Upsert the row and the tool rows of the run whose stored dict is ``data``.

    A stored row keeps its ``session_id`` and ``created_at``. An update rather than a replace, which would cascade the
    run's message rows away. Tool rows are keyed by their place in the list, so a save that appends a tool execution
    writes that one row; changed rows are rewritten and rows past the list's end deleted.
    """
    written = ("parent_run_id", *_RECORD_COLUMNS, "seq", "updated_at")
    run_id, parent_run_id, columns = data["run_id"], data.get("parent_run_id"), run_columns(data, kind)
    connection.execute(
        f"INSERT INTO {quote_identifier(names.log_runs)} (run_id, session_id, created_at, {', '.join(written)}) "  # noqa: S608
        f"VALUES ({', '.join('?' * (len(written) + 3))}) "
        f"ON CONFLICT(run_id) DO UPDATE SET {', '.join(f'{column} = excluded.{column}' for column in written)}",
        (run_id, session_id, created_at, parent_run_id, *astuple(columns), seq, updated_at),
    )
    tools, table = data.get("tools") or [], quote_identifier(names.log_tools)
    connection.executemany(
        f"INSERT INTO {table} (run_id, ord, data) VALUES (?, ?, ?) ON CONFLICT(run_id, ord) "  # noqa: S608
        "DO UPDATE SET data = excluded.data WHERE data IS NOT excluded.data",
        [(run_id, position, json.dumps(tool)) for position, tool in enumerate(tools)],
    )
    connection.execute(f"DELETE FROM {table} WHERE run_id = ? AND ord >= ?", (run_id, len(tools)))  # noqa: S608


def _join_covering_row(
    connection: sqlite3.Connection,
    names: TableNames,
    session_id: str,
    scope_key: str,
    seq: int,
    metadata: Mapping[str, Any],
) -> None:
    """Add a visible run's event ids to the summary row that covers it, so redacting a reply linked later still cuts it.

    The covering row is the scope's first row by ``id`` whose ``through`` reaches the run's ``seq``. A run at or behind
    the latest barrier's ``through`` is retired and joins none; for any other run, since ``through`` never decreases in
    ``id`` order, that row comes after the latest barrier and has text.
    """
    summaries = quote_identifier(names.log_summaries)
    covering = connection.execute(
        f"SELECT id, event_ids, seen_event_ids FROM {summaries} WHERE session_id = ?1 AND scope_key = ?2 "  # noqa: S608
        f"AND through >= ?3 AND ?3 > COALESCE((SELECT through FROM {summaries} WHERE session_id = ?1 "
        "AND scope_key = ?2 AND summary IS NULL ORDER BY id DESC LIMIT 1), 0) ORDER BY id LIMIT 1",
        (session_id, scope_key, seq),
    ).fetchone()
    if covering is not None:
        summary_id, stored_event_ids, stored_seen = covering
        event_ids, seen = summary_event_sets([metadata])
        connection.execute(
            f"UPDATE {summaries} SET event_ids = ?, seen_event_ids = ? WHERE id = ?",  # noqa: S608
            (
                json.dumps(sorted({*json.loads(stored_event_ids), *event_ids})),
                json.dumps(sorted({*json.loads(stored_seen), *seen})),
                summary_id,
            ),
        )


def set_visible_messages(
    connection: sqlite3.Connection,
    names: TableNames,
    run_id: str,
    messages: Sequence[Mapping[str, Any]],
    status: str,
) -> None:
    """Make the replay view of run ``run_id``, saved with ``status``, its message dicts, rewriting changed rows only.

    History copies are never stored, and system messages only while the run is paused, for its continuation. A row a
    fold took stays as it is, so it never replays again, and an unfolded row the list no longer holds is deleted. A
    list holding one message id twice raises rather than keeping one copy.
    """
    paused = status == RunStatus.paused.value
    kept = [
        message for message in messages if not message.get("from_history") and (paused or message["role"] != "system")
    ]
    ids = [message["id"] for message in kept]
    if len(set(ids)) < len(ids):
        msg = f"Run {run_id!r} holds a message id twice"
        raise ValueError(msg)
    table = quote_identifier(names.log_messages)
    connection.executemany(
        f"INSERT INTO {table} (run_id, message_id, ord, data) VALUES (?, ?, ?, ?) ON CONFLICT(run_id, message_id) "  # noqa: S608
        "DO UPDATE SET ord = excluded.ord, data = excluded.data "
        "WHERE folded_by IS NULL AND (ord, data) IS NOT (excluded.ord, excluded.data)",
        [(run_id, message["id"], position, json.dumps(message)) for position, message in enumerate(kept)],
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

    A paused team run's ``member_responses``, which its resume reads, are the member runs its requirements name, in
    first-mention order, each decoded apart from the member's own record so the two share nothing.
    """
    records = _decoded_runs(connection, names, f"{condition} ORDER BY run.seq", parameters)
    for record in records:
        if isinstance(record, TeamRunRecord) and record.status is RunStatus.paused:
            named = list(dict.fromkeys(item.member_run_id for item in record.requirements or () if item.member_run_id))
            members = _decoded_runs(connection, names, "run.parent_run_id = ?", (record.run_id,))
            by_id = {member.run_id: cast("RunRecord", member) for member in members}
            record.member_responses = [by_id[member_id] for member_id in named if member_id in by_id]
    return records


def _decoded_runs(
    connection: sqlite3.Connection,
    names: TableNames,
    condition: str,
    parameters: tuple[object, ...],
) -> list[AnyRunRecord]:
    """Decode the runs whose rows meet ``condition``, each with its unfolded message and tool rows in ``ord`` order.

    Each call parses its own copy of the JSON.
    """
    rows = connection.execute(
        f"SELECT run_id, session_id, parent_run_id, {', '.join(_RECORD_COLUMNS)} "  # noqa: S608
        f"FROM {quote_identifier(names.log_runs)} AS run WHERE {condition}",
        parameters,
    ).fetchall()
    run_ids = [run_id for run_id, *_ in rows]
    messages = _rows_by_run(connection, names.log_messages, run_ids, "folded_by IS NULL")
    tools = _rows_by_run(connection, names.log_tools, run_ids)
    return [
        run_record(run_id, session_id, parent_run_id, RunColumns(*columns), messages[run_id], tools[run_id])
        for run_id, session_id, parent_run_id, *columns in rows
    ]


def _rows_by_run(
    connection: sqlite3.Connection,
    table: str,
    run_ids: list[str],
    condition: str = "TRUE",
) -> dict[str, list[dict[str, Any]]]:
    """Decode the ``data`` of ``table``'s rows of the runs ``run_ids`` that meet ``condition``, by run, in ``ord`` order."""
    decoded: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for run_id, data in connection.execute(
        f"SELECT run_id, data FROM {quote_identifier(table)} WHERE {condition} "  # noqa: S608
        "AND run_id IN (SELECT value FROM json_each(?)) ORDER BY run_id, ord",
        (json.dumps(run_ids),),
    ):
        decoded[run_id].append(json.loads(data))
    return decoded


def _loads_sql(alias: str, summaries: str) -> str:
    """Return the SQL condition under which run ``alias`` loads as a top-level run.

    A paused, cancelled, or errored run loads wherever it sits; any other loads unless its scope's latest summary
    row, a barrier included, covers it. Every run of a scope without summary rows loads.
    """
    return (
        f"({alias}.parent_run_id IS NULL AND ({alias}.status IN ({NON_VISIBLE_RUN_STATUS_SQL}) OR {alias}.seq > COALESCE("  # noqa: S608
        f"(SELECT row.through FROM {summaries} AS row WHERE row.session_id = {alias}.session_id "
        f"AND row.scope_key = {alias}.scope_key ORDER BY row.id DESC LIMIT 1), 0)))"
    )


def _replay_summary(
    connection: sqlite3.Connection,
    summaries: str,
    session_id: str,
    owner_scope: str,
) -> ScopeSummary | None:
    """Return the text of the owner scope's latest summary row, which the session replays; a barrier has none."""
    latest = connection.execute(
        f"SELECT summary, created_at FROM {summaries} WHERE session_id = ? AND scope_key = ? "  # noqa: S608
        "ORDER BY id DESC LIMIT 1",
        (session_id, owner_scope),
    ).fetchone()
    if latest is None or latest[0] is None:
        return None
    return ScopeSummary(text=latest[0], updated_at=datetime.fromtimestamp(latest[1], UTC))


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


def summary_event_sets(run_metadata: Sequence[object]) -> tuple[set[str], set[str]]:
    """Return the ``event_ids`` and ``seen_event_ids`` a summary row keeps of the runs whose metadata it is given.

    ``event_ids``, which redactions match, hold what the runs answered and represent; ``seen_event_ids`` only what they
    represent.
    """
    seen = set().union(*map(seen_event_ids, run_metadata))
    return seen.union(*map(source_event_ids, run_metadata)), seen


def store_fold(
    connection: sqlite3.Connection,
    names: TableNames,
    session_id: str,
    run: AnyRunRecord,
    messages: Sequence[Message],
    member_run_ids: Collection[str],
    summary_id: int,
) -> None:
    """Store what fold summary ``summary_id`` took out of ``run``, the live running record, in the caller's transaction.

    The run takes the next ``seq``, so it sorts after the summary; one without a row gets a cancelled one with its live
    scalars and metadata, which a crash leaves as a run that does not replay. The folded ``messages``, and the run's
    member runs that ``member_run_ids`` names, are flagged with the summary, a stored message keeping its data, so none
    replays again. The caller creates the session row (``ensure_session_row``) before it inserts the summary row.
    """
    runs, now, seq = quote_identifier(names.log_runs), int(time()), _next_seq(connection, names)
    move = f"UPDATE {runs} SET seq = ? WHERE session_id = ? AND run_id = ?"  # noqa: S608
    if not connection.execute(move, (seq, session_id, run.run_id)).rowcount:
        data = {**run.to_dict(), "status": RunStatus.cancelled.value, "tools": None, "requirements": None}
        created_at, kind = run.created_at or now, run_kind(run)
        upsert_run(connection, names, session_id, data, kind, seq=seq, created_at=created_at, updated_at=now)
    folded = [message.to_dict() for message in messages]
    flag_folded(connection, names, session_id, run.run_id, folded, member_run_ids, summary_id)


def flag_folded(
    connection: sqlite3.Connection,
    names: TableNames,
    session_id: str,
    run_id: str,
    messages: Sequence[Mapping[str, Any]],
    member_run_ids: Collection[str],
    summary_id: int,
) -> None:
    """Flag the message dicts ``messages`` of stored run ``run_id`` and its member runs ``member_run_ids`` as folded.

    Summary ``summary_id`` took them, in the caller's transaction. A message the run does not hold yet is stored
    folded; one it holds keeps its data and gains the flag. Either way it takes its place in ``messages`` as its
    ``ord``, so each fold's messages read in the fold's order. A message or member run keeps the flag and place of the
    first fold that took it, and none replays again.
    """
    connection.executemany(
        f"INSERT INTO {quote_identifier(names.log_messages)} (run_id, message_id, ord, folded_by, data) "  # noqa: S608
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT(run_id, message_id) DO UPDATE SET folded_by = excluded.folded_by, "
        "ord = excluded.ord WHERE folded_by IS NULL",
        [
            (run_id, message["id"], position, summary_id, json.dumps(message))
            for position, message in enumerate(messages)
        ],
    )
    connection.execute(
        f"UPDATE {quote_identifier(names.log_runs)} SET folded_by = ? WHERE session_id = ? AND parent_run_id = ? "  # noqa: S608
        "AND folded_by IS NULL AND run_id IN (SELECT value FROM json_each(?))",
        (summary_id, session_id, run_id, json.dumps(sorted(member_run_ids))),
    )


def delete_runs_in_transaction(
    connection: sqlite3.Connection,
    names: TableNames,
    run_ids: Collection[str],
    *,
    session_id: str,
) -> set[str]:
    """Delete session ``session_id``'s runs ``run_ids`` with their members and messages; return the deleted ids.

    It records no removed run and keeps every summary row; a caller that cuts or retires history records the removed
    runs itself.
    """
    selected = json.dumps(sorted(run_ids))
    return {
        run_id
        for (run_id,) in connection.execute(
            f"DELETE FROM {quote_identifier(names.log_runs)} WHERE session_id = ?1 AND (run_id IN "  # noqa: S608
            "(SELECT value FROM json_each(?2)) OR parent_run_id IN (SELECT value FROM json_each(?2))) RETURNING run_id",
            (session_id, selected),
        ).fetchall()
    }


__all__ = [
    "BUSY_TIMEOUT_SECONDS",
    "ConversationStore",
    "LoggedRun",
    "SaveOutcome",
    "begun",
    "connect_by_name",
    "connect_conversation_database",
    "database_owner",
    "decoded_mapping",
    "delete_runs_in_transaction",
    "ensure_session_row",
    "flag_folded",
    "is_removed",
    "logged_runs",
    "persist_to",
    "report_created_row",
    "runs_without",
    "seen_event_ids",
    "set_visible_messages",
    "source_event_ids",
    "store_fold",
    "summary_event_sets",
    "upsert_run",
    "watch_session_row",
    "write_with_session_state",
]
