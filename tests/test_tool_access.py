"""Transport-neutral tool discovery, schema, and validation helpers."""

from __future__ import annotations

import pytest
from agno.tools.function import Function

from mindroom.tool_system.tool_access import (
    ToolDescriptor,
    ToolKey,
    function_schema,
    search_tool_metadata,
    validate_tool_arguments,
)


def test_tool_descriptors_keep_same_named_functions_namespaced() -> None:
    """Selecting a namespaced descriptor returns only that tool's details."""
    calendar = ToolDescriptor(
        key=ToolKey(toolkit="calendar", function="list"),
        description="List calendar events",
        input_schema={"type": "object", "properties": {"date": {"type": "string"}}},
        instructions=("Use ISO dates.",),
    )
    files = ToolDescriptor(
        key=ToolKey(toolkit="files", function="list"),
        description="List files",
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
        instructions=("Use an absolute path.",),
    )

    catalog = {calendar.key: calendar, files.key: files}

    assert calendar.key != files.key
    assert catalog[ToolKey(toolkit="files", function="list")] == files
    assert catalog[ToolKey(toolkit="files", function="list")].input_schema == {
        "type": "object",
        "properties": {"path": {"type": "string"}},
    }
    assert catalog[ToolKey(toolkit="files", function="list")].instructions == ("Use an absolute path.",)


def test_function_schema_prepares_a_copy_without_mutating_the_function() -> None:
    """Schema preparation rebuilds a selected function while preserving its live binding."""

    def list_files(path: str) -> str:
        return path

    required: list[object] = []
    required.append(required)
    function = Function(
        name="list",
        entrypoint=list_files,
        parameters={"type": "object", "properties": {"path": {"type": "string"}}, "required": required},
    )

    schema = function_schema(function)

    assert schema["required"] == ["path"]
    assert function.parameters["required"][0] is function.parameters["required"]
    assert function.entrypoint is list_files


def test_search_tool_metadata_preserves_catalog_order_and_requires_all_words() -> None:
    """Keyword matching is stable and searches every public metadata value."""
    items = [
        {"toolkit": "calendar", "function": "list", "description": "List calendar events"},
        {"toolkit": "files", "function": "list", "description": "List shared files"},
        {"toolkit": "files", "function": "read", "description": "Read a file"},
    ]

    assert search_tool_metadata(items, "files list", 5) == [items[1]]
    assert search_tool_metadata(items, "", 2) == items[:2]


def test_validate_tool_arguments_rejects_malformed_and_remote_schemas() -> None:
    """Malformed arguments and network-resolved schemas fail before any tool body can run."""
    schema = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
        "additionalProperties": False,
    }

    validate_tool_arguments(schema, {"path": "/documents"})
    with pytest.raises(ValueError, match="Invalid tool arguments"):
        validate_tool_arguments(schema, {"path": 42})
    with pytest.raises(ValueError, match="Invalid tool arguments"):
        validate_tool_arguments({"$ref": "https://schema.example.org/remote.json"}, {})
