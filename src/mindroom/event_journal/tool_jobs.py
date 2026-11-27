"""Background tool-job snapshots, indexed metadata, and single-runtime ownership in the event journal."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from . import approval_calls
from .schema import TOOL_JOB_METADATA_COLUMNS
from .turn_rows import fetch_in

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .backend import Row, Transaction
    from .upgrades import SchemaInspector

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
# Statuses after which execution has ended for good, with an outcome a reply can retrieve.
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "denied", "interrupted"})
_BACKFILL_BATCH_SIZE = 1000
_LOAD_ALL_SQL = "SELECT job_id, job_json FROM tool_jobs"
_BACKFILL_FIRST_PAGE_SQL = "SELECT job_id, job_json FROM tool_jobs ORDER BY job_id LIMIT ?"
_BACKFILL_NEXT_PAGE_SQL = "SELECT job_id, job_json FROM tool_jobs WHERE job_id > ? ORDER BY job_id LIMIT ?"

# LEGACY_COMPAT: tool_jobs rows stored before indexed metadata columns were added.
# Legacy format: tool_jobs had only (job_id, job_json, result_payload_json), forcing startup to scan every row.
# Last legacy release: v2026.11.671; replacement: unreleased, adds the 6 metadata columns and partial indexes.
# Handling: upgrade_tool_jobs_metadata adds the 6 metadata columns with defaults and backfills existing rows in
# bounded keyset-paginated batches, tolerating malformed legacy JSON so startup never crashes on backfill.
# Coverage: tests/test_event_journal_tool_jobs.py::test_legacy_tool_jobs_table_upgrades_and_backfills_metadata.
_METADATA_COLUMN_NAMES = tuple(name for name, _ddl in TOOL_JOB_METADATA_COLUMNS)
_METADATA_COLUMNS = frozenset(_METADATA_COLUMN_NAMES)
_METADATA_SET_SQL = ", ".join(f"{name} = ?" for name in _METADATA_COLUMN_NAMES)
_ACCEPT_COLUMNS_SQL = ", ".join(("job_id", "job_json", *_METADATA_COLUMN_NAMES))
_ACCEPT_PLACEHOLDERS_SQL = ", ".join("?" for _ in range(2 + len(_METADATA_COLUMN_NAMES)))


class ToolJobExistsError(ValueError):
    """A job with this ID was already accepted."""


class ToolJobOwnershipLostError(RuntimeError):
    """A newer runtime took over the tool jobs, so this one may no longer change them."""


@dataclass(frozen=True, slots=True)
class SavedToolJob:
    """One job's saved JSON snapshot."""

    job_id: str
    job_json: str


@dataclass(frozen=True, slots=True)
class ToolJobSnapshotUpdate:
    """One accepted job's snapshot and optional payload update to write in a transaction."""

    job_id: str
    job_json: str
    result_payload_json: str | None
    approval_end_reason: str | None


def _delegation_child(kind: object, adapter: object) -> dict[object, object] | None:
    child = adapter.get("child") if kind == "delegation" and isinstance(adapter, dict) else None
    return child if isinstance(child, dict) else None


def is_workflow_node_payload(kind: object, adapter: object) -> bool:
    """Return whether a tool-job payload represents one call of a workflow run."""
    child = _delegation_child(kind, adapter)
    return child is not None and bool(child.get("workflow_run_id"))


def job_delegation_id_from_payload(kind: object, adapter: object) -> str | None:
    """Return the delegation ID stored in a delegation job payload, or ``None``."""
    child = _delegation_child(kind, adapter)
    raw = None if child is None else child.get("delegation_id")
    return raw if isinstance(raw, str) else None


def _datetime_to_epoch_us(dt: datetime) -> int:
    """Convert a datetime to exact UTC epoch microseconds without float rounding or 64-bit overflow."""
    delta = (dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt) - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def _iso_to_epoch_us(value: object) -> int:
    if not isinstance(value, str):
        return 0
    try:
        return _datetime_to_epoch_us(datetime.fromisoformat(value))
    except ValueError:
        return 0


def _safe_text(value: object) -> tuple[str | None, bool]:
    if not isinstance(value, str):
        return None, True
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return None, False
    return value, True


def _extract_metadata(job_json: str) -> tuple[object, ...]:
    """Return the 6 indexed metadata column values from *job_json*, tolerating legacy or corrupt payloads."""
    try:
        raw = json.loads(job_json)
    except ValueError:
        raw = None
    if not isinstance(raw, dict):
        # Keep corrupt rows hot (cold=False) so startup validation still sees them.
        return (False, False, 0, None, None, None)
    kind = raw.get("kind") if isinstance(raw.get("kind"), str) else "tool"
    adapter, owner = raw.get("adapter"), raw.get("owner")
    raw_recipient = raw_room_id = None
    if isinstance(owner, dict):
        transport, agent = owner.get("transport_agent_name"), owner.get("agent_name")
        raw_recipient = (
            transport
            if isinstance(transport, str) and transport
            else (agent if isinstance(agent, str) and agent else None)
        )
        raw_room_id = owner.get("room_id")
    recipient, ok_recipient = _safe_text(raw_recipient)
    room_id, ok_room = _safe_text(raw_room_id)
    delegation_id, ok_deleg = _safe_text(job_delegation_id_from_payload(kind, adapter))
    cold = (
        isinstance(raw.get("job_id"), str)
        and isinstance(owner, dict)
        and ok_recipient
        and ok_room
        and ok_deleg
        and raw.get("status") in TERMINAL_STATUSES
        and raw.get("consumed") is True
        and kind != "workflow"
        and not is_workflow_node_payload(kind, adapter)
    )
    return (
        cold,
        raw.get("returned_inline") is True,
        _iso_to_epoch_us(raw.get("updated_at")),
        recipient,
        room_id,
        delegation_id,
    )


def _saved_jobs(rows: Sequence[Row]) -> tuple[SavedToolJob, ...]:
    return tuple(SavedToolJob(str(row["job_id"]), str(row["job_json"])) for row in rows)


def upgrade_tool_jobs_metadata(transaction: Transaction, inspector: SchemaInspector) -> None:
    """Add indexed metadata columns to an existing tool_jobs table and backfill them in bounded batches."""
    columns = inspector.columns("tool_jobs")
    if not columns or columns >= _METADATA_COLUMNS:
        return
    for name, ddl in TOOL_JOB_METADATA_COLUMNS:
        if name not in columns:
            transaction.execute(f"ALTER TABLE tool_jobs ADD COLUMN {name} {ddl}")
    after_job_id: str | None = None
    while True:
        rows = (
            transaction.fetchall(_BACKFILL_FIRST_PAGE_SQL, (_BACKFILL_BATCH_SIZE,))
            if after_job_id is None
            else transaction.fetchall(_BACKFILL_NEXT_PAGE_SQL, (after_job_id, _BACKFILL_BATCH_SIZE))
        )
        if not rows:
            break
        for row in rows:
            job_id = str(row["job_id"])
            transaction.execute(
                f"UPDATE tool_jobs SET {_METADATA_SET_SQL} WHERE job_id = ?",  # noqa: S608 - fixed columns
                (*_extract_metadata(str(row["job_json"])), job_id),
            )
        after_job_id = str(rows[-1]["job_id"])


def take_ownership(transaction: Transaction, runtime_generation: str) -> None:
    """Make one runtime generation the only writer of every tool job."""
    transaction.execute(
        """
        INSERT INTO tool_job_owner (singleton, runtime_generation)
        VALUES (?, ?)
        ON CONFLICT (singleton) DO UPDATE SET runtime_generation = excluded.runtime_generation
        """,
        (True, runtime_generation),
    )


def require_ownership(transaction: Transaction, runtime_generation: str) -> None:
    """Refuse unless this generation owns the jobs, holding the owner row until the transaction ends.

    The no-op update locks that row, so on PostgreSQL a takeover from another process cannot commit between this
    check and the write that follows it; SQLite's single writer already orders them.
    """
    row = transaction.fetchone(
        """
        UPDATE tool_job_owner SET runtime_generation = runtime_generation
        WHERE singleton = ? AND runtime_generation = ?
        RETURNING runtime_generation
        """,
        (True, runtime_generation),
    )
    if row is None:
        msg = "Another tool job runtime took over this journal."
        raise ToolJobOwnershipLostError(msg)


def accept(transaction: Transaction, runtime_generation: str, job_id: str, job_json: str) -> None:
    """Save a newly accepted job, refusing an ID that is still saved."""
    require_ownership(transaction, runtime_generation)
    sql = f"INSERT INTO tool_jobs ({_ACCEPT_COLUMNS_SQL}) VALUES ({_ACCEPT_PLACEHOLDERS_SQL}) ON CONFLICT (job_id) DO NOTHING RETURNING job_id"  # noqa: S608 - fixed columns
    if transaction.fetchone(sql, (job_id, job_json, *_extract_metadata(job_json))) is None:
        msg = "Tool job already exists."
        raise ToolJobExistsError(msg)


def save_many(
    transaction: Transaction,
    runtime_generation: str,
    updates: Sequence[ToolJobSnapshotUpdate],
) -> None:
    """Replace accepted jobs' snapshots in one owned transaction, denying pending cards for any that end."""
    if not updates:
        return
    require_ownership(transaction, runtime_generation)
    sql = (
        f"UPDATE tool_jobs SET job_json = ?, result_payload_json = COALESCE(?, result_payload_json), "  # noqa: S608 - fixed columns
        f"{_METADATA_SET_SQL} WHERE job_id = ?"
    )
    for update in updates:
        transaction.execute(
            sql,
            (update.job_json, update.result_payload_json, *_extract_metadata(update.job_json), update.job_id),
        )
        if update.approval_end_reason is not None:
            approval_calls.deny_pending(
                transaction,
                owner_id=approval_calls.tool_job_run_id(update.job_id),
                reason=update.approval_end_reason,
            )


def delete(transaction: Transaction, runtime_generation: str, job_id: str) -> None:
    """Forget one job together with its payload."""
    require_ownership(transaction, runtime_generation)
    transaction.execute("DELETE FROM tool_jobs WHERE job_id = ?", (job_id,))


def load_all(transaction: Transaction) -> tuple[SavedToolJob, ...]:
    """Return every saved job in ID order."""
    # Scan sequentially and sort in Python instead of forcing secondary-index random seeks on large tables.
    jobs = list(_saved_jobs(transaction.fetchall(_LOAD_ALL_SQL)))
    jobs.sort(key=lambda job: job.job_id)
    return tuple(jobs)


def load_startup(transaction: Transaction) -> tuple[SavedToolJob, ...]:
    """Return only active, unconsumed, workflow, or workflow-node jobs in ID order."""
    return _saved_jobs(
        transaction.fetchall("SELECT job_id, job_json FROM tool_jobs WHERE NOT cold ORDER BY job_id/*bytes*/"),
    )


def load_ended_rooms(transaction: Transaction) -> frozenset[tuple[str, str]]:
    """Return distinct (recipient, room_id) pairs retaining at least one cold terminal job."""
    rows = transaction.fetchall(
        "SELECT DISTINCT recipient, room_id FROM tool_jobs WHERE cold AND recipient IS NOT NULL AND room_id IS NOT NULL",
    )
    return frozenset((str(row["recipient"]), str(row["room_id"])) for row in rows)


def load_by_id(transaction: Transaction, job_id: str, *, cold_only: bool = False) -> SavedToolJob | None:
    """Return one saved job by its exact primary key, or None."""
    sql = (
        "SELECT job_id, job_json FROM tool_jobs WHERE job_id = ? AND cold"
        if cold_only
        else "SELECT job_id, job_json FROM tool_jobs WHERE job_id = ?"
    )
    row = transaction.fetchone(sql, (job_id,))
    return None if row is None else SavedToolJob(str(row["job_id"]), str(row["job_json"]))


def _prefix_upper_bound(prefix: str) -> str | None:
    trimmed = prefix.rstrip("\U0010ffff")
    if not trimmed:
        return None
    last = ord(trimmed[-1])
    return trimmed[:-1] + chr(0xE000 if last == 0xD7FF else last + 1)


def load_by_prefix(transaction: Transaction, prefix: str) -> tuple[SavedToolJob, ...]:
    """Return cold non-inline saved jobs whose ID starts with *prefix*, in byte order."""
    if not prefix:
        return ()
    upper = _prefix_upper_bound(prefix)
    upper_clause, params = (" AND job_id/*bytes*/ < ?", (prefix, upper)) if upper is not None else ("", (prefix,))
    sql = (
        "SELECT job_id, job_json FROM tool_jobs "  # noqa: S608 - fixed upper bound clause
        f"WHERE cold AND NOT returned_inline AND job_id/*bytes*/ >= ?{upper_clause} ORDER BY job_id/*bytes*/"
    )
    return _saved_jobs(transaction.fetchall(sql, params))


def load_listable(transaction: Transaction, *, recipient: str, room_id: str | None) -> tuple[SavedToolJob, ...]:
    """Return cold non-inline saved jobs belonging to *(recipient, room_id)*, newest first."""
    room_clause, params = (
        (" AND room_id IS NULL", (recipient,)) if room_id is None else (" AND room_id = ?", (recipient, room_id))
    )
    sql = (
        "SELECT job_id, job_json FROM tool_jobs "  # noqa: S608 - fixed room filter clause
        f"WHERE cold AND NOT returned_inline AND recipient = ?{room_clause} ORDER BY updated_at_us DESC, job_id/*bytes*/"
    )
    return _saved_jobs(transaction.fetchall(sql, params))


def load_expirable(
    transaction: Transaction,
    *,
    inline_before: datetime,
    ended_before: datetime,
) -> tuple[SavedToolJob, ...]:
    """Return cold terminal saved jobs older than the inline or non-inline retention cutoff."""
    sql = (
        "SELECT job_id, job_json FROM tool_jobs "
        "WHERE cold AND returned_inline = ? AND updated_at_us < ? ORDER BY updated_at_us, job_id/*bytes*/"
    )
    return _saved_jobs(
        [
            *transaction.fetchall(sql, (True, _datetime_to_epoch_us(inline_before))),
            *transaction.fetchall(sql, (False, _datetime_to_epoch_us(ended_before))),
        ],
    )


def load_by_delegation_ids(transaction: Transaction, delegation_ids: Sequence[str]) -> tuple[SavedToolJob, ...]:
    """Return saved delegation jobs matching any ID in *delegation_ids*."""
    sql = "SELECT job_id, job_json FROM tool_jobs WHERE delegation_id IS NOT NULL AND delegation_id IN ({in})"
    return _saved_jobs(fetch_in(transaction, sql, (), delegation_ids))


def load_payload(transaction: Transaction, job_id: str) -> str | None:
    """Return one job's outcome payload, or ``None`` when it has none or no longer exists."""
    row = transaction.fetchone("SELECT result_payload_json FROM tool_jobs WHERE job_id = ?", (job_id,))
    return None if row is None or row["result_payload_json"] is None else str(row["result_payload_json"])
