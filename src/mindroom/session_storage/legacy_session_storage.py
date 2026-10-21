"""Compatibility for retired run metadata in older session payloads."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agno.run.team import TeamRunOutput

if TYPE_CHECKING:
    from collections.abc import Iterable

    from agno.run.agent import RunOutput


# LEGACY_COMPAT: Run metadata carrying a copy of every model-visible tool schema.
# Legacy format: Run rows whose `metadata` holds a `tools_schema` list of tool definitions, written with every run.
# Last legacy release: v2026.10.281; replacement: the unreleased run metadata omits tool schemas.
# Handling: Each loaded run, including member responses embedded in a paused team run, drops the key, so cached and
# replayed runs stop holding the schemas; rows written by current releases omit it.
# Coverage: tests/test_agent_storage_runs.py::test_loaded_runs_drop_their_stored_tool_schemas,
# tests/test_agent_storage_runs.py::test_loaded_team_runs_drop_tool_schemas_of_embedded_member_responses.
def drop_run_tool_schemas(runs: Iterable[RunOutput | TeamRunOutput]) -> None:
    """Drop the per-run tool schema copy that older releases stored in run metadata."""
    for run in runs:
        if run.metadata:
            run.metadata.pop("tools_schema", None)
        if isinstance(run, TeamRunOutput):
            drop_run_tool_schemas(run.member_responses)
