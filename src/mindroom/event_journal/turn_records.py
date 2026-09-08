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

import json
from dataclasses import replace
from typing import TYPE_CHECKING

from mindroom.handled_turns import TurnRecordCodec, resolve_turn_record
from mindroom.turn_record import (
    canonicalize_turn_record,
    completed_response_record,
    merge_committed_response,
    same_turn_identity,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.turn_record import TurnRecord

    from .backend import Transaction
    from .models import TerminalTurnWrite

_COLUMNS = "index_event_id, anchor_event_id, record_json"


def _claim_records(transaction: Transaction, agent_name: str, event_ids: Sequence[str]) -> dict[str, TurnRecord]:
    records = {}
    for event_id in sorted(set(event_ids)):
        row = transaction.fetchone(
            """
            UPDATE turn_records SET record_json = record_json
            WHERE agent_name = ? AND index_event_id = ? RETURNING record_json
            """,
            (agent_name, event_id),
        )
        if row is not None:
            record = TurnRecordCodec._from_ledger_record(event_id, json.loads(str(row["record_json"])))
            assert record is not None, "Corrupt turn record"
            records[event_id] = record
    return records


def _claim_turn_records(transaction: Transaction, agent_name: str, candidate: TurnRecord) -> dict[str, TurnRecord]:
    """Claim current canonical sources before aliases, revisions, or old-anchor deletion."""
    # Replay-only tombstones stay auxiliary even when a newer snapshot knows
    # their standalone rows; only indexed identities discover canonical owners.
    placeholders = ", ".join("?" for _ in candidate.indexed_event_ids)
    rows = transaction.fetchall(
        f"SELECT index_event_id, record_json FROM turn_records WHERE agent_name = ? AND index_event_id IN ({placeholders})",  # noqa: S608
        (agent_name, *candidate.indexed_event_ids),
    )
    source_ids = set(candidate.source_event_ids)
    for row in rows:
        owner = TurnRecordCodec._from_ledger_record(str(row["index_event_id"]), json.loads(str(row["record_json"])))
        assert owner is not None, "Corrupt turn record"
        source_ids.update(owner.source_event_ids)
    records = _claim_records(transaction, agent_name, tuple(source_ids))
    if any(not source_ids.issuperset(owner.source_event_ids) for owner in records.values()):
        # Roll back, including any ACK, so accepted work stays pending and a
        # retry discovers the new owner without acquiring source locks backwards.
        msg = "Canonical turn ownership changed while acquiring source rows"
        raise RuntimeError(msg)
    event_ids = {*candidate.indexed_event_ids, *(candidate.revision_replay or {})}
    for owner in records.values():
        event_ids.update(owner.indexed_event_ids)
        event_ids.update(owner.revision_replay or {})
    records.update(_claim_records(transaction, agent_name, tuple(event_ids - source_ids)))
    return records


def write_record(
    transaction: Transaction,
    agent_name: str,
    *,
    index_event_ids: Sequence[str],
    anchor_event_id: str,
    record_json: str,
) -> str | None:
    """Keep already-committed delivery proof when a cached ledger write arrives late."""
    candidate = TurnRecordCodec._from_ledger_record(index_event_ids[0], json.loads(record_json))
    assert candidate is not None, "Corrupt turn record"
    assert candidate.anchor_event_id == anchor_event_id, "Mismatched turn anchor"
    records = _claim_turn_records(transaction, agent_name, candidate)
    candidate = resolve_turn_record(candidate, records)
    if candidate is None:
        return None
    current = next((records[event_id] for event_id in candidate.source_event_ids if event_id in records), None)
    if (
        current is not None
        and current.completed
        and current.response_event_id
        and same_turn_identity(current, candidate)
        and not (
            candidate.response_event_id is None
            and candidate.user_stop_receipt_order is not None
            and candidate.user_stop_settled_receipt_order == candidate.user_stop_receipt_order
            and set(candidate.source_event_ids).issubset(candidate.redacted_source_event_ids)
        )
    ):
        candidate = merge_committed_response(candidate, current)
        if candidate is None:
            return None
    if candidate.response_event_id is not None and set(candidate.source_event_ids).issubset(
        candidate.redacted_source_event_ids,
    ):
        cleaned = transaction.fetchone(
            """SELECT 1 FROM matrix_delivery_outbox AS initial
            WHERE initial.delivery_id = ? AND initial.stage = 'initial'
              AND initial.acknowledged_event_id = ? AND (initial.retired = 1 OR ?)
              AND NOT EXISTS (
                SELECT 1 FROM matrix_delivery_outbox AS final
                WHERE final.principal_id = initial.principal_id AND final.delivery_id = initial.delivery_id
                  AND final.stage = 'final' AND (final.acknowledged_event_id IS NOT NULL
                    OR (final.retired = 0 AND final.permanent_failure_reason IS NULL))
              )""",
            (
                candidate.anchor_event_id,
                candidate.response_event_id,
                candidate.completed
                and candidate.user_stop_receipt_order is None
                and (current is None or not current.completed),
            ),
        )
        if cleaned is not None:
            # A completed candidate can arrive after Matrix redaction but before
            # detachment/retirement. Only FINAL proves this deleted INITIAL answered.
            candidate = replace(
                candidate,
                response_event_id=None,
                completed=candidate.user_stop_receipt_order is not None,
                user_stop_settled_receipt_order=candidate.user_stop_receipt_order,
            )
    assert candidate.anchor_event_id is not None
    record_json = json.dumps(TurnRecordCodec._to_ledger_record(candidate))
    upsert(
        transaction,
        agent_name,
        index_event_ids=candidate.indexed_event_ids,
        anchor_event_id=candidate.anchor_event_id,
        record_json=record_json,
    )
    return record_json


def commit_terminal(transaction: Transaction, prepared: TerminalTurnWrite) -> TerminalTurnWrite | None:
    """Merge final-delivery proof against transaction-current canonical ownership."""
    candidate = TurnRecordCodec._from_ledger_record(prepared.index_event_ids[0], json.loads(prepared.record_json))
    assert candidate is not None, "Corrupt terminal turn record"
    records = _claim_turn_records(transaction, prepared.agent_name, candidate)
    current = next((records[event_id] for event_id in candidate.source_event_ids if event_id in records), None)
    if any(
        (owner := records.get(event_id)) is not None and owner.source_event_ids != candidate.source_event_ids
        for event_id in candidate.indexed_event_ids
    ):
        return None
    tombstones = tuple(
        event_id
        for event_id, record in records.items()
        if record is not None and event_id in record.redacted_source_event_ids
    )
    if current is not None:
        candidate = canonicalize_turn_record(candidate, redacted_source_event_ids=current.redacted_source_event_ids)
    if (
        current is not None
        and candidate.latest_edit_receipt_order is not None
        and (
            current.user_stop_receipt_order is not None
            and current.user_stop_receipt_order >= candidate.latest_edit_receipt_order
        )
    ):
        committed = current
    else:
        assert candidate.response_event_id is not None
        committed = merge_committed_response(
            current,
            completed_response_record(candidate, candidate.response_event_id),
            tombstoned_event_ids=tombstones,
        )
    if committed is None or committed.anchor_event_id is None:
        return None
    write = replace(
        prepared,
        index_event_ids=committed.indexed_event_ids,
        anchor_event_id=committed.anchor_event_id,
        record_json=json.dumps(TurnRecordCodec._to_ledger_record(committed)),
    )
    upsert(
        transaction,
        write.agent_name,
        index_event_ids=write.index_event_ids,
        anchor_event_id=write.anchor_event_id,
        record_json=write.record_json,
    )
    return write


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


def load_record(transaction: Transaction, agent_name: str, event_id: str) -> TurnRecord | None:
    """Read exact durable turn authority without borrowing the process ledger cache."""
    row = transaction.fetchone(
        "SELECT anchor_event_id, record_json FROM turn_records WHERE agent_name = ? AND index_event_id = ?",
        (agent_name, event_id),
    )
    if row is None:
        return None
    try:
        record = TurnRecordCodec._from_ledger_record(event_id, json.loads(str(row["record_json"])))
    except (TypeError, ValueError):
        return None
    if record is None or record.anchor_event_id != row["anchor_event_id"] or event_id not in record.indexed_event_ids:
        return None
    return record


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


__all__ = ["adopt_missing", "commit_terminal", "forget", "load_all", "load_record", "upsert", "write_record"]
