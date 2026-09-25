"""Read compaction state written before compacted runs were archived."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.history import archive

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping

    from agno.db.base import BaseDb

# LEGACY_COMPAT: Destructive compaction state without an archive.
# Legacy format: A v2 ``mindroom_compaction`` scope state holding ``compacted_run_ids``
# tombstones and ``last_compacted_at``/``last_summary_model``/``last_compacted_run_count``
# audit fields, written when compaction deleted the runs it summarized.
# Last legacy release: v2026.9.310; replacement: the next release archives compacted runs
# in ``<session_table>_compactions`` and ``<session_table>_compacted_runs``.
# Handling: ``adopt_legacy_compaction`` records such a scope, or a ``session.summary`` of a scope
# without archive generations, as a content-free legacy generation holding that summary, the
# preserved seen ids it may contain, and tombstone rows; ``history/storage.py`` then drops these
# keys. Once a scope has a generation the archive is authoritative, so retired keys that a stale
# pre-adoption snapshot writes back are dropped without being adopted again. While that summary
# replays, redacting an event it may contain retires it, because the folded content no longer
# exists.
# Coverage: tests/test_legacy_compaction_state.py,
# tests/test_compaction_redaction.py::test_redacting_legacy_provenance_clears_summary_and_archived_generations,
# tests/test_compaction_redaction.py::test_removing_a_live_run_retires_a_legacy_summary,
# and tests/test_turn_store.py::test_prepare_redaction_invalidates_legacy_compacted_replay.
_LEGACY_STATE_KEYS = frozenset(
    {"compacted_run_ids", "last_compacted_at", "last_summary_model", "last_compacted_run_count"},
)


def _legacy_tombstones(raw_state: Mapping[str, object] | None) -> tuple[str, ...] | None:
    """Return a destructive compactor's tombstones, or ``None`` when it did not write this state."""
    if raw_state is None or not _LEGACY_STATE_KEYS.intersection(raw_state):
        return None
    raw_run_ids = raw_state.get("compacted_run_ids")
    if not isinstance(raw_run_ids, list):
        return ()
    return tuple(dict.fromkeys(run_id for run_id in raw_run_ids if isinstance(run_id, str) and run_id))


def adopt_legacy_compaction(
    storage: BaseDb,
    *,
    session_id: str,
    scope_key: str,
    raw_state: Mapping[str, object] | None,
    summary: str | None,
    preserved_event_ids: Collection[str],
) -> bool:
    """Record legacy history as one generation; return whether retired state keys must be dropped."""
    tombstones = _legacy_tombstones(raw_state)
    if archive.latest_generation(storage, session_id=session_id, scope_key=scope_key) is not None:
        # Retired keys on an archive-managed scope come from a stale snapshot written before adoption.
        return tombstones is not None
    if tombstones is None and summary is None:
        return False
    archive.record_legacy_generation(
        storage,
        session_id=session_id,
        scope_key=scope_key,
        summary=summary,
        tombstone_run_ids=tombstones or (),
        event_ids=preserved_event_ids,
    )
    return tombstones is not None
