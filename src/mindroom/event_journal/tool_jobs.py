"""Background tool jobs, saved by whichever runtime generation currently owns them."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .backend import Transaction


@dataclass(frozen=True, slots=True)
class SavedToolJob:
    """One job's saved JSON snapshot."""

    job_id: str
    job_json: str


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


def _require_ownership(transaction: Transaction, runtime_generation: str) -> None:
    row = transaction.fetchone("SELECT runtime_generation FROM tool_job_owner WHERE singleton = ?", (True,))
    if row is None or row["runtime_generation"] != runtime_generation:
        msg = "Another tool job runtime took over this journal."
        raise ToolJobOwnershipLostError(msg)


def accept(transaction: Transaction, runtime_generation: str, job_id: str, job_json: str) -> None:
    """Save a newly accepted job, refusing an ID any runtime already accepted."""
    _require_ownership(transaction, runtime_generation)
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


def save(
    transaction: Transaction,
    runtime_generation: str,
    job_id: str,
    job_json: str,
    result_payload_json: str | None,
) -> None:
    """Replace an accepted job's snapshot, and its outcome payload when one is given."""
    _require_ownership(transaction, runtime_generation)
    transaction.execute(
        "UPDATE tool_jobs SET job_json = ?, result_payload_json = COALESCE(?, result_payload_json) WHERE job_id = ?",
        (job_json, result_payload_json, job_id),
    )


def delete(transaction: Transaction, runtime_generation: str, job_id: str) -> None:
    """Forget one job together with its payload."""
    _require_ownership(transaction, runtime_generation)
    transaction.execute("DELETE FROM tool_jobs WHERE job_id = ?", (job_id,))


def load_all(transaction: Transaction) -> tuple[SavedToolJob, ...]:
    """Return every saved job in ID order."""
    rows = transaction.fetchall("SELECT job_id, job_json FROM tool_jobs ORDER BY job_id/*bytes*/")
    return tuple(SavedToolJob(str(row["job_id"]), str(row["job_json"])) for row in rows)


def load_payload(transaction: Transaction, job_id: str) -> str | None:
    """Return one job's outcome payload, or ``None`` when it has none or no longer exists."""
    row = transaction.fetchone("SELECT result_payload_json FROM tool_jobs WHERE job_id = ?", (job_id,))
    return None if row is None or row["result_payload_json"] is None else str(row["result_payload_json"])
