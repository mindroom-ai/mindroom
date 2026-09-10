"""Read the pre-journal handled-turn ledger during a one-time upgrade."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING

from mindroom.logging_config import get_logger
from mindroom.turn_record import RevisionReplay, canonicalize_turn_record

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.event_journal.store import TurnRecordStore
    from mindroom.handled_turns import TurnRecordCodec
    from mindroom.turn_record import TurnRecord

logger = get_logger(__name__)

_LEDGER_RECORDS_KEY = "records"

# Legacy format: Schema-version-1 handled turns without source or suppressed revision summaries.
# Last legacy release: v2026.7.252; replacement: v2026.7.253 added optional source-level summaries.
# Handling: Accept absent summaries without inventing revision identity that the old writer never persisted.
# Coverage: tests/test_handled_turns.py::test_a_pre_database_ledger_is_adopted_on_first_load.

# Legacy format: Source-level revision summaries without per-revision replay state.
# Last legacy release: v2026.9.42; replacement: v2026.9.43 added revision_replay.
# Handling: Reconstruct reduced replay facts and label their summary-only provenance.
# Coverage: tests/test_handled_turns.py::test_v2026_9_42_turn_record_restores_revision_replay_through_store_reopen.


def legacy_responses_file_path(storage_path: Path, agent_name: str) -> Path:
    """Return where a pre-journal MindRoom kept this agent's handled turns.

    Named rather than spelled out at the one call site because it is half of a
    contract with a version that is no longer in this tree: the writer is gone,
    so nothing here fails if the reader drifts off the path that writer used.
    It would simply find no file, import nothing, and re-answer the backlog of
    every installation being upgraded -- silently, and only in production.
    Giving the path a name is what lets a test pin it against the bytes the old
    version actually wrote.

    See ``import_legacy_ledger`` for what is done with it.
    """
    return storage_path / "tracking" / f"{agent_name}_responded.json"


def restore_legacy_revision_replay(record: TurnRecord, raw_record: Mapping[str, object]) -> TurnRecord:
    """Restore replay provenance that older ledger records derived from revisions."""
    if "revision_replay" in raw_record or not record.source_event_revisions:
        return record
    return canonicalize_turn_record(
        record,
        revision_replay={
            revision_id: RevisionReplay(
                record.prompt_source_event_id(source),
                timestamp,
                legacy_summary_provenance=True,
            )
            for source, (timestamp, revision_id) in record.source_event_revisions.items()
            if revision_id != record.prompt_source_event_id(source)
        },
    )


async def import_legacy_ledger(
    *,
    path: Path | None,
    agent_name: str,
    records: TurnRecordStore,
    already_stored: set[str],
    codec: type[TurnRecordCodec],
) -> tuple[tuple[str, str, str], ...]:
    """Adopt an agent's pre-database records once and return the stored rows.

    The file's presence is the only trigger, and its rename is the only marker.
    An interrupted import must retry because it may have adopted only part of a
    coalesced turn. ``adopt_missing`` preserves rows the current runtime wrote
    while filling every absent legacy index, then the file rename makes
    a later compaction unable to resurrect retired history.
    """
    if path is None or not path.exists():
        return ()

    # Legacy format: Top-level event-to-record handled-turn JSON map.
    # Last legacy release: v2026.7.101; replacement: v2026.7.102 intentionally rejected this shape.
    # Handling: Process as zero rows and preserve exact bytes under .imported without restoring its reader.
    # Coverage: tests/test_handled_turns.py::test_released_unversioned_ledger_cutoff_preserves_bytes_without_adoption.

    # Legacy format: Schema-version-1 handled-turn JSON ledger before journal ownership.
    # Last legacy release: v2026.8.30; replacement: v2026.8.31 moved records into turn_records.
    # Handling: Adopt missing indexes and rename only after the full pass; .imported means processed, not adopted.
    # Coverage: tests/test_handled_turns.py::test_interrupted_legacy_ledger_import_retries_missing_indexes_before_rename.
    raw = json.loads(path.read_text())
    raw_records = raw.get(_LEDGER_RECORDS_KEY) if isinstance(raw, Mapping) else None
    decoded = (
        {
            event_id: record
            for event_id, raw_record in raw_records.items()
            if (record := codec._from_ledger_record(event_id, raw_record)) is not None
        }
        if isinstance(raw_records, Mapping)
        else {}
    )
    # One row per distinct turn, not per index: `upsert` already stores a
    # record under every event that indexes it, and writing it once per index
    # would re-delete and re-insert the same siblings repeatedly.
    unseen = {
        record.indexed_event_ids: record
        for record in decoded.values()
        if not already_stored.issuperset(record.indexed_event_ids)
    }
    adopted = 0
    for record in unseen.values():
        # Apply the current record invariants before storing this historical
        # projection, just as normal ledger reads do.
        imported = canonicalize_turn_record(record)
        assert imported.anchor_event_id is not None
        adopted += await records.adopt_missing(
            index_event_ids=imported.indexed_event_ids,
            anchor_event_id=imported.anchor_event_id,
            record_json=json.dumps(codec._to_ledger_record(imported)),
        )
    path.replace(path.with_suffix(f"{path.suffix}.imported"))
    logger.info(
        "handled_turn_ledger_imported",
        agent=agent_name,
        imported_event_count=adopted,
    )
    return await records.load_all()
