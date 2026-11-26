"""Background tool jobs, saved by whichever runtime generation currently owns them."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from . import approval_calls

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .backend import Transaction


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


class ToolJobOwnershipLostError(RuntimeError):
    """A newer runtime took over the tool jobs, so this one may no longer change them."""


class ToolJobExistsError(ValueError):
    """A job with this ID was already accepted."""


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
        INSERT INTO tool_jobs (job_id, job_json) VALUES (?, ?)
        ON CONFLICT (job_id) DO NOTHING
        RETURNING job_id
        """,
        (job_id, job_json),
    )
    if inserted is None:
        msg = "Tool job already exists."
        raise ToolJobExistsError(msg)


def _apply_save(transaction: Transaction, update: ToolJobSnapshotUpdate) -> None:
    transaction.execute(
        "UPDATE tool_jobs SET job_json = ?, result_payload_json = COALESCE(?, result_payload_json) WHERE job_id = ?",
        (update.job_json, update.result_payload_json, update.job_id),
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


_LOAD_ALL_SQL = "SELECT job_id, job_json FROM tool_jobs"


def load_all(transaction: Transaction) -> tuple[SavedToolJob, ...]:
    """Return every saved job in ID order."""
    # Scan sequentially and sort in Python instead of forcing secondary-index random seeks on large tables.
    rows = transaction.fetchall(_LOAD_ALL_SQL)
    jobs = [SavedToolJob(str(row["job_id"]), str(row["job_json"])) for row in rows]
    jobs.sort(key=lambda job: job.job_id)
    return tuple(jobs)


def load_payload(transaction: Transaction, job_id: str) -> str | None:
    """Return one job's outcome payload, or ``None`` when it has none or no longer exists."""
    row = transaction.fetchone("SELECT result_payload_json FROM tool_jobs WHERE job_id = ?", (job_id,))
    return None if row is None or row["result_payload_json"] is None else str(row["result_payload_json"])
