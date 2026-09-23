"""Pure normalization of retired tool-job snapshots; the runtime owns publication."""

from __future__ import annotations

import hashlib
from typing import Any


def upgrade_schema_one_job(payload: dict[str, Any]) -> None:
    """Preserve saved facts without reconstructing missing authority or execution sources."""
    # LEGACY_COMPAT: Released jobs stored human holds and independent Matrix notification receipts.
    # Legacy format: Schema 1 with human_paused and deliveries, without source-event ownership.
    # Last legacy release: v2026.9.165; removed in v2026.9.166. Schema 2 replacement is unreleased.
    # Handling: Preserve consumption and exact-generation notifications separately. Interrupt abandoned
    # holds; retain child-only completed outcomes. Missing source and constructor proof stay missing.
    # Coverage: tests/test_legacy_tool_jobs.py::test_released_notification_does_not_replace_consumption
    # Coverage: tests/test_legacy_tool_jobs.py::test_released_execution_recovers_only_durable_outcomes
    # Coverage: tests/test_tool_job_retention.py::test_released_consumed_result_expires_without_source_history
    if "human_paused" in payload and "deliveries" in payload:
        payload.pop("human_paused")
        deliveries = payload.pop("deliveries")
        payload["legacy_source_untracked"] = True
        if any(
            item["job_id"] == payload["job_id"] and item["generation"] == payload["generation"] and item["acknowledged"]
            for item in deliveries
        ):
            payload["legacy_notified_generation"] = payload["generation"]
        if payload["status"] == "paused_for_human":
            payload["status"] = "running"
    if payload["kind"] == "delegation":
        child = payload["adapter"]["child"]
        if (
            payload["status"] in {"running", "cancel_requested"}
            and child["status"] in {"completed", "failed", "cancelled", "denied"}
            and child["result"] is not None
        ):
            payload["status"] = child["status"]
            payload["result"] = child["result"]
        child["result"] = None

    # LEGACY_COMPAT: Pre-digest job receipts persisted raw constructor configuration.
    # Legacy format: Later schema-1 snapshots with adapter.authority.construction.config_signature.
    # Last legacy release: Unreleased PR writer; schema 2 replaces the JSON string with its SHA-256.
    # Handling: Hash the exact saved UTF-8 bytes once, including expired receipts; never fill absent proof.
    # Coverage: tests/test_legacy_tool_jobs.py::test_raw_constructor_identity_is_scrubbed_on_recovery
    construction = payload["adapter"].get("authority", {}).get("construction")
    if construction is not None and "config_signature" in construction:
        construction["config_signature"] = hashlib.sha256(construction["config_signature"].encode()).hexdigest()
