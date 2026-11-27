"""Background tool-job snapshots, indexed metadata, and single-runtime ownership in the event journal."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from . import approval_calls
from .turn_rows import fetch_in

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .backend import Row, Transaction
    from .upgrades import SchemaInspector

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "denied", "interrupted"})
_BACKFILL_BATCH_SIZE = 1000
_LOAD_ALL_SQL = "SELECT job_id, job_json FROM tool_jobs"

# LEGACY_COMPAT: tool_jobs rows stored before indexed metadata columns were added.
# Legacy format: tool_jobs had only (job_id, job_json, result_payload_json), forcing startup to scan every row.
# Handling: upgrade_tool_jobs_metadata adds the 6 metadata columns with defaults and backfills existing rows in
# bounded keyset-paginated batches, tolerating malformed legacy JSON so startup never crashes on backfill.
_METADATA_COLUMN_DEFS: tuple[tuple[str, str], ...] = (
    ("cold", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("returned_inline", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("updated_at_us", "BIGINT NOT NULL DEFAULT 0"),
    ("recipient", "TEXT"),
    ("room_id", "TEXT"),
    ("delegation_id", "TEXT"),
)
_METADATA_COLUMNS = frozenset(name for name, _ddl in _METADATA_COLUMN_DEFS)
_METADATA_SET_SQL = "cold = ?, returned_inline = ?, updated_at_us = ?, recipient = ?, room_id = ?, delegation_id = ?"


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
    result_payload_json: str | None = None
    approval_end_reason: str | None = None


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


def _extract_metadata(job_json: str) -> tuple[object, ...]:
    """Return the 6 indexed metadata column values from *job_json*, tolerating legacy or corrupt payloads."""
    try:
        raw = json.loads(job_json)
    except ValueError:
        raw = None
    if not isinstance(raw, dict):
        # Keep corrupt rows hot (cold=False) so startup validation still sees them.
        return (False, False, 0, None, None, None)
    status = raw.get("status") if isinstance(raw.get("status"), str) else ""
    consumed = raw.get("consumed") is True
    returned_inline = raw.get("returned_inline") is True
    kind = raw.get("kind") if isinstance(raw.get("kind"), str) else "tool"
    adapter = raw.get("adapter")
    child = adapter.get("child") if isinstance(adapter, dict) else None
    is_workflow = kind == "workflow" or (
        kind == "delegation" and isinstance(child, dict) and bool(child.get("workflow_run_id"))
    )
    cold = status in _TERMINAL_STATUSES and consumed and not is_workflow
    owner = raw.get("owner")
    recipient: str | None = None
    room_id: str | None = None
    if isinstance(owner, dict):
        transport, agent = owner.get("transport_agent_name"), owner.get("agent_name")
        recipient = (
            transport
            if isinstance(transport, str) and transport
            else (agent if isinstance(agent, str) and agent else None)
        )
        room_id = owner.get("room_id") if isinstance(owner.get("room_id"), str) else None
    raw_deleg = child.get("delegation_id") if kind == "delegation" and isinstance(child, dict) else None
    delegation_id = raw_deleg if isinstance(raw_deleg, str) else None
    return (
        cold,
        returned_inline,
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
    for name, ddl in _METADATA_COLUMN_DEFS:
        if name not in columns:
            transaction.execute(f"ALTER TABLE tool_jobs ADD COLUMN {name} {ddl}")
    after_job_id: str | None = None
    while True:
        cursor_clause, params = (
            ("", (_BACKFILL_BATCH_SIZE,))
            if after_job_id is None
            else (" WHERE job_id > ?", (after_job_id, _BACKFILL_BATCH_SIZE))
        )
        rows = transaction.fetchall(
            f"SELECT job_id, job_json FROM tool_jobs{cursor_clause} ORDER BY job_id LIMIT ?",  # noqa: S608
            params,
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
    inserted = transaction.fetchone(
        """
        INSERT INTO tool_jobs (
            job_id, job_json, cold, returned_inline, updated_at_us, recipient, room_id, delegation_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (job_id) DO NOTHING
        RETURNING job_id
        """,
        (job_id, job_json, *_extract_metadata(job_json)),
    )
    if inserted is None:
        msg = "Tool job already exists."
        raise ToolJobExistsError(msg)


def _apply_save(transaction: Transaction, update: ToolJobSnapshotUpdate) -> None:
    transaction.execute(
        f"""
        UPDATE tool_jobs
        SET job_json = ?, result_payload_json = COALESCE(?, result_payload_json), {_METADATA_SET_SQL}
        WHERE job_id = ?
        """,  # noqa: S608 - fixed columns
        (update.job_json, update.result_payload_json, *_extract_metadata(update.job_json), update.job_id),
    )
    if update.approval_end_reason is not None:
        approval_calls.deny_pending(
            transaction,
            owner_id=approval_calls.tool_job_run_id(update.job_id),
            reason=update.approval_end_reason,
        )


def save(
    transaction: Transaction,
    runtime_generation: str,
    job_id: str,
    job_json: str,
    result_payload_json: str | None,
    *,
    approval_end_reason: str | None,
) -> None:
    """Replace an accepted job's snapshot, and its outcome payload when one is given.

    A snapshot that ends the job's approvals, its end or its cancel request, denies the calls its approval cards still
    ask about with ``approval_end_reason`` and queues the cards' terminal edits, so no card of an ending job stays open.
    """
    save_many(
        transaction,
        runtime_generation,
        (ToolJobSnapshotUpdate(job_id, job_json, result_payload_json, approval_end_reason),),
    )


def save_many(
    transaction: Transaction,
    runtime_generation: str,
    updates: Sequence[ToolJobSnapshotUpdate],
) -> None:
    """Replace accepted jobs' snapshots in one owned transaction, denying pending cards for any that end."""
    if not updates:
        return
    require_ownership(transaction, runtime_generation)
    for update in updates:
        _apply_save(transaction, update)


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
        transaction.fetchall(
            """
            SELECT job_id, job_json FROM tool_jobs
            WHERE NOT cold
            ORDER BY job_id/*bytes*/
            """,
        ),
    )


def load_ended_rooms(transaction: Transaction) -> frozenset[tuple[str, str]]:
    """Return distinct (recipient, room_id) pairs retaining at least one cold terminal job."""
    rows = transaction.fetchall(
        """
        SELECT DISTINCT recipient, room_id FROM tool_jobs
        WHERE cold AND recipient IS NOT NULL AND room_id IS NOT NULL
        """,
    )
    return frozenset((str(row["recipient"]), str(row["room_id"])) for row in rows)


def load_by_id(transaction: Transaction, job_id: str) -> SavedToolJob | None:
    """Return one saved job by its exact primary key, or None."""
    row = transaction.fetchone("SELECT job_id, job_json FROM tool_jobs WHERE job_id = ?", (job_id,))
    return None if row is None else SavedToolJob(str(row["job_id"]), str(row["job_json"]))


def _prefix_upper_bound(prefix: str) -> str | None:
    last = ord(prefix[-1])
    if last >= 0x10FFFF:
        return None
    return prefix[:-1] + chr(0xE000 if last == 0xD7FF else last + 1)


def load_by_prefix(transaction: Transaction, prefix: str) -> tuple[SavedToolJob, ...]:
    """Return cold non-inline saved jobs whose ID starts with *prefix*, in byte order."""
    if not prefix:
        return ()
    upper = _prefix_upper_bound(prefix)
    upper_clause, params = (" AND job_id/*bytes*/ < ?", (prefix, upper)) if upper is not None else ("", (prefix,))
    return _saved_jobs(
        transaction.fetchall(
            f"""
            SELECT job_id, job_json FROM tool_jobs
            WHERE cold AND NOT returned_inline AND job_id/*bytes*/ >= ?{upper_clause}
            ORDER BY job_id/*bytes*/
            """,  # noqa: S608 - fixed upper bound clause
            params,
        ),
    )


def load_listable(transaction: Transaction, *, recipient: str, room_id: str | None) -> tuple[SavedToolJob, ...]:
    """Return cold non-inline saved jobs belonging to *(recipient, room_id)*, newest first."""
    room_clause, params = (
        (" AND room_id IS NULL", (recipient,)) if room_id is None else (" AND room_id = ?", (recipient, room_id))
    )
    return _saved_jobs(
        transaction.fetchall(
            f"""
            SELECT job_id, job_json FROM tool_jobs
            WHERE cold AND NOT returned_inline AND recipient = ?{room_clause}
            ORDER BY updated_at_us DESC, job_id/*bytes*/
            """,  # noqa: S608 - fixed room filter clause
            params,
        ),
    )


def load_expirable(
    transaction: Transaction,
    *,
    inline_before: datetime,
    ended_before: datetime,
) -> tuple[SavedToolJob, ...]:
    """Return cold terminal saved jobs older than the inline or non-inline retention cutoff."""
    sql = """
        SELECT job_id, job_json FROM tool_jobs
        WHERE cold AND returned_inline = ? AND updated_at_us < ?
        ORDER BY updated_at_us, job_id/*bytes*/
    """
    return _saved_jobs(
        [
            *transaction.fetchall(sql, (True, _datetime_to_epoch_us(inline_before))),
            *transaction.fetchall(sql, (False, _datetime_to_epoch_us(ended_before))),
        ],
    )


def load_by_delegation_ids(transaction: Transaction, delegation_ids: Sequence[str]) -> tuple[SavedToolJob, ...]:
    """Return saved delegation jobs matching any ID in *delegation_ids*."""
    if not delegation_ids:
        return ()
    return _saved_jobs(
        fetch_in(
            transaction,
            "SELECT job_id, job_json FROM tool_jobs WHERE delegation_id IS NOT NULL AND delegation_id IN ({in})",
            (),
            delegation_ids,
        ),
    )


def load_payload(transaction: Transaction, job_id: str) -> str | None:
    """Return one job's outcome payload, or ``None`` when it has none or no longer exists."""
    row = transaction.fetchone("SELECT result_payload_json FROM tool_jobs WHERE job_id = ?", (job_id,))
    return None if row is None or row["result_payload_json"] is None else str(row["result_payload_json"])
