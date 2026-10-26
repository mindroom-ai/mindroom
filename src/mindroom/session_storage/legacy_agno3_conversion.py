"""The one-way conversion of a conversation database from the Agno layout to storage v2.

Every open of a database runs ``migrate_conversation_database``, and so does the startup sweep for every database under
the session state root. A database with ``<s>_log_meta`` is converted already and costs one read. Any other converts
in one write transaction, after the byte-for-byte backup beside it (``legacy_agno3_backup``), so a crash or an error
leaves the file as it was and the next open converts it again.

Worker code can write session directories, so every database is untrusted input: the conversion follows no link to the
file, reads text that is not UTF-8 with replacement characters, decodes each run row on its own, and leaves out a row it
cannot read, which the backup keeps.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import sys
import time
from bisect import bisect_right
from contextlib import ExitStack, closing
from dataclasses import dataclass, field
from functools import partial
from itertools import count
from typing import TYPE_CHECKING, Any, cast

from mindroom.background_tasks import run_blocking_until_complete
from mindroom.constants import resolve_session_state_root
from mindroom.history.legacy_compaction_state import adopt_summary_rows
from mindroom.history.legacy_stored_tool_calls import repair_legacy_tool_calls
from mindroom.history.types import HistoryScope
from mindroom.llm.messages import Message
from mindroom.llm.runs import RunRecord, RunStatus, TeamRunRecord
from mindroom.logging_config import get_logger
from mindroom.session_storage.codec import run_kind
from mindroom.session_storage.legacy_agno2_runs import decode_json, stored_timestamp
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
    decoded_mapping,
    flag_folded,
    seen_event_ids,
    set_visible_messages,
    source_event_ids,
    upsert_run,
)
from mindroom.usage.legacy_usage_storage import seed_usage_ledger
from mindroom.usage.storage import SYSTEM_USAGE_STORAGE_NAME, quote_identifier

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Mapping
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.llm.runs import AnyRunRecord, ToolExecution
    from mindroom.session_storage.types import SessionKind

logger = get_logger(__name__)

# Startup skips a locked database at once; its owner converts it with the full timeout when it next opens it.
_STARTUP_LOCK_TIMEOUT_SECONDS = 0.0
# The dropped Agno tables leave their pages free; compacting the file pays off once they are a quarter of it.
_COMPACT_FREE_SHARE = 0.25


def migrate_conversation_database(path: Path, names: TableNames, *, timeout: float = BUSY_TIMEOUT_SECONDS) -> None:
    """Bring the database at ``path`` to storage v2, the one entry every open runs.

    A database with ``<s>_log_meta`` costs one read. Any other gets, in one ``BEGIN IMMEDIATE``: the backup when it
    holds any table, the Agno-era steps (usage ledger seeding, summary-row adoption with Agno 2 blob runs and
    positions), the conversion, the drop of the Agno tables and dead columns, and the v2 tables and meta row. A file
    in write-ahead-log mode returns to the rollback journal first, which checkpoints its log. A converted file is then
    compacted when that pays off (``_compact``), and its conversion is logged.
    """
    with ExitStack() as descriptors, closing(_connect_by_name(path, timeout)) as connection:
        if _is_converted(connection, names):
            return
        _leave_wal_mode(connection, path)
        started = time.monotonic()
        connection.execute("BEGIN IMMEDIATE")
        converted: tuple[int, int] | None = None
        try:
            # Asked again under the lock: another opener may have converted the file while this one waited, and backing
            # up the converted file would replace the original's backup.
            if not _is_converted(connection, names):
                # SQLite keeps whatever bytes worker code stores as text; the conversion's own connection reads them.
                connection.text_factory = _TEXT_WITH_REPLACEMENT_CHARACTERS
                if _convert(connection, AgnoTableNames(names.storage_name), path, descriptors):
                    converted = connection.execute(
                        f"SELECT (SELECT count(*) FROM {quote_identifier(names.log_runs)}), "  # noqa: S608
                        f"(SELECT count(*) FROM {quote_identifier(names.log_messages)})",
                    ).fetchone()
        except BaseException:
            connection.rollback()
            raise
        connection.commit()
        if converted is None:
            return
        compacted = _compact(connection, path)
        logger.info(
            "conversation_converted",
            path=str(path),
            runs=converted[0],
            messages=converted[1],
            compacted=compacted,
            duration_seconds=round(time.monotonic() - started, 3),
        )


def _compact(connection: sqlite3.Connection, path: Path) -> bool:
    """``VACUUM`` the converted file once the dropped tables left a quarter of it free and its directory has room.

    ``VACUUM`` writes a rollback journal of the compacted size beside the file and a temporary copy of the same size,
    which shares the directory's filesystem unless SQLite's temporary directory lies elsewhere, so it needs twice the
    live pages free beyond what the conversion's own check covered. Without that room the file keeps its free pages,
    which later writes reuse. Returns whether the file was compacted.
    """
    page_size, pages, free = (
        connection.execute(f"PRAGMA {pragma}").fetchone()[0] for pragma in ("page_size", "page_count", "freelist_count")
    )
    if free < pages * _COMPACT_FREE_SHARE:
        return False
    status = os.statvfs(path.parent)
    if status.f_bavail * status.f_frsize < 2 * (pages - free) * page_size:
        return False
    try:
        connection.execute("VACUUM")
    except sqlite3.Error as error:
        # The conversion is committed, and a failed VACUUM leaves the file as it was, so the open goes on.
        logger.warning("conversation_compaction_failed", path=str(path), error=str(error))
        return False
    return True


async def migrate_conversation_storage(runtime_paths: RuntimePaths) -> None:
    """Convert every conversation database under the session state root before the runtime starts."""
    await run_blocking_until_complete(_migrate_databases, runtime_paths)


def _migrate_databases(runtime_paths: RuntimePaths) -> None:
    root = resolve_session_state_root(runtime_paths.storage_root, runtime_paths)
    directories = [*_directories(root / "agents"), *_directories(root / "teams")]
    for scope in _directories(root / "private_instances"):
        directories.extend(_directories(scope))
    # The system usage store, which records internal provider calls, is a conversation database of its own.
    system = root / SYSTEM_USAGE_STORAGE_NAME
    if not system.is_symlink() and system.is_dir():
        directories.append(system)
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
    file, every link followed, must then be ``path`` with only the directories above the sessions directory resolved, so
    a link swapped in at either place before SQLite resolves the path is refused before anything is written, though
    SQLite's open can have created an empty file at the link's target. Two residuals remain: a sessions directory
    swapped for a link inside SQLite's own resolve-then-open window opens the link's target under the expected name, and
    a missing or empty file there gets the v2 tables (one with tables stops at the backup's no-follow walk); and links
    above the sessions directory are followed, as the store follows them.
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


# Decodes text that is not UTF-8, which worker code can store, with replacement characters instead of failing the read.
_TEXT_WITH_REPLACEMENT_CHARACTERS = partial(str, encoding="utf-8", errors="replace")


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


def _convert(connection: sqlite3.Connection, names: AgnoTableNames, path: Path, descriptors: ExitStack) -> bool:
    """Convert the database in the caller's write transaction; one without a sessions table only gets the v2 tables.

    Returns whether the file held any table, so a new or empty file, which only gets the v2 tables, reports none.
    """
    tables = table_names(connection)
    if tables:
        write_conversion_backup(path, descriptors)
    if names.sessions in tables:
        _session_text_as_utf8(connection, names)
        seed_usage_ledger(connection, names)
        adopt_summary_rows(connection, names)
        _fold_force_requests(connection, names)
        # Every Agno table then exists, so each step reads its rows from it.
        for statement in agno_schema_sql(names):
            connection.execute(statement)
    for statement in log_schema_sql(names):
        connection.execute(statement)
    if names.sessions not in tables:
        return bool(tables)
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
    return True


def _session_text_as_utf8(connection: sqlite3.Connection, names: AgnoTableNames) -> None:
    """Store each session column's text that is not UTF-8 as the conversion reads it, so every later read decodes it.

    A session whose id is not UTF-8, which no read could name, is left out; the backup keeps it.
    """
    sessions = quote_identifier(names.sessions)
    rows = connection.execute(
        f"SELECT CAST(session_id AS BLOB), {', '.join(SESSION_COLUMNS)} FROM {sessions}",  # noqa: S608
    ).fetchall()
    for key, *values in rows:
        for column, value in zip(SESSION_COLUMNS, values, strict=True):
            # Only a value read with a replacement character can differ from what is stored.
            if not (isinstance(value, str) and "\ufffd" in value):
                continue
            where = f"WHERE CAST(session_id AS BLOB) IS ? AND {column} IS NOT ?"
            if column == "session_id":
                connection.execute(f"DELETE FROM {sessions} {where}", (key, value))  # noqa: S608
            else:
                connection.execute(f"UPDATE {sessions} SET {column} = ? {where}", (value, key, value))  # noqa: S608


@dataclass(frozen=True, slots=True)
class _SummaryRow:
    """One Agno-layout summary row, its segment decoded, or None when it has none or it does not decode."""

    id: int
    scope_key: str
    after_run_index: float
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

    def through(self, after_run_index: float) -> int:
        """Return the largest ``seq`` of a run sorted at or before ``after_run_index``, else 0."""
        position = bisect_right(self.indexes, after_run_index)
        return self.seqs[position - 1] if position else 0


def _summary_rows(
    connection: sqlite3.Connection,
    names: AgnoTableNames,
    session_id: str,
    condition: str = "",
) -> Iterator[_SummaryRow]:
    """Read session ``session_id``'s summary rows that meet ``condition`` in ``id`` order, one segment at a time.

    Worker code can write the table, so a value no reader of the converted row could use is replaced: a row whose scope
    key is not text, which no read names, is left out; a position that is not a number sorts after every numbered run
    and before every run without one, where SQLite sorted it for a load; a fold's run id that is not text names no run;
    event ids keep only their text entries; and a creation time that is not whole seconds a datetime holds becomes the
    conversion's.
    """
    rows = connection.execute(
        "SELECT id, scope_key, after_run_index, summary, legacy, event_ids, seen_event_ids, created_at, "  # noqa: S608
        f"segment_run_id, CAST(segment AS TEXT) FROM {quote_identifier(names.summaries)} WHERE session_id = ? "
        f"AND typeof(scope_key) = 'text' {condition} ORDER BY id",
        (session_id,),
    )
    now = int(time.time())
    for row_id, scope_key, after_run_index, summary, legacy, event_ids, seen, created_at, run_id, segment in rows:
        decoded = _decoded(decode_json, segment) if segment is not None else None
        yield _SummaryRow(
            row_id,
            scope_key,
            after_run_index if isinstance(after_run_index, int | float) else sys.float_info.max,
            summary,
            legacy,
            _text_event_ids(event_ids),
            _text_event_ids(seen),
            stored_timestamp(created_at, now),
            run_id if isinstance(run_id, str) else None,
            cast("dict[str, Any]", decoded) if isinstance(decoded, dict) else None,
        )


def _text_event_ids(raw: object) -> str:
    """Return a summary row's stored event ids as a JSON array of their text entries, the only ones MindRoom wrote."""
    decoded = _decoded(decode_json, raw) if isinstance(raw, str) else None
    event_ids = decoded if isinstance(decoded, list) else []
    return json.dumps([event_id for event_id in event_ids if isinstance(event_id, str)])


def _has_row(connection: sqlite3.Connection, names: AgnoTableNames, run_id: str) -> bool:
    query = f"SELECT 1 FROM {quote_identifier(names.runs)} WHERE run_id = ?"  # noqa: S608
    return connection.execute(query, (run_id,)).fetchone() is not None


# LEGACY_COMPAT: Agno 3 run rows, one `run_data` JSON document per run.
# Legacy format: `<s>_runs` (`agno_runs` for the storage name `agno`) rows ordered by `run_index`, each run's messages,
# tools, and a paused team run's embedded `member_responses` inside `run_data`; workflow rows and members of members.
# Last legacy release: run rows, v2026.10.569; replacement: the next release, `<s>_log_runs` rows with their
# `<s>_log_tools` rows.
# Workflow rows and members of members have no known MindRoom writer (unversioned external input).
# Handling: Per session, each agent or team row becomes a `<s>_log_runs` row with the next `seq` in the order a load
# read them, decoded through `run_from_row`, the row's ids winning, with a `<s>_log_tools` row per tool execution in
# order; a row without a position goes last rather than first, so no summary row covers it, as none did; a member of a
# member is re-parented to its team run. A paused team run's embedded members replace or create their member rows, since
# a resume read them; a fold whose run has no row and was not removed gets a cancelled one right after its latest
# summary row, with the fold's metadata and members. Workflow rows and rows that do not decode are left out, the latter
# logged by id; the backup keeps both. Worker code can write the run data, so a row whose run id is not text is left out
# too, with what a paused team row embeds, as is an embedded or folded member whose run id is not text; a position that
# is not a number goes last, as SQLite sorted it; a member's creation time that is not whole seconds a datetime holds
# becomes its team run row's or its fold's; and each message and tool keeps its call ids only as text or none
# (`_use_text_call_ids`).
# Coverage: tests/session_storage/test_legacy_agno3_conversion.py::test_runs_convert_in_load_order_with_their_row_ids,
# ::test_members_of_members_join_their_team_run, ::test_a_paused_team_runs_embedded_members_become_its_member_rows,
# ::test_a_fold_whose_run_has_no_row_gets_a_cancelled_row_after_its_summary,
# ::test_fold_runs_without_a_row_sort_by_their_latest_fold,
# ::test_workflow_rows_and_rows_that_do_not_decode_are_left_out,
# ::test_rows_worker_code_wrote_in_odd_shapes_convert_alone, ::test_members_with_unusable_values_convert_alone,
# ::test_converted_call_ids_are_text_or_none_so_folded_calls_read, ::test_a_run_row_whose_id_is_not_text_is_left_out,
# ::test_a_run_whose_position_is_not_a_number_goes_last_as_a_load_read_it,
# tests/storage_oracle/test_migrated.py::test_a_converted_input_shows_its_recording_but_for_named_masks, and
# tests/storage_oracle/test_migrated.py::test_a_fold_run_a_retirement_removed_converts_without_a_row.
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
        _write_run(connection, names, session_id, run, seq)

    # A load read a row without a position first, but no summary row ever covered it, so it always loaded; first here,
    # it would sort under every summary's ``through``, so it goes last, where none covers it.
    for run_id, parent_run_id, kind, run_data, run_index, created_at, updated_at in connection.execute(
        f"SELECT run_id, parent_run_id, run_type, CAST(run_data AS TEXT), run_index, created_at, "  # noqa: S608
        f"COALESCE(updated_at, created_at) FROM {runs} WHERE session_id = ? AND typeof(run_id) = 'text' "
        "ORDER BY run_index IS NULL, run_index, created_at, run_id",
        (session_id,),
    ):
        index = run_index if isinstance(run_index, int | float) else math.inf
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
            for member in _addressable_members(record):
                if not _has_row(connection, names, member.run_id):
                    member.parent_run_id, member.created_at = run_id, stored_timestamp(member.created_at, created_at)
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
        "AND typeof(run_id) = 'text' AND run_type = 'team' AND status = ?",
        (session_id, RunStatus.paused.value),
    ):
        team = _decoded(partial(run_from_row, "team"), run_data)
        for member in _addressable_members(team) if isinstance(team, TeamRunRecord) else ():
            member.parent_run_id = run_id
            embedded[member.run_id] = member
    return embedded


def _addressable_members(team: TeamRunRecord) -> list[RunRecord]:
    """Return the members a paused team run embeds that a resume can address: those whose run id is text."""
    return [member for member in team.member_responses if isinstance(member.run_id, str)]


def _fold_owners_without_rows(connection: sqlite3.Connection, names: AgnoTableNames, session_id: str) -> list[_Run]:
    """Return a cancelled run for each fold whose running run has no row, each followed by its segments' new members.

    Each takes the place right after the latest summary row of its folds, as a fold stores a run without a row now. A
    run a retirement removed gets none, as in a store that never converted: it never writes a row again, and the
    barrier keeps its folds out of replay and out of what a redaction matches.
    """
    folds: dict[str, list[_SummaryRow]] = {}
    without_row = (
        f"AND segment_run_id IS NOT NULL AND segment_run_id NOT IN (SELECT run_id FROM {quote_identifier(names.runs)}) "  # noqa: S608
        f"AND segment_run_id NOT IN (SELECT run_id FROM {quote_identifier(names.removed_runs)})"
    )
    for row in _summary_rows(connection, names, session_id, without_row):
        if row.segment_run_id is not None:
            folds.setdefault(row.segment_run_id, []).append(row)
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
        members = {
            member_id: member
            for row in owned
            for member in row.segment_list("member_responses")
            if isinstance(member_id := member.get("run_id"), str)
        }
        for member_id, data in members.items():
            if not _has_row(connection, names, member_id):
                member = _decoded(RunRecord.from_dict, {**data, "parent_run_id": run_id})
                if member is not None:
                    member.created_at = stored_timestamp(member.created_at, latest.created_at)
                    runs.append(_Run(member, index, member.created_at, latest.created_at))
    # Stable, so each run's new members stay right after it.
    return sorted(runs, key=lambda run: run.index)


# LEGACY_COMPAT: Agno 3 run dicts carry keys the owned run records dropped.
# Legacy format: `run_data` rows, a paused team run's embedded `member_responses`, and a fold segment's
# `member_responses` written by the Agno-backed runtime may hold `input`, `content_type`, `session_state`, `events`,
# `images`/`videos`/`audio`/`files`, `response_audio`, `citations`, `references`, `reasoning_*`, `followups`,
# `model_provider_data`, fork and queue lineage fields, and per-tool `requires_user_input`, `user_input_schema`,
# `user_feedback_schema`, `answered`, `approval_id`.
# Last legacy release: v2026.10.498; replacement: v2026.10.499 (#3022) writes only the keys the owned records model.
# Handling: Every run the conversion writes is stored as its decoded record's `to_dict`, so the keys are dropped and
# every key MindRoom reads survives; no runtime reader decodes an Agno-written run dict.
# Coverage: tests/session_storage/test_legacy_agno3_conversion.py::test_converted_runs_keep_only_the_keys_records_model,
# tests/llm/test_codecs.py::test_run_record_round_trips_agno_dict_minus_dropped_keys, and
# tests/llm/test_codecs.py::test_stored_fixture_dicts_round_trip_minus_dropped_keys.
def _write_run(
    connection: sqlite3.Connection,
    names: TableNames,
    session_id: str,
    run: _Run,
    seq: int,
) -> None:
    # The repair reads call ids, so it runs on messages whose ids are text or None.
    _use_text_call_ids(run.record.messages, run.record.tools or ())
    run.record.messages = repair_legacy_tool_calls(run.record.messages)
    data = run.record.to_dict()
    kind = run_kind(run.record)
    upsert_run(connection, names, session_id, data, kind, seq=seq, created_at=run.created_at, updated_at=run.updated_at)
    _convert_messages(connection, names, data)


# LEGACY_COMPAT: Run messages stored as a list inside an Agno 3 run row.
# Legacy format: `run_data.messages`, which may hold history copies (CLI checkpoints), system messages of runs that
# are not paused, and one message id twice.
# Last legacy release: history copies, v2026.10.569; replacement: the next release, one
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


# LEGACY_COMPAT: Agno-layout summary rows positioned by `after_run_index`.
# Legacy format: `<s>_summaries` rows keyed `(after_run_index, id)` against the runs' `run_index`, with `summary_model`,
# `segment_run_id`, and a mid-turn fold's `segment` payload; legacy rows at -1 and barriers without text.
# Last legacy release: v2026.10.569; replacement: the next release, `<s>_log_summaries` rows.
# Handling: Each row keeps its id, text, legacy flag, event sets, and time; `through` is the largest `seq` of a run
# sorted at or before its `after_run_index`, else 0, which keeps every run-to-row and row-to-row order; a fold's row
# names its run in `fold_run_id` and keeps the answered and seen ids of the segment's metadata as its provenance.
# Inserting the ids sets the id sequence past the largest; `summary_model` is dropped. Worker code can write the table,
# so a row whose scope key is not text, which no read names, is left out; a position that is not a number sorts after
# every numbered run and before every run without one, as SQLite sorted it for a load; a fold's run id that is not text
# names no run; event ids keep only their text entries; and a creation time that is not whole seconds a datetime holds
# becomes the conversion's.
# Coverage: tests/session_storage/test_legacy_agno3_conversion.py::test_summary_rows_keep_their_order_against_the_runs,
# ::test_summary_ids_are_never_handed_out_again, ::test_a_fold_row_keeps_its_runs_provenance,
# ::test_a_summary_row_whose_position_is_not_a_number_covers_every_numbered_run_as_a_load_read_it,
# ::test_a_summary_row_whose_scope_key_is_not_text_is_left_out, ::test_a_fold_whose_run_id_is_not_text_names_no_run,
# ::test_summary_event_ids_keep_only_their_text_entries, and
# ::test_a_summary_rows_unusable_creation_time_becomes_the_conversions.
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
# Last legacy release: v2026.10.569; replacement: the next release, `folded_by` on message and run rows.
# Handling: In row order, each segment message the run's row lacks becomes a message row folded by that row, and one it
# holds gains the flag; a tool execution no folded message names becomes a folded tool-result message with its call
# id; the segment's members gain the flag on their rows, so the run's folded tool calls stay what they were. A segment
# whose run is not this session's converted row, or that does not decode, flags nothing; its messages keep call ids
# only as text or none, as run messages do.
# Coverage: tests/session_storage/test_legacy_agno3_conversion.py::test_a_segment_flags_what_its_fold_took,
# ::test_a_segment_tool_no_folded_message_names_stays_folded, ::test_a_segment_whose_run_is_not_converted_flags_nothing,
# ::test_rows_worker_code_wrote_in_odd_shapes_convert_alone, ::test_segment_messages_name_only_text_call_ids, and
# ::test_converted_call_ids_are_text_or_none_so_folded_calls_read.
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
    folded = [message for message in decoded if message is not None]
    _use_text_call_ids(folded)
    named = {call.get("id") for message in folded for call in message.tool_calls or ()}
    named |= {message.tool_call_id for message in folded}
    messages = [message.to_dict() for message in folded]
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


def _use_text_call_ids(messages: Iterable[Message], tools: Iterable[ToolExecution] = ()) -> None:
    """Keep the decoded messages' and tools' call ids only as text or None, as MindRoom wrote them.

    Worker code can write run data, so a message's tool calls keep only their objects, and a call id of another type
    becomes None; every reader can then hash and compare the ids.
    """
    for message in messages:
        calls = message.tool_calls if isinstance(message.tool_calls, list) else []
        message.tool_calls = [_with_text_id(call) for call in calls if isinstance(call, dict)]
        message.tool_call_id = message.tool_call_id if isinstance(message.tool_call_id, str) else None
    for tool in tools:
        tool.tool_call_id = tool.tool_call_id if isinstance(tool.tool_call_id, str) else None


def _with_text_id(call: dict[str, Any]) -> dict[str, Any]:
    return call if isinstance(call.get("id"), str | None) else {**call, "id": None}


def _decoded[Value, Record](decode: Callable[[Value], Record], data: Value) -> Record | None:
    """Return ``decode(data)`` of a value worker code may have written, or None when it does not decode."""
    try:
        return decode(data)
    except Exception:
        return None


# LEGACY_COMPAT: Manual compaction requests mirrored into the tools' shared session state.
# Legacy format: `mindroom_pending_compaction_scope_keys` in a session row's `session_data.session_state`: the keys of
# the scopes a `compact_context` call asked to compact, written beside the metadata force flag, which each scope's next
# run read into that flag.
# Last legacy release: v2026.10.569; replacement: the next release, the metadata force flag alone.
# Handling: Each listed scope gets the force flag in the session's metadata, as its next run would have set it, and the
# key leaves the session state; an entry that names no scope requests nothing. The row's update time stays.
# Coverage: tests/session_storage/test_legacy_agno3_conversion.py::test_a_queued_compaction_request_becomes_the_metadata_flag,
# ::test_a_request_queued_before_the_summary_rows_existed_leaves_the_scopes_tombstones_to_their_step,
# ::test_a_queued_request_that_names_no_scope_requests_nothing, ::test_session_data_that_holds_no_state_object_converts_as_stored,
# and tests/storage_oracle/test_migrated.py::test_a_converted_end_state_shows_its_last_recorded_observables_but_for_named_masks.
_PENDING_FORCE_KEY = "mindroom_pending_compaction_scope_keys"


def _fold_force_requests(connection: sqlite3.Connection, names: AgnoTableNames) -> None:
    # The owner of the flag imports the store opener, which imports this module.
    from mindroom.history.storage import with_force_compaction  # noqa: PLC0415

    sessions = quote_identifier(names.sessions)
    for session_id, raw_data, raw_metadata in connection.execute(
        f"SELECT session_id, session_data, metadata FROM {sessions}",  # noqa: S608
    ).fetchall():
        data = decoded_mapping(raw_data)
        state = data.get("session_state") if data is not None else None
        if not isinstance(state, dict) or _PENDING_FORCE_KEY not in state:
            continue
        scopes = _requested_scopes(state.pop(_PENDING_FORCE_KEY))
        metadata = decoded_mapping(raw_metadata)
        for scope in scopes:
            metadata = with_force_compaction(metadata, scope)
        connection.execute(
            f"UPDATE {sessions} SET session_data = ?, metadata = ? WHERE session_id = ?",  # noqa: S608
            (json.dumps(data), json.dumps(metadata) if scopes else raw_metadata, session_id),
        )


def _requested_scopes(scope_keys: object) -> list[HistoryScope]:
    """Return the scopes ``scope_keys`` lists; a run read only its own scope's key, so no other entry requested one."""
    scopes: list[HistoryScope] = []
    for scope_key in scope_keys if isinstance(scope_keys, list) else []:
        kind, _, scope_id = scope_key.partition(":") if isinstance(scope_key, str) else ("", "", "")
        if kind in ("agent", "team") and scope_id:
            scopes.append(HistoryScope(kind=cast("SessionKind", kind), scope_id=scope_id))
    return scopes


# LEGACY_COMPAT: The Agno-layout removed-run table.
# Legacy format: `<s>_removed_runs`, the ids of runs a cut removed, which a late save must not write again.
# Last legacy release: v2026.10.569; replacement: the next release, `<s>_log_removed`.
# Handling: Every row of a stored session is copied, so a late save of a removed run still writes nothing; an id that is
# not UTF-8 is copied as the conversion reads it, with replacement characters, and two such ids can read alike.
# Coverage: tests/session_storage/test_legacy_agno3_conversion.py::test_removed_runs_stay_removed and
# ::test_a_removed_run_whose_id_is_not_utf8_is_stored_as_the_conversion_reads_it.
def _copy_removed(connection: sqlite3.Connection, names: AgnoTableNames) -> None:
    rows = connection.execute(
        f"SELECT run_id, session_id FROM {quote_identifier(names.removed_runs)} "  # noqa: S608
        f"WHERE session_id IN (SELECT session_id FROM {quote_identifier(names.sessions)})",
    ).fetchall()
    connection.executemany(
        f"INSERT OR IGNORE INTO {quote_identifier(names.log_removed)} (run_id, session_id) VALUES (?, ?)",  # noqa: S608
        rows,
    )


# LEGACY_COMPAT: The Agno-layout run, summary-row, and removed-run tables, and Agno's own version table.
# Legacy format: `<s>_runs` (or `agno_runs`), `<s>_summaries`, and `<s>_removed_runs` with their indexes, and
# `agno_schema_versions`, where Agno recorded the layout version it created each table with.
# Last legacy release: v2026.10.569; replacement: the next release, the `<s>_log_*` tables.
# Handling: Dropped once copied, in the conversion's transaction; the backup keeps them.
# Coverage: tests/session_storage/test_legacy_agno3_conversion.py::test_a_converted_database_holds_only_the_v2_layout.
def _drop_agno_tables(connection: sqlite3.Connection, names: AgnoTableNames) -> None:
    for table in (names.runs, names.summaries, names.removed_runs):
        connection.execute(f"DROP TABLE {quote_identifier(table)}")
    connection.execute(f"DROP TABLE IF EXISTS {quote_identifier(AGNO_SCHEMA_VERSIONS_TABLE)}")


# LEGACY_COMPAT: Sessions-table columns only Agno wrote.
# Legacy format: `workflow_id`, `agent_data`, `team_data`, `workflow_data`, and `summary` on every Agno sessions table,
# Agno 2's cleared `runs` blob column, and the unused `created_at` index.
# Last legacy release: v2026.10.569; replacement: the next release, the nine columns storage v2 keeps.
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
