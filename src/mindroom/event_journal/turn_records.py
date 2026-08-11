"""Durable turn records, in the database that settles the turns they describe.

"Has this turn finished?" used to be answered by two records in two
substrates: the journal's pending set and a JSON-file ledger. They could not
share a transaction, so they settled at different moments, and every reader
that needed a trustworthy answer had to consult both and know why.

These rows are what collapsed that. The ledger in ``handled_turns.py`` now
loads its whole map from here and writes every change back through this
module, so a turn and its settlement commit together. The JSON file survives
only as a one-time import for installs that predate the move.

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

    Finding those rows cannot start from the anchor being written. Redaction
    and conflict projection re-anchor a record when the anchor itself is one of
    the dropped sources (``handled_turns.py`` picks the last retained source
    instead), so the rows to clean up are filed under the *old* anchor and a
    delete scoped to the new one never sees them. The old anchors are recovered
    from the rows the surviving indexes still point at.
    """
    if not index_event_ids:
        return
    placeholders = ", ".join("?" for _ in index_event_ids)
    previous = transaction.fetchall(
        f"""
        SELECT DISTINCT anchor_event_id FROM turn_records
        WHERE agent_name = ? AND index_event_id IN ({placeholders})
        """,  # noqa: S608 - placeholders are generated, values are still bound
        (agent_name, *index_event_ids),
    )
    anchors = {anchor_event_id, *(str(row["anchor_event_id"]) for row in previous)}
    anchor_placeholders = ", ".join("?" for _ in anchors)
    transaction.execute(
        f"""
        DELETE FROM turn_records
        WHERE agent_name = ?
          AND anchor_event_id IN ({anchor_placeholders})
          AND index_event_id NOT IN ({placeholders})
        """,  # noqa: S608 - placeholders are generated, values are still bound
        (agent_name, *sorted(anchors), *index_event_ids),
    )
    for index_event_id in index_event_ids:
        transaction.execute(
            """
            INSERT INTO turn_records (
                agent_name, index_event_id, anchor_event_id, record_json
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT (agent_name, index_event_id) DO UPDATE SET
                anchor_event_id = excluded.anchor_event_id,
                record_json = excluded.record_json
            """,
            (agent_name, index_event_id, anchor_event_id, record_json),
        )


def adopt_missing(
    transaction: Transaction,
    agent_name: str,
    *,
    index_event_ids: Sequence[str],
    anchor_event_id: str,
    record_json: str,
) -> int:
    """Fill only the indexes this agent has no record under, and return how many.

    For migration, and deliberately not ``upsert``. A legacy record can overlap
    a stored one *partially*: it indexes two sources of one coalesced turn, the
    runtime has already written a newer record under the first, and the second
    is absent. Sending that through ``upsert`` overwrites the newer record and
    -- because the legacy file is renamed immediately afterwards -- destroys the
    only copy of it. Skipping the record instead leaves the second source with
    no record at all, so a message that was answered can be answered again.

    Neither is acceptable, so this does neither: every occupied index keeps
    what it has, every empty one gains the legacy record. The stored record
    stays authoritative where it exists, which is right because it is at least
    as current as the file's copy, and the gap is closed where nothing else
    can close it.

    No delete pass either. ``upsert`` removes rows a shrinking turn no longer
    indexes, which is correct for a live write and wrong here: the rows this
    would remove are exactly the newer ones being protected.
    """
    if not index_event_ids:
        return 0
    adopted = 0
    for index_event_id in index_event_ids:
        existing = transaction.fetchone(
            """
            SELECT 1 AS present FROM turn_records
            WHERE agent_name = ? AND index_event_id = ?
            """,
            (agent_name, index_event_id),
        )
        if existing is not None:
            continue
        transaction.execute(
            """
            INSERT INTO turn_records (
                agent_name, index_event_id, anchor_event_id, record_json
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT (agent_name, index_event_id) DO NOTHING
            """,
            (agent_name, index_event_id, anchor_event_id, record_json),
        )
        adopted += 1
    return adopted


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


__all__ = ["adopt_missing", "forget", "load_all", "upsert"]
