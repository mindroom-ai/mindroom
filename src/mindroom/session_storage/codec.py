"""Run rows to run records and back.

A row's ``run_type`` column says which record its ``run_data`` holds; the JSON keys are the ones
``mindroom.llm.runs`` reads and writes.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from mindroom.llm.runs import RunRecord, TeamRunRecord
from mindroom.session_storage.legacy_tool_results import split_combined_tool_results

if TYPE_CHECKING:
    from mindroom.llm.runs import AnyRunRecord
    from mindroom.session_storage.types import SessionKind


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
    "run_from_row",
    "run_type",
]
