"""Run rows to run records and back.

A row's ``run_type`` column says which record its ``run_data`` holds; the JSON keys are the ones
``mindroom.llm.runs`` reads and writes.

Storage v2 splits a run's stored dict over its ``*_log_runs`` row (``run_columns``) and one message row per message;
``run_record`` joins them again.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mindroom.llm.runs import RunRecord, TeamRunRecord
from mindroom.session_storage.legacy_tool_results import split_combined_tool_results

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.llm.runs import AnyRunRecord
    from mindroom.session_storage.types import SessionKind

# The keys of a stored run dict that a storage v2 run row holds in columns of their own, or in rows of other tables:
# messages in the messages table, and a paused team run's members in their own run rows.
_NOT_IN_RECORD = frozenset(
    {
        "run_id",
        "session_id",
        "parent_run_id",
        "status",
        "messages",
        "tools",
        "requirements",
        "metadata",
        "member_responses",
    },
)


@dataclass(frozen=True, slots=True)
class RunColumns:
    """The columns of a storage v2 run row that its stored dict fills, the ids and the messages aside.

    ``record`` is the JSON of every other key. ``tools``, ``requirements``, and ``metadata`` are JSON, or None when
    the record has none, so ``[]`` and ``{}`` stay distinct from none.
    """

    kind: SessionKind
    scope_key: str
    status: str
    record: str
    tools: str | None
    requirements: str | None
    metadata: str | None


def run_columns(data: Mapping[str, Any], kind: SessionKind) -> RunColumns:
    """Return the run-row columns of ``data``, a ``kind`` run's stored dict (``record.to_dict()``).

    ``scope_key`` is the run's ``HistoryScope.key``, ``agent:<agent_id>`` or ``team:<team_id>``; a run without its
    agent or team id gets a key no scope has.
    """
    owner = data.get("team_id" if kind == "team" else "agent_id")
    return RunColumns(
        kind=kind,
        scope_key=f"{kind}:{owner or ''}",
        status=data["status"],
        record=json.dumps({key: value for key, value in data.items() if key not in _NOT_IN_RECORD}),
        tools=_encoded(data.get("tools")),
        requirements=_encoded(data.get("requirements")),
        metadata=_encoded(data.get("metadata")),
    )


def run_record(
    run_id: str,
    session_id: str,
    parent_run_id: str | None,
    columns: RunColumns,
    messages: list[dict[str, Any]],
) -> AnyRunRecord:
    """Decode a storage v2 run row and its messages' stored dicts, in replay order, into its record.

    The row's ids win over any the record JSON holds. A team run decodes without ``member_responses``; the store
    fills them from the member rows.
    """
    data = {
        **json.loads(columns.record),
        "run_id": run_id,
        "session_id": session_id,
        "parent_run_id": parent_run_id,
        "status": columns.status,
        "messages": messages,
        "tools": _decoded(columns.tools),
        "requirements": _decoded(columns.requirements),
        "metadata": _decoded(columns.metadata),
    }
    return TeamRunRecord.from_dict(data) if columns.kind == "team" else RunRecord.from_dict(data)


def _encoded(value: object) -> str | None:
    return None if value is None else json.dumps(value)


def _decoded(text: str | None) -> object:
    return None if text is None else json.loads(text)


def run_from_row(run_type: str, run_data: str) -> AnyRunRecord | None:
    """Decode one run row; a row of any other type than agent or team (Agno's workflow runs) reads as None."""
    if run_type not in ("agent", "team"):
        return None
    data = json.loads(run_data)
    _drop_tool_schemas(data)
    run = TeamRunRecord.from_dict(data) if run_type == "team" else RunRecord.from_dict(data)
    split_combined_tool_results(run)
    return run


def run_type(run: AnyRunRecord) -> SessionKind:
    """Return the ``run_type`` column value of ``run``'s row."""
    return "team" if isinstance(run, TeamRunRecord) else "agent"


# LEGACY_COMPAT: Run metadata carrying a copy of every model-visible tool schema.
# Legacy format: Run rows whose `metadata` holds a `tools_schema` list of tool definitions, written with every run,
# including member responses embedded in a paused team run.
# Last legacy release: v2026.10.281; replacement: v2026.10.282 stopped storing tool schemas in run metadata.
# Handling: Every decoded run row drops the key, from its member responses too, so loaded and replayed runs
# never hold the schemas and a rewritten row no longer stores them; rows written by current releases omit it.
# Coverage: tests/session_storage/test_store.py::test_loaded_runs_drop_their_stored_tool_schemas.
def _drop_tool_schemas(data: dict[str, Any]) -> None:
    for run in (data, *(data.get("member_responses") or ())):
        if isinstance(run, dict) and isinstance(metadata := run.get("metadata"), dict):
            metadata.pop("tools_schema", None)


__all__ = [
    "RunColumns",
    "run_columns",
    "run_from_row",
    "run_record",
    "run_type",
]
