"""The conversion's step that seeds the usage ledger of an Agno-layout database from the history it holds."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast

from mindroom.session_storage.legacy_agno2_runs import decode_session_json, has_blob_column
from mindroom.session_storage.schema import quote_identifier, table_names, usage_table_sql, usage_upsert_sql
from mindroom.usage.storage import project_model_entry, project_token_counters

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Mapping

    from mindroom.session_storage.legacy_agno3_schema import AgnoTableNames


# LEGACY_COMPAT: Usage derived from disposable conversation rows and session blobs.
# Legacy format: Agno 3 run_data rows and retained Agno 2 single/double-encoded runs blobs.
# Last legacy release: v2026.9.190, the last verified native writer without an independent usage table.
# Replacement: v2026.9.191 introduced independent usage snapshots; no historical billing completeness is inferred.
# Handling: Detect by schema in a database's conversion to storage v2; seed in its transaction, prefer current rows,
# retain unknown dates and content-free gaps. Each snapshot projects a stored run dict (``_project_usage``) as
# ``usage.storage.run_usage_snapshot`` projects a typed run, but for what no release before the ledger stored: requests
# mid-turn compaction carried, model provenance stamps, and retry-attempt sums.
# Coverage: tests/session_storage/test_legacy_usage_ledger.py.
def seed_usage_ledger(connection: sqlite3.Connection, names: AgnoTableNames) -> None:
    """Seed the usage ledger of a database that has none in the caller's write transaction."""
    tables = table_names(connection)
    if names.sessions not in tables or names.usage in tables:
        return
    connection.execute(usage_table_sql(names))
    legacy_column = "runs" if has_blob_column(connection, names) else "NULL"
    has_runs = names.runs in tables
    for session_id, blob in connection.execute(
        f"SELECT session_id, {legacy_column} FROM {quote_identifier(names.sessions)}",  # noqa: S608
    ):
        known_ids: set[str] = set()
        if has_runs:
            for run_id, raw, created_at in connection.execute(
                f"SELECT run_id, run_data, created_at FROM {quote_identifier(names.runs)} WHERE session_id = ?",  # noqa: S608
                (session_id,),
            ):
                known_ids.add(run_id)
                decoded = _decode(raw)
                run = {**decoded, "run_id": run_id, "created_at": created_at} if isinstance(decoded, dict) else None
                _insert_usage(connection, names, session_id, run_id, run)
        decoded_blob = _decode(blob)
        # SQL NULL and JSON null were Agno's native empty-history values.
        if decoded_blob is None:
            continue
        for run in decoded_blob if isinstance(decoded_blob, list) else [None]:
            raw_id = cast("dict[str, object]", run).get("run_id") if isinstance(run, dict) else None
            run_id = raw_id if isinstance(raw_id, str) and raw_id else None
            if run_id is not None and run_id in known_ids:
                continue
            if run_id is not None:
                known_ids.add(run_id)
            _insert_usage(connection, names, session_id, run_id, run)


def _decode(raw: object) -> object:
    try:
        return decode_session_json(raw)
    except (RecursionError, TypeError, ValueError):
        return False


def _insert_usage(
    connection: sqlite3.Connection,
    names: AgnoTableNames,
    session_id: str,
    run_id: str | None,
    run: object,
) -> None:
    payload = json.dumps(_project_usage(cast("dict[str, object]", run))) if isinstance(run, dict) else None
    connection.execute(usage_upsert_sql(names), (session_id, run_id, payload))


def _project_usage(run: Mapping[str, object]) -> dict[str, object]:
    """Keep only attribution, timestamps and provider counters, never conversation content."""
    result = {
        key: run[key]
        for key in (
            "run_id",
            "parent_run_id",
            "team_id",
            "user_id",
            "model_provider",
            "model",
            "created_at",
            "updated_at",
        )
        if key in run and isinstance(run[key], (str, int, float, type(None)))
    }
    if run.get("parent_run_id") is not None and "parent_run_id" not in result:
        result["parent_run_id"] = False
    metadata = run.get("metadata")
    if isinstance(metadata, dict):
        requester = cast("dict[str, object]", metadata).get("requester_id")
        if isinstance(requester, str):
            result["metadata"] = {"requester_id": requester}
    metrics = run.get("metrics", {})
    if not isinstance(metrics, dict):
        result["metrics"] = None
        return result
    result["metrics"] = _project_run_metrics(cast("dict[str, object]", metrics))
    requests = _project_requests(run.get("messages"))
    if requests is not None:
        result["requests"] = requests
    return result


def _project_requests(messages: object) -> list[dict[str, object]] | None:
    """Project assistant counters and timestamps."""
    if not isinstance(messages, list):
        return None
    requests: list[dict[str, object]] = []
    for raw_message in messages:
        if not isinstance(raw_message, dict):
            continue
        message = cast("dict[str, object]", raw_message)
        if message.get("role") != "assistant" or message.get("from_history", False) is not False:
            continue
        message_metrics = message.get("metrics")
        if not isinstance(message_metrics, dict):
            continue
        request_metrics = project_token_counters(cast("dict[str, object]", message_metrics))
        if request_metrics:
            created_at = message.get("created_at")
            requests.append(
                {
                    "created_at": created_at if isinstance(created_at, (int, float)) else None,
                    "metrics": request_metrics,
                },
            )
    return requests


def _project_run_metrics(metrics: Mapping[str, object]) -> dict[str, object]:
    selected = project_token_counters(metrics)
    details = metrics.get("details")
    if details is not None:
        # An invalid details sentinel lets the reporter retain totals while reporting missing attribution.
        selected["details"] = _project_model_details(details)
    return selected


def _project_model_details(details: object) -> object:
    if not isinstance(details, dict):
        return False
    models = []
    for entries in details.values():
        if not isinstance(entries, list):
            return False
        for entry in entries:
            if not isinstance(entry, dict):
                return False
            models.append(project_model_entry(cast("dict[str, object]", entry)))
    return {"model": models}
