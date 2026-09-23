"""Tests for historical revision-summary replay decisions."""

from __future__ import annotations

from dataclasses import replace

from mindroom.legacy_revision_replay import (
    preserve_summary_provenance,
    summary_depends_on_source,
    summary_source_id,
)
from mindroom.turn_record import RevisionReplay


def test_preserve_summary_provenance_retains_historical_flag_and_current_response() -> None:
    """A current replay update must retain historical summary ownership."""
    old = RevisionReplay("$source", 10, legacy_summary_provenance=True)
    new = RevisionReplay("$source", 10, response_event_id="$response")

    retained = preserve_summary_provenance(new, old)

    assert retained.legacy_summary_provenance is True
    assert retained.response_event_id == "$response"
    assert new.legacy_summary_provenance is False


def test_preserve_summary_provenance_changes_only_the_historical_flag() -> None:
    """Provenance retention must preserve current physical revision facts."""
    old = RevisionReplay("$old-source", 5, legacy_summary_provenance=True)
    current = RevisionReplay(
        "$source",
        10,
        redacted=True,
        cleanup_pending=True,
        response_event_id="$response",
    )

    assert preserve_summary_provenance(current, old) == replace(
        current,
        legacy_summary_provenance=True,
    )


def test_false_prior_provenance_does_not_overwrite_current_true_provenance() -> None:
    """A modern prior replay cannot clear provenance already held by current state."""
    previous = RevisionReplay("$source", 10)
    current = RevisionReplay("$source", 20, legacy_summary_provenance=True)

    assert preserve_summary_provenance(current, previous) is current


def test_summary_source_id_selects_only_historical_summary_owners() -> None:
    """Source-only summary ownership applies only to reconstructed replay facts."""
    historical = RevisionReplay("$source", 10, legacy_summary_provenance=True)
    modern = RevisionReplay("$source", 10)

    assert summary_source_id(historical) == "$source"
    assert summary_source_id(modern) is None


def test_summary_dependency_requires_summary_owner_and_seen_source() -> None:
    """Cleanup reaches a source-only summary only when every ownership fact agrees."""
    seen_event_ids = {"$source"}

    assert summary_depends_on_source(
        "$source",
        has_summary=True,
        seen_event_ids=seen_event_ids,
    )
    assert not summary_depends_on_source(
        "$source",
        has_summary=False,
        seen_event_ids=seen_event_ids,
    )
    assert not summary_depends_on_source(
        None,
        has_summary=True,
        seen_event_ids=seen_event_ids,
    )
    assert not summary_depends_on_source(
        "$elsewhere",
        has_summary=True,
        seen_event_ids=seen_event_ids,
    )
