"""Interpret older usage snapshots."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping


# LEGACY_COMPAT: Request snapshots omit their provider and model.
# Legacy format: Requests contain only created_at and metrics.
# Last legacy release: v2026.9.303 for run requests (replacement: v2026.9.304, which attributes each request to its
# model) and v2026.10.631 for a compaction summary's and a voice transcription's one request (replacement:
# v2026.10.632).
# Current producer: voice transcription emits one request without a model when its usage row names no provider (a
# compatible or custom-host endpoint).
# Handling: Inherit only a single run model; the caller must reconcile every counter.
# Coverage: tests/test_request_usage.py::test_request_export_keeps_short_calls_distinct_and_preserves_all_counters;
# tests/test_request_usage.py::test_unreconciled_request_details_preserve_aggregates_and_mark_coverage;
# tests/test_request_usage.py::test_a_compaction_summary_stored_with_an_untagged_request_reports_its_model;
# tests/test_voice_system_usage.py::test_custom_stt_uses_submitted_model_without_guessing_provider.
def legacy_request_model(models: Mapping[tuple[str, str], object]) -> tuple[str, str] | None:
    """Return unambiguous run attribution for requests written without a model."""
    return next(iter(models)) if len(models) == 1 else None
