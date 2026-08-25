"""Tests for the shared prompt tool-schema cache."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agno.tools.function import Function

from mindroom.tool_schema_cache import cached_processed_schema
from mindroom.tool_system.output_files import (
    OUTPUT_PATH_ARGUMENT,
    ToolOutputFilePolicy,
    wrap_function_for_output_files,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_cached_processed_schema_returns_private_copies() -> None:
    """Mutating a returned snapshot must not corrupt the shared LRU entry."""

    def sync_event(title: str, include_attendees: bool = False) -> str:
        """Sync one event."""
        return f"{title}:{include_attendees}"

    function = Function(name="sync_event", entrypoint=sync_event)

    first = cached_processed_schema(function, strict=False)
    assert first is not None
    first.parameters["properties"]["injected"] = {"type": "string"}
    first.parameters["required"].append("injected")

    second = cached_processed_schema(function, strict=False)
    assert second is not None
    assert "injected" not in second.parameters["properties"]
    assert second.parameters["required"] == ["title"]
    assert second.parameters is not first.parameters


def test_cached_processed_schema_applies_output_path_postprocessor_without_sharing_results(tmp_path: Path) -> None:
    """Output-wrapped functions should reuse schema work without changing the live function."""

    def export_report(report_id: str) -> str:
        """Export one report."""
        return report_id

    function = Function(name="export_report", entrypoint=export_report)
    wrap_function_for_output_files(function, ToolOutputFilePolicy(workspace_root=tmp_path))

    first = cached_processed_schema(function, strict=True)

    assert first is not None
    assert first.parameters["properties"][OUTPUT_PATH_ARGUMENT]["default"] is None
    assert first.parameters["required"] == ["report_id"]
    assert OUTPUT_PATH_ARGUMENT not in function.parameters["properties"]

    live = function.model_copy(deep=True)
    live.process_entrypoint(strict=True)
    assert first.parameters == live.parameters
    assert first.description == live.description

    first.parameters["properties"][OUTPUT_PATH_ARGUMENT]["default"] = "changed"

    second = cached_processed_schema(function, strict=True)

    assert second is not None
    assert second.parameters["properties"][OUTPUT_PATH_ARGUMENT]["default"] is None
