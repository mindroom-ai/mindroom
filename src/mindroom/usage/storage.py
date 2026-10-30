"""Content-free usage snapshots, independent of conversation run retention."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, cast, get_args

from mindroom.history.types import HistoryScope
from mindroom.llm.messages import MODEL_PROVENANCE_KEY, read_model_provenance, usage_sums_attempts
from mindroom.llm.runs import TeamRunRecord
from mindroom.llm.usage import TOKEN_FIELDS, RequestUsage, TokenCounts

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.llm.messages import Message
    from mindroom.llm.runs import AnyRunRecord
    from mindroom.llm.usage import HelperUsage, RunMetrics

# Run metadata key holding the projected usage of requests mid-turn compaction folded out of the run's messages.
COMPACTED_REQUESTS_METADATA_KEY = "mindroom_compacted_requests"

type IndependentUsageKind = Literal[
    "claude_agent",
    "compaction_summary",
    "memory_auto_flush",
    "memory_extraction",
    "dynamic_workflow",
    "knowledge_chunk_context",
    "knowledge_graph",
    "live_voice",
    "realtime_voice",
    "skill_learning",
    "routing",
    "room_topic",
    "schedule_parse",
    "team_mode",
    "thread_summary",
    "voice_normalization",
    "voice_transcription",
]
# ``pre_ledger`` is the row holding the usage a session's stored totals counted beyond its run rows when this ledger
# replaced them (``session_storage.legacy_usage_totals``): part of the session's totals, with no date or requester.
type UsageKind = Literal["run", "pre_ledger"] | IndependentUsageKind
USAGE_KINDS: frozenset[str] = frozenset({"run", "pre_ledger", *get_args(IndependentUsageKind.__value__)})

SYSTEM_USAGE_ENTITY = "system:internal"
SYSTEM_USAGE_STORAGE_NAME = "system"
# The owner the system usage database records: the internal entity, as an agent.
SYSTEM_USAGE_OWNER = HistoryScope(kind="agent", scope_id=SYSTEM_USAGE_ENTITY)


def run_usage_snapshot(record: AnyRunRecord) -> dict[str, object]:
    """Return the ledger snapshot of ``record``'s usage, without the times of the save that records it.

    It keeps the run's attribution, its requester, its token counters in total and per model, and its requests: those
    mid-turn compaction carried out of its messages, then those its messages answered (``message_request_snapshot``),
    never conversation content. A run resumed from a row worker code wrote can hold values of any shape: an
    attribution value that is not a scalar is left out, and a parent or a counter of another shape becomes ``False``
    for the reporter (``project_token_counters``).
    """
    team_id = record.team_id if isinstance(record, TeamRunRecord) else None
    attribution = {
        "run_id": record.run_id,
        "parent_run_id": record.parent_run_id,
        "team_id": team_id,
        "user_id": record.user_id,
        "model_provider": record.model_provider,
        "model": record.model,
    }
    snapshot: dict[str, object] = {
        key: value for key, value in attribution.items() if isinstance(value, (str, int, float))
    }
    if record.parent_run_id is not None and "parent_run_id" not in snapshot:
        snapshot["parent_run_id"] = False
    metadata = record.metadata if isinstance(record.metadata, dict) else {}
    requester = metadata.get("requester_id")
    if isinstance(requester, str):
        snapshot["metadata"] = {"requester_id": requester}
    snapshot["metrics"] = {} if record.metrics is None else _project_run_metrics(record.metrics)
    requests = (message_request_snapshot(message) for message in record.messages)
    snapshot["requests"] = [
        *_carried_requests(record, metadata),
        *(request for request in requests if request is not None),
    ]
    return snapshot


def request_usage(message: Message) -> RequestUsage | None:
    """Return the usage of the provider request ``message`` answered, or None when it is not one.

    That is an assistant message of the run that holds it, not a history copy, that reported token counters, unless
    they sum a retried request's attempts, which describe no one request. Its model is the one its provenance stamp
    records; a message stored before stamps existed and a stamp of another shape name none alike, which
    ``message_request_snapshot`` tells apart.
    """
    provider_data = _provider_data(message)
    if (
        message.role != "assistant"
        or message.from_history
        or usage_sums_attempts(provider_data)
        or not message.metrics.reported
    ):
        return None
    return RequestUsage(
        created_at=message.created_at,
        metrics=message.metrics,
        model=read_model_provenance(provider_data),
    )


def message_request_snapshot(message: Message) -> dict[str, object] | None:
    """Return the ledger snapshot of the request ``message`` answered (``request_usage``), or None when it is not one.

    A provenance stamp of another shape, as a row worker code wrote can hold, stays visible to the reporter as invalid
    attribution (``False``), apart from a message stored before stamps existed, whose model the report infers.
    """
    request = request_usage(message)
    if request is None:
        return None
    snapshot = _project_request(request)
    assert snapshot is not None, "a request reported counters, so it projects to a snapshot"
    if request.model is None and MODEL_PROVENANCE_KEY in (_provider_data(message) or {}):
        snapshot["model"] = snapshot["model_provider"] = False
    return snapshot


def _provider_data(message: Message) -> dict[str, object] | None:
    # A message read from a row worker code wrote can hold provider data of any shape.
    return message.provider_data if isinstance(message.provider_data, dict) else None


def _carried_requests(record: AnyRunRecord, metadata: Mapping[str, object]) -> list[dict[str, object]]:
    """Return the usage mid-turn compaction carried out of the run's messages, minus any message it still saves.

    A reply that failed after a compaction can still hold some folded messages, which already count.
    """
    carried = metadata.get(COMPACTED_REQUESTS_METADATA_KEY)
    if not isinstance(carried, list):
        return []
    saved = {message.id for message in record.messages}
    requests: list[dict[str, object]] = []
    for item in carried:
        if not isinstance(item, dict):
            continue
        entry = cast("dict[str, object]", item)
        if entry.get("message_id") not in saved:
            requests.append({key: value for key, value in entry.items() if key != "message_id"})
    return requests


def helper_usage_snapshot(
    usage: HelperUsage,
    *,
    usage_id: str,
    kind: IndependentUsageKind,
    requester_id: str | None,
) -> dict[str, object]:
    """Return the snapshot of helper usage recorded as ``usage_id`` for ``requester_id``, with a run snapshot's keys.

    A model or provider left unknown is omitted, and a request that reported no counters is not listed.
    """
    snapshot: dict[str, object] = {"run_id": usage_id, "parent_run_id": None, "team_id": None, "user_id": requester_id}
    if usage.provider is not None:
        snapshot["model_provider"] = usage.provider
    if usage.model is not None:
        snapshot["model"] = usage.model
    snapshot["created_at"] = usage.created_at
    if usage.voice is not None:
        snapshot["voice_seconds"] = usage.voice.seconds
        snapshot["voice_finalized"] = usage.voice.finalized
    snapshot["metrics"] = _project_run_metrics(usage.metrics)
    if usage.voice is None:
        snapshot["requests"] = [request for request in map(_project_request, usage.requests) if request is not None]
    snapshot["kind"] = kind
    return snapshot


def _project_request(request: RequestUsage) -> dict[str, object] | None:
    if not request.metrics.reported:
        return None
    projected: dict[str, object] = {
        "created_at": request.created_at,
        "metrics": project_token_counters(request.metrics.to_dict()),
    }
    if request.model is not None:
        projected["model"] = request.model.model_id
        projected["model_provider"] = request.model.provider
    return projected


def _project_run_metrics(metrics: RunMetrics) -> dict[str, object]:
    selected = project_token_counters(metrics.to_dict())
    if metrics.details:
        selected["details"] = {
            "model": [
                project_model_entry(entry.to_dict()) for entries in metrics.details.values() for entry in entries
            ],
        }
    return selected


def project_model_entry(entry: Mapping[str, object]) -> dict[str, object]:
    """Return a stored model entry's token counters as ``project_token_counters`` does, and its string model names."""
    model = project_token_counters(entry)
    for key in ("id", "provider"):
        if isinstance(entry.get(key), str):
            model[key] = entry[key]
    return model


def project_token_counters(metrics: Mapping[str, object]) -> dict[str, object]:
    """Return the token counters of a stored usage dict, each that is not a scalar as ``False``.

    The reporter tells such an invalid counter from an absent one, and no nested value is ever kept.
    """
    return {
        key: value if isinstance(value, (int, float, str, type(None))) else False
        for key in TOKEN_FIELDS
        if (value := metrics.get(key)) is not None
    }


def stored_token_counts(values: Mapping[str, object]) -> TokenCounts | None:
    """Return the token counters a stored usage dict holds, or None when it holds none.

    A whole float or a short digit string counts, as rows older releases wrote can hold them. Raises ValueError for any
    other counter that is not a nonnegative whole number, so a report never counts part of a damaged dict.
    """
    if all(values.get(name) is None for name in TOKEN_FIELDS):
        return None
    return TokenCounts(**{name: _stored_count(values.get(name)) for name in TOKEN_FIELDS})


def _stored_count(value: object) -> int:
    if value is None:
        return 0
    if isinstance(value, int) and not isinstance(value, bool):
        count = value
    elif (isinstance(value, float) and value.is_integer()) or (isinstance(value, str) and len(value) <= 32):
        count = int(value)
    else:
        raise ValueError
    if count < 0:
        raise ValueError
    return count
