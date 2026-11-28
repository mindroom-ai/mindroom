"""Import pre-journal handled-turn ledgers and read their legacy summary provenance."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, cast

from mindroom.logging_config import get_logger

from . import legacy_turn_rows, turn_rows

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from .backend import Transaction

logger = get_logger(__name__)

_LEDGER_RECORDS_KEY = "records"


def legacy_responses_file_path(storage_path: Path, agent_name: str) -> Path:
    """Return where a pre-journal MindRoom kept this agent's handled turns.

    Named rather than spelled out at the one call site because it is half of a
    contract with a version that is no longer in this tree: a reader that
    drifted off the path would find no file, import nothing, and re-answer the
    backlog of every installation being upgraded.
    """
    return storage_path / "tracking" / f"{agent_name}_responded.json"


def legacy_summary_sources(
    transaction: Transaction,
    agent_name: str,
    room_id: str,
    event_ids: Iterable[str],
) -> dict[str, str]:
    """Return revision to source for the legacy summary revisions in ``room_id`` that name one of these events."""
    ids = tuple(dict.fromkeys(event_ids))
    if not ids:
        return {}
    placeholders = ", ".join("?" for _ in ids)
    rows = transaction.fetchall(
        f"SELECT revision_id, source_event_id FROM legacy_summary_revisions "  # noqa: S608
        f"WHERE agent_name = ? AND room_id = ? "
        f"AND (revision_id IN ({placeholders}) OR source_event_id IN ({placeholders}))",
        (agent_name, room_id, *ids, *ids),
    )
    return {str(row["revision_id"]): str(row["source_event_id"]) for row in rows}


def _import_legacy_ledger_rows(transaction: Transaction, agent_name: str, raw_ledger: object) -> int:
    """Convert one agent's pre-journal ledger into turn rows; return how many turns were written.

    An event the row store already indexes keeps its owner, so an interrupted
    or repeated import cannot overwrite newer state, while every redaction the
    file records still applies.
    """
    # LEGACY_COMPAT: Unversioned event-to-record handled-turn JSON maps.
    # Legacy format: Top-level event-to-record handled-turn JSON map.
    # Last legacy release: v2026.7.101; replacement: v2026.7.102 intentionally rejected this shape.
    # Handling: Process as zero rows; the caller still renames the file to .imported, preserving its bytes.
    # Coverage: tests/test_turn_rows_conversion.py::test_an_unversioned_ledger_imports_nothing.
    records = (
        cast("Mapping[str, object]", raw_ledger).get(_LEDGER_RECORDS_KEY) if isinstance(raw_ledger, Mapping) else None
    )
    if not isinstance(records, Mapping):
        return 0
    # LEGACY_COMPAT: Schema-v1 handled-turn JSON before journal ownership.
    # Legacy format: Schema-version-1 handled-turn JSON ledger, one record per indexed event, before journal ownership.
    # Last legacy release: v2026.8.30; replacement: v2026.8.31 moved records into the journal database.
    # Handling: Entries group and convert as turn_records rows do; events the row store indexes keep their owner.
    # Coverage: tests/test_turn_rows_conversion.py::test_an_interrupted_import_into_a_populated_store_keeps_its_owners.
    entries = [
        (
            str(index_event_id),
            str(cast("Mapping[str, object]", raw_record).get("anchor_event_id")),
            json.dumps(raw_record, sort_keys=True),
        )
        for index_event_id, raw_record in records.items()
        if isinstance(raw_record, Mapping)
    ]
    groups = legacy_turn_rows._groups(entries)
    candidates = {event_id for group in groups for event_id in group.record.indexed_event_ids}
    occupied = frozenset(
        event_id for event_id in candidates if turn_rows.get_turn_record(transaction, agent_name, event_id) is not None
    )
    deliveries = legacy_turn_rows._outstanding_final_deliveries(transaction).get(agent_name, {})
    return legacy_turn_rows._convert(
        transaction,
        agent_name,
        groups,
        occupied=occupied,
        final_deliveries=deliveries,
        logins=legacy_turn_rows._agent_logins(transaction).get(agent_name, ()),
    )


async def import_legacy_ledger_file(records: turn_rows.TurnRecordStore, *, path: Path | None) -> int:
    """Import an agent's pre-journal ledger file once, then rename it; return how many turns were written.

    The file's presence is the only trigger and its rename the only marker:
    the import commits in one transaction before the rename, and a crash in
    between repeats an import whose every event is then already indexed.
    """
    if path is None or not path.exists():
        return 0
    raw = json.loads(path.read_text())
    imported = await records.write(_import_legacy_ledger_rows, raw)
    path.replace(path.with_suffix(f"{path.suffix}.imported"))
    logger.info("handled_turn_ledger_imported", agent=records.agent_name, imported_turn_count=imported)
    return imported


__all__ = ["import_legacy_ledger_file", "legacy_responses_file_path", "legacy_summary_sources"]
