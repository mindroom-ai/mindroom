"""Compatibility for Agno 2 session payloads retained beside current run rows."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Collection

    from sqlalchemy import Table
    from sqlalchemy.orm import Session

# Legacy format: Agno 2 sessions stored run history in single- or double-encoded JSON `runs` blobs.
# Last legacy release: v2026.9.11; replacement: v2026.9.12 wrote first-class Agno 3 run rows.
# Handling: Merge retained blob runs with current rows and scrub deletions and descendants transactionally.
# Coverage: tests/test_agent_storage_runs.py::test_legacy_runs_blob_is_merged_into_reads_and_deletions_stick.


def scrub_legacy_run_blobs(
    session: Session,
    sessions_table: Table,
    run_ids: Collection[str],
) -> None:
    """Remove deleted ids and descendants from retained 2.x ``runs`` payloads."""
    if "runs" not in sessions_table.c:
        return
    rows = session.execute(
        sessions_table.select()
        .with_only_columns(sessions_table.c.session_id, sessions_table.c.runs)
        .where(sessions_table.c.runs.isnot(None)),
    ).fetchall()
    for session_id, blob in rows:
        legacy_runs = _decode_legacy_run_blob(blob)
        kept = _legacy_runs_without(legacy_runs, run_ids)
        if len(kept) == len(legacy_runs):
            continue
        session.execute(
            sessions_table.update().where(sessions_table.c.session_id == session_id).values(runs=kept),
        )


def merge_legacy_run_payloads(current_runs: list[object], legacy_payload: object) -> list[object]:
    """Append historical runs absent from current rows without deduplicating the blob itself."""
    decoded = decode_persisted_session_json(legacy_payload)
    if decoded is None:
        return list(current_runs)
    if not isinstance(decoded, list):
        raise TypeError
    merged = list(current_runs)
    current_ids = {run_id for run_id in map(_run_id, current_runs) if run_id is not None}
    merged.extend(legacy_run for legacy_run in decoded if _run_id(legacy_run) not in current_ids)
    return merged


def decode_persisted_session_json(raw_value: object) -> object:
    """Strictly decode the single- or double-encoded JSON used by retained session payloads."""
    if raw_value is None:
        return None
    if not isinstance(raw_value, (str, bytes, bytearray)):
        raise TypeError
    decoded = json.loads(raw_value)
    return json.loads(decoded) if isinstance(decoded, str) else decoded


def legacy_session_runs_projection(columns: Collection[str]) -> str:
    """Select old blob payload fields using stable aliases whether or not the column remains."""
    if "runs" in columns:
        return "runs AS legacy_runs_payload, length(CAST(runs AS BLOB)) AS legacy_runs_payload_bytes"
    return "NULL AS legacy_runs_payload, NULL AS legacy_runs_payload_bytes"


def _decode_legacy_run_blob(blob: object) -> list[object]:
    """Forgiving decode for deletion-time scrubbing of malformed historical blobs."""
    if isinstance(blob, str):
        try:
            blob = json.loads(blob)
        except json.JSONDecodeError:
            return []
    return cast("list[object]", blob) if isinstance(blob, list) else []


def _legacy_runs_without(runs: list[object], run_ids: Collection[str]) -> list[object]:
    """Remove requested historical runs and all entries descending from them."""
    removed = set(run_ids)
    while True:
        children = {
            run_id
            for run in runs
            if _string_field(run, "parent_run_id") in removed
            and (run_id := _string_field(run, "run_id")) is not None
            and run_id not in removed
        }
        if not children:
            break
        removed |= children
    return [
        run
        for run in runs
        if _string_field(run, "run_id") not in removed and _string_field(run, "parent_run_id") not in removed
    ]


def _run_id(run: object) -> str | None:
    value = cast("dict[str, object]", run).get("run_id") if isinstance(run, dict) else None
    return value if isinstance(value, str) else None


def _string_field(entry: object, key: str) -> str | None:
    value = cast("dict[str, object]", entry).get(key) if isinstance(entry, dict) else None
    return value if isinstance(value, str) and value else None
