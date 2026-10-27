"""Run rows to run records and back.

A run's stored dict (``record.to_dict()``) spreads over its ``*_log_runs`` row (``run_columns``), one message row per
message, and one tool row per tool execution; ``run_record`` joins them again. The JSON keys are the ones
``mindroom.llm.runs`` reads and writes.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mindroom.llm.runs import RunRecord, TeamRunRecord

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.llm.runs import AnyRunRecord
    from mindroom.session_storage.types import SessionKind

# The keys of a stored run dict that a run row holds in columns of their own, or in rows of other tables:
# messages and tool executions in tables of their own, and a paused team run's members in their own run rows.
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


# Escaped backslashes match first, so no escape starts inside one; surrogate pairs, which ``json.dumps`` writes for every
# character beyond the Basic Multilingual Plane, match before a lone surrogate's escape.
_SURROGATE_ESCAPES = re.compile(
    r"\\\\|\\u[dD][89abAB][0-9a-fA-F]{2}\\u[dD][c-fC-F][0-9a-fA-F]{2}|\\u[dD][89a-fA-F][0-9a-fA-F]{2}",
)
# Text without this holds no surrogate escape, so it skips the substitution.
_SURROGATE_ESCAPE_START = re.compile(r"\\u[dD]")


def decode_json(text: str | bytes | bytearray) -> object:
    """Decode JSON text worker code may have written, with each lone surrogate in it read as a replacement character.

    A lone surrogate decodes to a string SQLite cannot store, so writing it would fail the conversion. Bytes that are not
    text in their encoding read as replacement characters too.
    """
    if not isinstance(text, str):
        text = bytes(text).decode(json.detect_encoding(text), "replace")
    if _SURROGATE_ESCAPE_START.search(text):
        text = _SURROGATE_ESCAPES.sub(_kept_or_replaced, text)
    return json.loads(text)


def _kept_or_replaced(match: re.Match[str]) -> str:
    """Keep an escaped backslash or a surrogate pair, and replace a lone surrogate's escape."""
    return "\\ufffd" if len(match.group()) == len("\\ud800") else match.group()


@dataclass(frozen=True, slots=True)
class RunColumns:
    """The columns of a run row that its stored dict fills, the ids, the messages, and the tool executions aside.

    ``record`` is the JSON of every other key. ``requirements`` and ``metadata`` are JSON, or None when the record has
    none, so ``[]`` and ``{}`` stay distinct from none; ``has_tools`` does the same for the tool list, whose entries
    are rows of their own.
    """

    kind: SessionKind
    scope_key: str
    status: str
    has_tools: bool
    record: str
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
        has_tools=data.get("tools") is not None,
        record=json.dumps({key: value for key, value in data.items() if key not in _NOT_IN_RECORD}),
        requirements=_encoded(data.get("requirements")),
        metadata=_encoded(data.get("metadata")),
    )


def run_record(
    run_id: str,
    session_id: str,
    parent_run_id: str | None,
    columns: RunColumns,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
) -> AnyRunRecord:
    """Decode a run row, its messages' stored dicts in replay order, and its tool executions' in order, into its record.

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
        "tools": tools if columns.has_tools else None,
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
    "decode_json",
    "run_columns",
    "run_kind",
    "run_record",
]
