"""Own historical revision-summary replay decisions."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Collection

    from mindroom.turn_record import RevisionReplay

# Legacy format: Source-level revision summaries without per-revision replay provenance.
# Last legacy release: v2026.9.42; replacement: v2026.9.43 added per-revision provenance.
# Handling: Preserve and select source-only summary ownership only for reconstructed historical revisions.
# Coverage: tests/test_legacy_revision_replay.py and tests/test_turn_store.py::test_legacy_compacted_revision_uses_retained_owner_on_cold_reopen.


def preserve_summary_provenance(current: RevisionReplay, previous: RevisionReplay) -> RevisionReplay:
    """Retain historical summary ownership across current replay updates."""
    return replace(current, legacy_summary_provenance=True) if previous.legacy_summary_provenance else current


def summary_source_id(revision: RevisionReplay) -> str | None:
    """Return the source ID only when an old summary owns the revision indirectly."""
    return revision.source_event_id if revision.legacy_summary_provenance else None


def summary_depends_on_source(
    legacy_source_event_id: str | None,
    *,
    has_summary: bool,
    seen_event_ids: Collection[str],
) -> bool:
    """Return whether a historical summary consumed the retained source owner."""
    return has_summary and legacy_source_event_id is not None and legacy_source_event_id in seen_event_ids
