"""Run rows to run records and back.

A run's stored dict (``record.to_dict()``) spreads over its ``*_log_runs`` row (``run_columns``) and one message row per
message; ``run_record`` joins them again. The JSON keys are the ones ``mindroom.llm.runs`` reads and writes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mindroom.llm.runs import RunRecord, TeamRunRecord

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.llm.runs import AnyRunRecord
    from mindroom.session_storage.types import SessionKind

# The keys of a stored run dict that a run row holds in columns of their own, or in rows of other tables:
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
    """The columns of a run row that its stored dict fills, the ids and the messages aside.

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
    """Decode a run row and its messages' stored dicts, in replay order, into its record.

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


def run_kind(run: AnyRunRecord) -> SessionKind:
    """Return the ``kind`` of ``run``'s row."""
    return "team" if isinstance(run, TeamRunRecord) else "agent"


__all__ = [
    "RunColumns",
    "run_columns",
    "run_kind",
    "run_record",
]
