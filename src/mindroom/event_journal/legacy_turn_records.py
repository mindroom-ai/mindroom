"""Migration-only turn-record writes for pre-journal handled-turn adoption."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .backend import Transaction


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
