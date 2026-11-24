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


# LEGACY_COMPAT: Usage recorded under a per-API provider name.
# Legacy format: usage snapshots and run records name the provider of a model on the `openai` route `OpenAI Chat` or
# `OpenAI Responses`, on `codex` `OpenAI Codex Responses`, and on `kimi` `Kimi Chat`, in per-model `details`, in request
# provenance, and as the `model_provider` of memory-extraction and compaction-summary rows.
# Last legacy release: v2026.10.735 (replacement: the next release, which records each route's one provider name,
# `OpenAI`, `OpenAI Codex`, or `Kimi`, as runs already stored it in their `model_provider`).
# Handling: Usage reports, spend, and reply cost read each stored name as its route's name, so old and new usage of one
# model add up and stay priced; stored rows are not rewritten.
# Coverage: tests/test_request_usage.py::test_usage_stored_under_a_per_api_provider_name_reports_under_its_route_name;
# tests/test_budget_pricing.py::test_a_run_stored_under_a_per_api_provider_name_is_priced_at_its_route_entry.
_PER_API_PROVIDER_NAMES = {
    "OpenAI Chat": "OpenAI",
    "OpenAI Responses": "OpenAI",
    "OpenAI Codex Responses": "OpenAI Codex",
    "Kimi Chat": "Kimi",
}


def current_provider_name(provider: str) -> str:
    """Return the provider name a model's usage is recorded under now for one stored ``provider`` name."""
    return _PER_API_PROVIDER_NAMES.get(provider, provider)
