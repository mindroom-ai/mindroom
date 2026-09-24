"""Content-free usage snapshots, independent of conversation run retention."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, cast

if TYPE_CHECKING:
    from collections.abc import Mapping

type IndependentUsageKind = Literal[
    "compaction_summary",
    "memory_auto_flush",
    "dynamic_workflow",
    "live_voice",
    "skill_learning",
]
type UsageKind = Literal["run"] | IndependentUsageKind

TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "audio_input_tokens",
    "audio_output_tokens",
    "audio_total_tokens",
)


def has_token_usage(metrics: Mapping[str, object]) -> bool:
    """Return whether serialized metrics report any provider token counter."""
    return any(metrics.get(key) for key in TOKEN_FIELDS)


def quote_identifier(value: str) -> str:
    """Quote a SQLite identifier supplied by the storage owner."""
    return '"' + value.replace('"', '""') + '"'


def usage_table_sql(session_table: str) -> str:
    """Create the usage table; its publication also marks completed initial seeding."""
    return (
        f"CREATE TABLE IF NOT EXISTS {quote_identifier(session_table + '_usage')} ("
        "id INTEGER PRIMARY KEY, "
        f"session_id TEXT NOT NULL REFERENCES {quote_identifier(session_table)}(session_id) ON DELETE CASCADE, "
        "run_id TEXT, usage_data TEXT, UNIQUE(session_id, run_id))"
    )


def usage_upsert_sql(session_table: str) -> str:
    """Replace a saved run's snapshot; missing historical IDs remain distinct rows."""
    return (
        f"INSERT INTO {quote_identifier(session_table + '_usage')} (session_id, run_id, usage_data) "  # noqa: S608
        "VALUES (?, ?, ?) ON CONFLICT(session_id, run_id) DO UPDATE SET usage_data = excluded.usage_data"
    )


def project_usage(run: Mapping[str, object]) -> dict[str, object]:
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
            "voice_seconds",
            "voice_finalized",
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
    run_metrics = cast("dict[str, object]", metrics)
    selected = _project_metrics(run_metrics)
    details = run_metrics.get("details")
    if details is not None:
        # An invalid details sentinel lets the reporter retain totals while reporting missing attribution.
        selected["details"] = _project_model_details(details)
    result["metrics"] = selected
    requests = _project_requests(run.get("messages"))
    if requests is not None:
        result["requests"] = requests
    return result


def _project_requests(messages: object) -> list[dict[str, object]] | None:
    """Project only current assistant counters and timestamps, with no model guesses."""
    if not isinstance(messages, list):
        return None
    requests: list[dict[str, object]] = []
    for raw_message in messages:
        if not isinstance(raw_message, dict):
            continue
        message = cast("dict[str, object]", raw_message)
        if message.get("role") != "assistant" or message.get("from_history", False) is not False:
            continue
        provider_data = message.get("provider_data")
        if (
            isinstance(provider_data, dict)
            and cast("dict[str, object]", provider_data).get("mindroom_aggregate_usage") is True
        ):
            # Retried attempts have aggregate counters, not one request's price or timestamp.
            continue
        message_metrics = message.get("metrics")
        if not isinstance(message_metrics, dict):
            continue
        request_metrics = _project_metrics(cast("dict[str, object]", message_metrics))
        if request_metrics:
            created_at = message.get("created_at")
            requests.append(
                {
                    "created_at": created_at if isinstance(created_at, (int, float)) else None,
                    "metrics": request_metrics,
                },
            )
    return requests


def _project_metrics(metrics: Mapping[str, object]) -> dict[str, object]:
    # Preserve scalar validation failures for the reporter, without retaining arbitrary nested data.
    return {
        key: value if isinstance(value, (int, float, str, type(None))) else False
        for key in TOKEN_FIELDS
        if (value := metrics.get(key)) is not None
    }


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
            model_metrics = cast("dict[str, object]", entry)
            model = _project_metrics(model_metrics)
            for key in ("id", "provider"):
                if isinstance(model_metrics.get(key), str):
                    model[key] = model_metrics[key]
            models.append(model)
    return {"model": models}
