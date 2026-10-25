"""The one-way conversion of a conversation database from the Agno layout to storage v2.

Every open of a database runs ``migrate_conversation_database``, and so does the startup sweep for every database under
the session state root. A database with ``<s>_log_meta`` is converted already and costs one read. Any other converts
in one write transaction, after the byte-for-byte backup beside it (``legacy_agno3_backup``), so a crash or an error
leaves the file as it was and the next open converts it again.

Worker code can write session directories, so every database is untrusted input: the conversion follows no link to the
file, decodes each run row on its own, and leaves out a row it cannot read, which the backup keeps.
"""

from __future__ import annotations

import json
import math
import sqlite3
from bisect import bisect_right
from contextlib import ExitStack, closing
from dataclasses import dataclass, field
from functools import partial
from itertools import count
from typing import TYPE_CHECKING, Any, cast

from mindroom.background_tasks import run_blocking_until_complete
from mindroom.constants import resolve_session_state_root
from mindroom.delegation.state import DELEGATION_STATE_KEY, DelegationState
from mindroom.history.legacy_compaction_state import adopt_summary_rows
from mindroom.llm.messages import Message
from mindroom.llm.runs import RunRecord, RunStatus, TeamRunRecord
from mindroom.logging_config import get_logger
from mindroom.session_storage.codec import run_kind
from mindroom.session_storage.legacy_agno3_backup import write_conversion_backup
from mindroom.session_storage.legacy_agno3_schema import (
    AGNO_SCHEMA_VERSIONS_TABLE,
    AgnoTableNames,
    agno_schema_sql,
    run_from_row,
)
from mindroom.session_storage.schema import SESSION_COLUMNS, TableNames, log_schema_sql, table_names
from mindroom.session_storage.store import (
    BUSY_TIMEOUT_SECONDS,
    connect_conversation_database,
    flag_folded,
    seen_event_ids,
    set_visible_messages,
    source_event_ids,
    upsert_run,
)
from mindroom.usage.legacy_usage_storage import seed_usage_ledger
from mindroom.usage.storage import quote_identifier

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.llm.runs import AnyRunRecord

logger = get_logger(__name__)

# Startup skips a locked database at once; its owner converts it with the full timeout when it next opens it.
_STARTUP_LOCK_TIMEOUT_SECONDS = 0.0


def migrate_conversation_database(path: Path, names: TableNames, *, timeout: float = BUSY_TIMEOUT_SECONDS) -> None:
    """Bring the database at ``path`` to storage v2, the one entry every open runs.

    A database with ``<s>_log_meta`` costs one read. Any other gets, in one ``BEGIN IMMEDIATE``: the backup when it
    holds any table, the Agno-era steps (usage ledger seeding, summary-row adoption with Agno 2 blob runs and
    positions), the conversion, the drop of the Agno tables and dead columns, and the v2 tables and meta row. A file
    in write-ahead-log mode returns to the rollback journal first, which checkpoints its log.
    """
    with ExitStack() as descriptors, closing(_connect_by_name(path, timeout)) as connection:
        if _is_converted(connection, names):
            return
        _leave_wal_mode(connection, path)
        connection.execute("BEGIN IMMEDIATE")
        try:
            # Asked again under the lock: another opener may have converted the file while this one waited, and backing
            # up the converted file would replace the original's backup.
            if not _is_converted(connection, names):
                _convert(connection, AgnoTableNames(names.storage_name), path, descriptors)
        except BaseException:
            connection.rollback()
            raise
        connection.commit()


async def migrate_conversation_storage(runtime_paths: RuntimePaths) -> None:
    """Convert every conversation database under the session state root before the runtime starts."""
    await run_blocking_until_complete(_migrate_databases, runtime_paths)


def _migrate_databases(runtime_paths: RuntimePaths) -> None:
    root = resolve_session_state_root(runtime_paths.storage_root, runtime_paths)
    directories = [*_directories(root / "agents"), *_directories(root / "teams")]
    for scope in _directories(root / "private_instances"):
        directories.extend(_directories(scope))
    for directory in directories:
        names = TableNames(directory.name)
        path = names.database_path(directory)
        try:
            # A store that never wrote has no database to convert; its first open creates one.
            if path.is_symlink() or path.exists():
                migrate_conversation_database(path, names, timeout=_STARTUP_LOCK_TIMEOUT_SECONDS)
        except Exception as error:
            # Worker code can write session databases, so one that is linked, unreadable, locked, or malformed must not
            # stop startup. Its owner converts it with the full lock timeout when it next opens it, or fails alone.
            logger.warning("conversation_conversion_skipped_store", path=str(path), error=str(error))


def _directories(root: Path) -> list[Path]:
    if root.is_symlink() or not root.is_dir():
        return []
    try:
        return [child for child in sorted(root.iterdir()) if not child.is_symlink() and child.is_dir()]
    except OSError as error:
        # Worker code can make a state directory unlistable; skip the stores beneath it instead of stopping startup.
        logger.warning("conversation_conversion_skipped_store", path=str(root), error=str(error))
        return []


def _connect_by_name(path: Path, timeout: float) -> sqlite3.Connection:
    """Connect to the file ``path`` names itself, never to a link's target, which ``sqlite3.connect`` would open or create.

    A link at the database or its sessions directory is refused before connecting. The name SQLite resolves for the
    file, every link followed, must then be ``path`` with only the directories above the sessions directory resolved,
    so a link swapped in at either place before SQLite resolves the path is refused before anything is written, though
    SQLite's open can have created an empty file at the link's target. Two residuals remain: a swap inside SQLite's own
    resolve-then-open window opens the link's target under the expected name, and a missing or empty file there gets
    the v2 tables (one with tables stops at the backup's no-follow walk); and links above the sessions directory are
    followed, as the store follows them.
    """
    if path.parent.is_symlink() or path.is_symlink():
        msg = f"Refusing to convert {path} to storage v2: the database or its directory is a link"
        raise ValueError(msg)
    expected = str(path.parent.parent.resolve() / path.parent.name / path.name)
    connection = connect_conversation_database(path, timeout=timeout)
    (opened,) = connection.execute("SELECT file FROM pragma_database_list WHERE name = 'main'").fetchone()
    if opened != expected:
        connection.close()
        msg = f"Refusing to convert {path} to storage v2: SQLite opened {opened} instead"
        raise ValueError(msg)
    return connection


def _is_converted(connection: sqlite3.Connection, names: TableNames) -> bool:
    query = "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?"
    return connection.execute(query, (names.log_meta,)).fetchone() is not None


def _leave_wal_mode(connection: sqlite3.Connection, path: Path) -> None:
    """Return a file another tool put in write-ahead-log mode to the rollback journal, checkpointing its log.

    MindRoom never sets the mode. Leaving it moves every commit into the file before the backup copies it, and no
    checkpoint can change the file under the conversion's lock.
    """
    try:
        mode = connection.execute("PRAGMA journal_mode = DELETE").fetchone()
    except sqlite3.OperationalError as error:
        mode = error
    if mode != ("delete",):
        msg = f"Converting {path} to storage v2 needs its write-ahead log checkpointed, which failed: {mode}"
        raise RuntimeError(msg)


def _convert(connection: sqlite3.Connection, names: AgnoTableNames, path: Path, descriptors: ExitStack) -> None:
    """Convert the database in the caller's write transaction; one without a sessions table only gets the v2 tables."""
    tables = table_names(connection)
    if tables:
        write_conversion_backup(connection, path, descriptors)
    if names.sessions in tables:
        seed_usage_ledger(connection, names)
        adopt_summary_rows(connection, names)
        # Every Agno table then exists, so each step reads its rows from it.
        for statement in agno_schema_sql(names):
            connection.execute(statement)
    for statement in log_schema_sql(names):
        connection.execute(statement)
    if names.sessions not in tables:
        return
    seqs = count(1)
    for (session_id,) in connection.execute(
        f"SELECT session_id FROM {quote_identifier(names.sessions)} ORDER BY session_id",  # noqa: S608
    ).fetchall():
        places = _convert_runs(connection, names, session_id, seqs, path)
        for row in _summary_rows(connection, names, session_id):
            _convert_summary(connection, names, session_id, row, places)
            _convert_segment(connection, names, session_id, row, places)
    _copy_removed(connection, names)
    _drop_agno_tables(connection, names)
    _drop_dead_session_columns(connection, names)
    _write_meta(connection, names, next(seqs))


@dataclass(frozen=True, slots=True)
class _SummaryRow:
    """One Agno-layout summary row, its segment decoded, or None when it has none or it does not decode."""

    id: int
    scope_key: str
    after_run_index: int
    summary: str | None
    legacy: int
    event_ids: str
    seen_event_ids: str
    created_at: int
    segment_run_id: str | None
    segment: dict[str, Any] | None

    def segment_list(self, key: str) -> list[dict[str, Any]]:
        """Return the segment's ``key`` entries that are objects."""
        entries = (self.segment or {}).get(key)
        return [entry for entry in entries if isinstance(entry, dict)] if isinstance(entries, list) else []


@dataclass(slots=True)
class _Run:
    """One run row to write: its record, the Agno ``run_index`` it sorts by, and its row's timestamps."""

    record: AnyRunRecord
    index: float
    created_at: int
    updated_at: int


@dataclass(slots=True)
class _Places:
    """The session's written runs in ``seq`` order: each one's ``seq`` and the ``run_index`` it sorted by."""

    indexes: list[float] = field(default_factory=list)
    seqs: list[int] = field(default_factory=list)
    run_ids: set[str] = field(default_factory=set)

    def add(self, run: _Run, seq: int) -> None:
        """Record that ``run`` went to ``seq``, the next one."""
        self.indexes.append(run.index)
        self.seqs.append(seq)
        self.run_ids.add(run.record.run_id)

    def through(self, after_run_index: int) -> int:
        """Return the largest ``seq`` of a run sorted at or before ``after_run_index``, else 0."""
        position = bisect_right(self.indexes, after_run_index)
        return self.seqs[position - 1] if position else 0


def _summary_rows(
    connection: sqlite3.Connection,
    names: AgnoTableNames,
    session_id: str,
    condition: str = "",
) -> Iterator[_SummaryRow]:
    """Read session ``session_id``'s summary rows that meet ``condition`` in ``id`` order, one segment at a time."""
    rows = connection.execute(
        "SELECT id, scope_key, after_run_index, summary, legacy, event_ids, seen_event_ids, created_at, "  # noqa: S608
        f"segment_run_id, CAST(segment AS TEXT) FROM {quote_identifier(names.summaries)} WHERE session_id = ? "
        f"{condition} ORDER BY id",
        (session_id,),
    )
    for *columns, segment in rows:
        decoded = _decoded(json.loads, segment) if segment is not None else None
        yield _SummaryRow(*columns, segment=cast("dict[str, Any]", decoded) if isinstance(decoded, dict) else None)


def _has_row(connection: sqlite3.Connection, names: AgnoTableNames, run_id: str) -> bool:
    query = f"SELECT 1 FROM {quote_identifier(names.runs)} WHERE run_id = ?"  # noqa: S608
    return connection.execute(query, (run_id,)).fetchone() is not None


# LEGACY_COMPAT: Agno 3 run rows, one `run_data` JSON document per run.
# Legacy format: `<s>_runs` (`agno_runs` for the storage name `agno`) rows ordered by `run_index`, each run's messages,
# tools, and a paused team run's embedded `member_responses` inside `run_data`; workflow rows and members of members.
# Last legacy release: run rows, the last release before storage v2; replacement: unreleased, `<s>_log_runs` rows.
# Workflow rows and members of members have no known MindRoom writer (unversioned external input).
# Handling: Per session, each agent or team row becomes a `<s>_log_runs` row with the next `seq` in the order a load
# read them, decoded through `run_from_row`, the row's ids winning; a row without a position goes last rather than
# first, so no summary row covers it, as none did; a member of a member is re-parented to its team run. A paused team
# run's embedded members replace or create their member rows, since a resume read them; a fold whose run has no row gets a
# cancelled one right after its latest summary row, with the fold's metadata and members. Workflow rows and rows that
# do not decode are left out, the latter logged by id; the backup keeps both.
# Coverage: tests/session_storage/test_legacy_agno3_conversion.py::test_runs_convert_in_load_order_with_their_row_ids,
# ::test_members_of_members_join_their_team_run, ::test_a_paused_team_runs_embedded_members_become_its_member_rows,
# ::test_a_fold_whose_run_has_no_row_gets_a_cancelled_row_after_its_summary,
# ::test_fold_runs_without_a_row_sort_by_their_latest_fold,
# ::test_workflow_rows_and_rows_that_do_not_decode_are_left_out,
# ::test_rows_worker_code_wrote_in_odd_shapes_convert_alone, and
# tests/storage_oracle/test_migrated.py::test_a_converted_input_shows_its_recording_but_for_named_masks.
def _convert_runs(
    connection: sqlite3.Connection,
    names: AgnoTableNames,
    session_id: str,
    seqs: Iterator[int],
    path: Path,
) -> _Places:
    """Write session ``session_id``'s run rows in load order, each with the next ``seq``, and return where each went.

    One run row is decoded at a time, besides the members paused team runs embed and the folds of runs without a row,
    so a long session converts in bounded memory.
    """
    runs = quote_identifier(names.runs)
    parents = dict(connection.execute(f"SELECT run_id, parent_run_id FROM {runs} WHERE session_id = ?", (session_id,)))  # noqa: S608
    embedded = _embedded_members(connection, names, session_id)
    pending = _fold_owners_without_rows(connection, names, session_id)
    places = _Places()

    def write(run: _Run) -> None:
        seq = next(seqs)
        places.add(run, seq)
        _write_run(connection, names, session_id, run, seq, path)

    # A load read a row without a position first, but no summary row ever covered it, so it always loaded; first here,
    # it would sort under every summary's ``through``, so it goes last, where none covers it.
    for run_id, parent_run_id, kind, run_data, run_index, created_at, updated_at in connection.execute(
        f"SELECT run_id, parent_run_id, run_type, CAST(run_data AS TEXT), run_index, created_at, "  # noqa: S608
        f"COALESCE(updated_at, created_at) FROM {runs} WHERE session_id = ? "
        "ORDER BY run_index IS NULL, run_index, created_at, run_id",
        (session_id,),
    ):
        index = math.inf if run_index is None else run_index
        # A fold's run without a row sorts after every run at or before its summary row, before every later one.
        while pending and pending[0].index < index:
            write(pending.pop(0))
        # A resume read the copy a paused team run embedded, not the member's own row, even one that does not decode.
        record = embedded.get(run_id)
        if record is None:
            record = _decoded_run(path, run_id, kind, run_data)
            if record is None:
                continue
            record.run_id, record.parent_run_id = run_id, _team_run(parents, parent_run_id)
        write(_Run(record, index, created_at, updated_at))
        if isinstance(record, TeamRunRecord) and record.status is RunStatus.paused:
            for member in record.member_responses:
                if not _has_row(connection, names, member.run_id):
                    member.parent_run_id = run_id
                    write(_Run(member, index, member.created_at, updated_at))
    for run in pending:
        write(run)
    return places


def _decoded_run(path: Path, run_id: str, kind: str, run_data: str) -> AnyRunRecord | None:
    """Decode one Agno run row, or None for a workflow row or one that does not decode, which is logged."""
    try:
        return run_from_row(kind, run_data)
    except Exception as error:
        # Worker code can write any row; one this release cannot read is left to the backup.
        logger.warning("conversation_run_left_out", path=str(path), run_id=run_id, error=str(error))
        return None


def _team_run(parents: Mapping[str, str | None], parent_run_id: str | None) -> str | None:
    """Return the top-level run above ``parent_run_id``: team members are agents, so a member of a member joins its team.

    Parents that form a cycle, which no writer stores, stop where the cycle closes.
    """
    seen: set[str] = set()
    while parent_run_id is not None and parents.get(parent_run_id) is not None and parent_run_id not in seen:
        seen.add(parent_run_id)
        parent_run_id = parents[parent_run_id]
    return parent_run_id


def _embedded_members(connection: sqlite3.Connection, names: AgnoTableNames, session_id: str) -> dict[str, RunRecord]:
    """Return the member runs session ``session_id``'s paused team runs embed, by id, as members of their team run."""
    embedded: dict[str, RunRecord] = {}
    for run_id, run_data in connection.execute(
        f"SELECT run_id, CAST(run_data AS TEXT) FROM {quote_identifier(names.runs)} WHERE session_id = ? "  # noqa: S608
        "AND run_type = 'team' AND status = ?",
        (session_id, RunStatus.paused.value),
    ):
        team = _decoded(partial(run_from_row, "team"), run_data)
        for member in team.member_responses if isinstance(team, TeamRunRecord) else ():
            member.parent_run_id = run_id
            embedded[member.run_id] = member
    return embedded


def _fold_owners_without_rows(connection: sqlite3.Connection, names: AgnoTableNames, session_id: str) -> list[_Run]:
    """Return a cancelled run for each fold whose running run has no row, each followed by its segments' new members.

    Each takes the place right after the latest summary row of its folds, as a fold stores a run without a row now.
    """
    folds: dict[str, list[_SummaryRow]] = {}
    without_row = (
        f"AND segment_run_id IS NOT NULL AND segment_run_id NOT IN (SELECT run_id FROM {quote_identifier(names.runs)})"  # noqa: S608
    )
    for row in _summary_rows(connection, names, session_id, without_row):
        folds.setdefault(cast("str", row.segment_run_id), []).append(row)
    runs: list[_Run] = []
    for run_id, owned in folds.items():
        latest = owned[-1]
        kind, _, owner = latest.scope_key.partition(":")
        metadata = (latest.segment or {}).get("metadata")
        decode = TeamRunRecord.from_dict if kind == "team" else RunRecord.from_dict
        record = decode(
            {
                "run_id": run_id,
                f"{kind}_id": owner,
                "status": RunStatus.cancelled.value,
                "created_at": latest.created_at,
                "metadata": metadata if isinstance(metadata, dict) else None,
            },
        )
        index = latest.after_run_index + 0.5
        runs.append(_Run(record, index, latest.created_at, latest.created_at))
        members = {member.get("run_id"): member for row in owned for member in row.segment_list("member_responses")}
        for member_id, data in members.items():
            if isinstance(member_id, str) and not _has_row(connection, names, member_id):
                member = _decoded(RunRecord.from_dict, {**data, "parent_run_id": run_id})
                if member is not None:
                    runs.append(_Run(member, index, member.created_at, latest.created_at))
    # Stable, so each run's new members stay right after it.
    return sorted(runs, key=lambda run: run.index)


def _write_run(
    connection: sqlite3.Connection,
    names: TableNames,
    session_id: str,
    run: _Run,
    seq: int,
    path: Path,
) -> None:
    data = run.record.to_dict()
    if isinstance(metadata := data.get("metadata"), dict):
        data["metadata"] = _current_delegation_snapshot(path, run.record.run_id, metadata)
    kind = run_kind(run.record)
    upsert_run(connection, names, session_id, data, kind, seq=seq, created_at=run.created_at, updated_at=run.updated_at)
    _convert_messages(connection, names, data)


# LEGACY_COMPAT: Run messages stored as a list inside an Agno 3 run row.
# Legacy format: `run_data.messages`, which may hold history copies (CLI checkpoints), system messages of runs that
# are not paused, and one message id twice.
# Last legacy release: history copies, the last release before storage v2; replacement: unreleased, one
# `<s>_log_messages` row each. System messages of runs that are not paused: no single verified cutoff. v2026.5.250
# (#1013) stopped the reply path's agent and team-scope stores writing them, but through v2026.10.498 an agent built
# without such a store opened an unstripped one (`agents.py` `_open_agent_session_storage`), and v2026.10.499 (#3022)
# is the first release where every save drops them. One message id twice: no known writer (unversioned external input).
# Handling: The list becomes message rows in its order under the save's rule: history copies are dropped, and system
# messages unless the run is paused; a repeated id gets one derived from its run and place, so no row is lost.
# Coverage: tests/session_storage/test_legacy_agno3_conversion.py::test_messages_convert_under_the_saves_rule and
# ::test_a_repeated_message_id_gets_a_derived_one.
def _convert_messages(connection: sqlite3.Connection, names: TableNames, data: dict[str, Any]) -> None:
    messages: list[dict[str, Any]] = data["messages"]
    taken = {message["id"] for message in messages}
    seen: set[str] = set()
    for position, message in enumerate(messages):
        if message["id"] in seen:
            derived = f"{data['run_id']}:{position}"
            while derived in taken:
                derived += "+"
            taken.add(derived)
            message["id"] = derived
        seen.add(message["id"])
    set_visible_messages(connection, names, data["run_id"], messages, data["status"])


# LEGACY_COMPAT: Delegation snapshots in run metadata missing fields later releases added.
# Legacy format: `metadata.mindroom_delegation` whose `children` and `pending_tool_sources[*].child` omit `agent_mode`
# (written through v2026.10.18), `requested_model_name` (through v2026.10.244), or `persona` (through v2026.10.230), or
# whose persona omits `skills` (through v2026.10.245) or `knowledge` (through v2026.10.263).
# Last legacy release: v2026.10.263; replacement: v2026.10.264 writes every field.
# Handling: Each converted run's snapshot is read through `DelegationState.from_metadata` and stored as it writes it
# back, every field present, so the readers' defaults for absent fields can go later; a snapshot the reader refuses
# stays as stored and is logged by run id.
# Coverage: tests/session_storage/test_legacy_agno3_conversion.py::test_delegation_snapshots_are_stored_whole and
# ::test_a_delegation_snapshot_the_reader_refuses_stays_as_stored.
def _current_delegation_snapshot(path: Path, run_id: str, metadata: dict[str, Any]) -> dict[str, Any]:
    if metadata.get(DELEGATION_STATE_KEY) is None:
        return metadata
    try:
        snapshot = DelegationState.from_metadata(metadata).to_dict()
    except Exception as error:
        logger.warning("conversation_delegation_snapshot_kept", path=str(path), run_id=run_id, error=str(error))
        return metadata
    return {**metadata, DELEGATION_STATE_KEY: snapshot}


# LEGACY_COMPAT: Agno-layout summary rows positioned by `after_run_index`.
# Legacy format: `<s>_summaries` rows keyed `(after_run_index, id)` against the runs' `run_index`, with `summary_model`,
# `segment_run_id`, and a mid-turn fold's `segment` payload; legacy rows at -1 and barriers without text.
# Last legacy release: the last release before storage v2; replacement: unreleased, `<s>_log_summaries` rows.
# Handling: Each row keeps its id, text, legacy flag, event sets, and time; `through` is the largest `seq` of a run
# sorted at or before its `after_run_index`, else 0, which keeps every run-to-row and row-to-row order; a fold's row
# names its run in `fold_run_id` and keeps the answered and seen ids of the segment's metadata as its provenance.
# Inserting the ids sets the id sequence past the largest; `summary_model` is dropped.
# Coverage: tests/session_storage/test_legacy_agno3_conversion.py::test_summary_rows_keep_their_order_against_the_runs,
# ::test_summary_ids_are_never_handed_out_again, and ::test_a_fold_row_keeps_its_runs_provenance.
def _convert_summary(
    connection: sqlite3.Connection,
    names: TableNames,
    session_id: str,
    row: _SummaryRow,
    places: _Places,
) -> None:
    metadata = (row.segment or {}).get("metadata")
    answered, seen = (
        (None, None)
        if row.segment_run_id is None
        else (json.dumps(sorted(source_event_ids(metadata))), json.dumps(sorted(seen_event_ids(metadata))))
    )
    connection.execute(
        f"INSERT INTO {quote_identifier(names.log_summaries)} (id, session_id, scope_key, through, summary, legacy, "  # noqa: S608
        "created_at, fold_run_id, event_ids, seen_event_ids, fold_event_ids, fold_seen_event_ids) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            row.id,
            session_id,
            row.scope_key,
            places.through(row.after_run_index),
            row.summary,
            row.legacy,
            row.created_at,
            row.segment_run_id,
            row.event_ids,
            row.seen_event_ids,
            answered,
            seen,
        ),
    )


# LEGACY_COMPAT: Mid-turn fold segments on Agno-layout summary rows.
# Legacy format: A fold row's `segment`: the running run's metadata, the folded message dicts, its tool executions, and
# the member runs ended so far; a load stripped the folded messages from the run's stored copy.
# Last legacy release: the last release before storage v2; replacement: unreleased, `folded_by` on message and run rows.
# Handling: In row order, each segment message the run's row lacks becomes a message row folded by that row, and one it
# holds gains the flag; a tool execution no folded message names becomes a folded tool-result message with its call
# id; the segment's members gain the flag on their rows, so the run's folded tool calls stay what they were. A segment
# whose run is not this session's converted row, or that does not decode, flags nothing.
# Coverage: tests/session_storage/test_legacy_agno3_conversion.py::test_a_segment_flags_what_its_fold_took,
# ::test_a_segment_tool_no_folded_message_names_stays_folded, ::test_a_segment_whose_run_is_not_converted_flags_nothing,
# and ::test_rows_worker_code_wrote_in_odd_shapes_convert_alone.
def _convert_segment(
    connection: sqlite3.Connection,
    names: TableNames,
    session_id: str,
    row: _SummaryRow,
    places: _Places,
) -> None:
    run_id = row.segment_run_id
    if run_id is None or run_id not in places.run_ids:
        return
    decoded = (_decoded(Message.from_dict, message) for message in row.segment_list("messages"))
    messages = [message.to_dict() for message in decoded if message is not None]
    named = {call.get("id") for message in messages for call in message.get("tool_calls") or ()}
    named |= {message.get("tool_call_id") for message in messages}
    for tool in row.segment_list("tools"):
        call_id = tool.get("tool_call_id")
        if isinstance(call_id, str) and call_id not in named:
            result = Message(
                role="tool",
                id=f"{run_id}:{row.id}:{call_id}",
                tool_call_id=call_id,
                tool_name=tool.get("tool_name"),
                created_at=row.created_at,
            )
            messages.append(result.to_dict())
    members = [member.get("run_id") for member in row.segment_list("member_responses")]
    member_ids = [member_id for member_id in members if isinstance(member_id, str)]
    flag_folded(connection, names, session_id, run_id, messages, member_ids, row.id)


def _decoded[Value, Record](decode: Callable[[Value], Record], data: Value) -> Record | None:
    """Return ``decode(data)`` of a value worker code may have written, or None when it does not decode."""
    try:
        return decode(data)
    except Exception:
        return None


# LEGACY_COMPAT: The Agno-layout removed-run table.
# Legacy format: `<s>_removed_runs`, the ids of runs a cut removed, which a late save must not write again.
# Last legacy release: the last release before storage v2; replacement: unreleased, `<s>_log_removed`.
# Handling: Every row of a stored session is copied, so a late save of a removed run still writes nothing.
# Coverage: tests/session_storage/test_legacy_agno3_conversion.py::test_removed_runs_stay_removed.
def _copy_removed(connection: sqlite3.Connection, names: AgnoTableNames) -> None:
    connection.execute(
        f"INSERT INTO {quote_identifier(names.log_removed)} (run_id, session_id) SELECT run_id, session_id "  # noqa: S608
        f"FROM {quote_identifier(names.removed_runs)} "
        f"WHERE session_id IN (SELECT session_id FROM {quote_identifier(names.sessions)})",
    )


# LEGACY_COMPAT: The Agno-layout run, summary-row, and removed-run tables, and Agno's own version table.
# Legacy format: `<s>_runs` (or `agno_runs`), `<s>_summaries`, and `<s>_removed_runs` with their indexes, and
# `agno_schema_versions`, where Agno recorded the layout version it created each table with.
# Last legacy release: the last release before storage v2; replacement: unreleased, the `<s>_log_*` tables.
# Handling: Dropped once copied, in the conversion's transaction; the backup keeps them.
# Coverage: tests/session_storage/test_legacy_agno3_conversion.py::test_a_converted_database_holds_only_the_v2_layout.
def _drop_agno_tables(connection: sqlite3.Connection, names: AgnoTableNames) -> None:
    for table in (names.runs, names.summaries, names.removed_runs):
        connection.execute(f"DROP TABLE {quote_identifier(table)}")
    connection.execute(f"DROP TABLE IF EXISTS {quote_identifier(AGNO_SCHEMA_VERSIONS_TABLE)}")


# LEGACY_COMPAT: Sessions-table columns only Agno wrote.
# Legacy format: `workflow_id`, `agent_data`, `team_data`, `workflow_data`, and `summary` on every Agno sessions table,
# Agno 2's cleared `runs` blob column, and the unused `created_at` index.
# Last legacy release: the last release before storage v2; replacement: unreleased, the nine columns storage v2 keeps.
# Handling: Every column outside `SESSION_COLUMNS` is dropped, and the index, so an older release started on the
# converted file finds its required columns missing and archives the sessions directory instead of writing into it.
# Coverage: tests/session_storage/test_legacy_agno3_conversion.py::test_a_converted_database_holds_only_the_v2_layout.
def _drop_dead_session_columns(connection: sqlite3.Connection, names: TableNames) -> None:
    sessions = quote_identifier(names.sessions)
    connection.execute(f"DROP INDEX IF EXISTS {quote_identifier(f'idx_{names.sessions}_created_at')}")
    for (column,) in connection.execute("SELECT name FROM pragma_table_info(?)", (names.sessions,)).fetchall():
        if column not in SESSION_COLUMNS:
            connection.execute(f"ALTER TABLE {sessions} DROP COLUMN {quote_identifier(column)}")


def _write_meta(connection: sqlite3.Connection, names: TableNames, next_seq: int) -> None:
    """Set the run counter one past the largest ``seq`` the conversion handed out."""
    connection.execute(f"UPDATE {quote_identifier(names.log_meta)} SET next_seq = ? WHERE id = 1", (next_seq,))  # noqa: S608


__all__ = [
    "migrate_conversation_database",
    "migrate_conversation_storage",
]
