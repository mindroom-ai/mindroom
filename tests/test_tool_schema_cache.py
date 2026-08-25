"""Tests for the shared prompt tool-schema cache."""

from __future__ import annotations

import gc
import inspect
from functools import partial
from typing import TYPE_CHECKING
from weakref import ref

import pytest
from agno.tools.function import Function

from mindroom.tool_schema_cache import (
    cached_processed_schema,
    clear_tool_schema_cache,
    set_schema_cache_postprocessor,
)
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


def test_cached_processed_schema_matches_output_wrapper_documentation_and_user_input(tmp_path: Path) -> None:
    """Cached output-path schemas must retain every wrapper-provided prompt field."""

    def export_report(report_id: str, include_notes: bool = False) -> str:
        """Export one report.

        Args:
            report_id: Report to export.
            include_notes: Whether to include notes.

        """
        return f"{report_id}:{include_notes}"

    function = Function(name="export_report", entrypoint=export_report, requires_user_input=True)
    wrap_function_for_output_files(function, ToolOutputFilePolicy(workspace_root=tmp_path))

    cached = cached_processed_schema(function, strict=True)
    live = function.model_copy(deep=True)
    live.process_entrypoint(strict=True)

    assert cached is not None
    assert cached.parameters == live.parameters
    assert cached.description == live.description
    assert cached.user_input_schema == tuple(live.user_input_schema)
    assert [field.name for field in cached.user_input_schema] == [
        "report_id",
        "include_notes",
        OUTPUT_PATH_ARGUMENT,
    ]

    cached.user_input_schema[0].description = "changed"

    second = cached_processed_schema(function, strict=True)

    assert second is not None
    assert second.user_input_schema[0].description is None
    assert second.user_input_schema[0] is not cached.user_input_schema[0]


def test_cached_processed_schema_reuses_independently_wrapped_functions_without_unwrapping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Equivalent output wrappers must share cached work without changing inspect.unwrap."""

    def export_report(report_id: str) -> str:
        """Export one report."""
        return report_id

    clear_tool_schema_cache()
    original_process_entrypoint = Function.process_entrypoint
    process_entrypoint_calls = 0

    def counting_process_entrypoint(self: Function, strict: bool = False) -> None:
        nonlocal process_entrypoint_calls
        process_entrypoint_calls += 1
        original_process_entrypoint(self, strict=strict)

    monkeypatch.setattr(Function, "process_entrypoint", counting_process_entrypoint)
    first_function = Function(name="export_report", entrypoint=export_report)
    second_function = Function(name="export_report", entrypoint=export_report)
    wrap_function_for_output_files(first_function, ToolOutputFilePolicy(workspace_root=tmp_path))
    wrap_function_for_output_files(second_function, ToolOutputFilePolicy(workspace_root=tmp_path))

    first = cached_processed_schema(first_function, strict=False)
    second = cached_processed_schema(second_function, strict=False)

    assert first is not None
    assert second is not None
    assert process_entrypoint_calls == 1
    assert inspect.unwrap(first_function.entrypoint) is first_function.entrypoint
    clear_tool_schema_cache()


def test_cached_processed_schema_rejects_plain_custom_processor_without_opt_in() -> None:
    """An unregistered plain custom processor must fall back to live processing."""

    def export_report(report_id: str) -> str:
        return report_id

    def custom_processor(strict: bool = False) -> None:
        del strict

    function = Function(name="export_report", entrypoint=export_report)
    object.__setattr__(function, "process_entrypoint", custom_processor)

    assert cached_processed_schema(function, strict=False) is None


def test_cached_processed_schema_rejects_partial_custom_processor_without_opt_in() -> None:
    """An unregistered partial custom processor must fall back to live processing."""

    def export_report(report_id: str) -> str:
        return report_id

    function = Function(name="export_report", entrypoint=export_report)
    object.__setattr__(function, "process_entrypoint", partial(Function.process_entrypoint, function))

    assert cached_processed_schema(function, strict=False) is None


def test_set_schema_cache_postprocessor_rejects_nested_functions() -> None:
    """Cache processors must have stable module-level identity."""

    def export_report(report_id: str) -> str:
        return report_id

    def nested_postprocessor(function: Function, strict: bool) -> None:
        function.process_entrypoint(strict=strict)

    function = Function(name="export_report", entrypoint=export_report)

    with pytest.raises(TypeError, match="module-level"):
        set_schema_cache_postprocessor(function, nested_postprocessor)


def test_cached_processed_schema_uses_bound_method_cache_source(tmp_path: Path) -> None:
    """Output-path wrapping must retain cache support for toolkit-bound methods."""

    class ReportExporter:
        def export_report(self, report_id: str) -> str:
            return report_id

    function = Function(name="export_report", entrypoint=ReportExporter().export_report)

    wrap_function_for_output_files(function, ToolOutputFilePolicy(workspace_root=tmp_path))

    snapshot = cached_processed_schema(function, strict=False)

    assert snapshot is not None
    assert snapshot.parameters["required"] == ["report_id"]


def test_cached_processed_schema_does_not_retain_wrapped_method_owner(tmp_path: Path) -> None:
    """Cached snapshots must not keep a wrapper's bound-method owner alive."""

    class ReportExporter:
        def export_report(self, report_id: str) -> str:
            return report_id

    clear_tool_schema_cache()
    owner = ReportExporter()
    owner_ref = ref(owner)
    function = Function(name="export_report", entrypoint=owner.export_report)
    wrap_function_for_output_files(function, ToolOutputFilePolicy(workspace_root=tmp_path))

    try:
        first = cached_processed_schema(function, strict=False)
        assert first is not None

        del function
        del owner
        gc.collect()

        assert owner_ref() is None

        next_function = Function(name="export_report", entrypoint=ReportExporter().export_report)
        wrap_function_for_output_files(next_function, ToolOutputFilePolicy(workspace_root=tmp_path))

        second = cached_processed_schema(next_function, strict=False)

        assert second == first
    finally:
        clear_tool_schema_cache()
