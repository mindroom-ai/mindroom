"""Durable turn records, in the database that settles the turns they describe.

"Has this turn finished?" is answered today by two records in two substrates:
the journal's pending set and a JSON-file ledger. They cannot share a
transaction, so they settle at different moments, and every reader that needs
a trustworthy answer has to consult both and know why.

This is the first half of collapsing that: the same records, stored where a
settlement can be written in the same transaction. Nothing reads these rows
yet. The ledger keeps its in-memory map and its own file until the readers and
writers move across, which is deliberate -- a dedupe substrate that is half
migrated is one that can answer a message twice.

Two decisions are worth stating because they are easy to get backwards.

The scope is the agent, not the journal principal. Every other table here is
per (agent, Matrix identity), because what it holds is only meaningful beside
the sync that produced it. A turn record is the opposite: it is the proof that
a message was already answered, and a bot that re-logs in under a new Matrix ID
must not lose that proof and answer everything a second time. Transactionality
comes from sharing the database, not from sharing the scope key.

And a record is stored once per event that indexes it, rather than once per
turn. A coalesced batch answers several sources with one turn and is reachable
from any of them, which is exactly how the ledger's map behaves; storing it by
anchor alone would make "was this source answered?" a scan.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .backend import Transaction

_COLUMNS = "index_event_id, anchor_event_id, record_json"


def upsert(
    transaction: Transaction,
    agent_name: str,
    *,
    index_event_ids: Sequence[str],
    anchor_event_id: str,
    record_json: str,
) -> None:
    """Store one turn record under every event that indexes it.

    Rows this turn used to be indexed by and no longer is are removed first. A
    turn's indexed set shrinks when sources are dropped from a coalesced batch,
    and a stale row left behind would answer "already handled" for a source
    this turn no longer accounts for -- which is the one direction that
    silently drops a user's message.
    """
    if not index_event_ids:
        return
    placeholders = ", ".join("?" for _ in index_event_ids)
    transaction.execute(
        f"""
        DELETE FROM turn_records
        WHERE agent_name = ? AND anchor_event_id = ? AND index_event_id NOT IN ({placeholders})
        """,  # noqa: S608 - placeholders are generated, values are still bound
        (agent_name, anchor_event_id, *index_event_ids),
    )
    updated_at_ns = time.time_ns()
    for index_event_id in index_event_ids:
        transaction.execute(
            """
            INSERT INTO turn_records (
                agent_name, index_event_id, anchor_event_id, record_json, updated_at_ns
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (agent_name, index_event_id) DO UPDATE SET
                anchor_event_id = excluded.anchor_event_id,
                record_json = excluded.record_json,
                updated_at_ns = excluded.updated_at_ns
            """,
            (agent_name, index_event_id, anchor_event_id, record_json, updated_at_ns),
        )


def load(transaction: Transaction, agent_name: str, *, event_id: str) -> str | None:
    """Return the stored record indexed by one event, if there is one."""
    row = transaction.fetchone(
        f"""
        SELECT {_COLUMNS} FROM turn_records
        WHERE agent_name = ? AND index_event_id = ?
        """,  # noqa: S608 - a fixed column list, not interpolated input
        (agent_name, event_id),
    )
    return None if row is None else str(row["record_json"])


def load_all(transaction: Transaction, agent_name: str) -> tuple[tuple[str, str, str], ...]:
    """Return every ``(index_event_id, anchor_event_id, record_json)`` for one agent.

    What a warm-up reads. Ordered by the event that indexes the record so a
    restart rebuilds the same map on both backends; the ordering is pinned to
    byte order for the same reason the outbox scans are, since a server whose
    collation is not byte order would otherwise disagree with SQLite about it.
    """
    rows = transaction.fetchall(
        f"""
        SELECT {_COLUMNS} FROM turn_records
        WHERE agent_name = ?
        ORDER BY index_event_id/*bytes*/
        """,  # noqa: S608 - a fixed column list, not interpolated input
        (agent_name,),
    )
    return tuple((str(row["index_event_id"]), str(row["anchor_event_id"]), str(row["record_json"])) for row in rows)


def forget(transaction: Transaction, agent_name: str, *, index_event_ids: Sequence[str]) -> None:
    """Drop records indexed by these events, as ledger compaction does."""
    if not index_event_ids:
        return
    placeholders = ", ".join("?" for _ in index_event_ids)
    transaction.execute(
        f"""
        DELETE FROM turn_records
        WHERE agent_name = ? AND index_event_id IN ({placeholders})
        """,  # noqa: S608 - placeholders are generated, values are still bound
        (agent_name, *index_event_ids),
    )


__all__ = ["forget", "load", "load_all", "upsert"]
