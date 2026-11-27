"""Transport-neutral tool discovery, schema, and validation helpers."""

from __future__ import annotations

from dataclasses import replace
from typing import Optional

import pytest

from mindroom.llm.provider import ToolSpec
from mindroom.tool_system.authoring import passthrough_tool
from mindroom.tool_system.tool_access import (
    search_tool_metadata,
    tool_definition,
    validate_tool_arguments,
)
from mindroom.tool_system.toolkits import adapt_callable


def test_adapted_schema_is_a_copy_that_leaves_the_function_unchanged() -> None:
    """Adapting a function derives the schema the model sees while preserving the live function and its binding."""

    def list_files(path: str) -> str:
        return path

    parameters = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
    marked = passthrough_tool(name="list", parameters=parameters)(list_files)

    adapted = adapt_callable(marked)

    assert adapted.parameters == parameters
    assert adapted.parameters is not parameters
    assert adapted.entrypoint is list_files


def test_a_tool_definition_shows_the_model_its_name_description_and_schema_only() -> None:
    """Strict mode and the approval flags are each route's to send, and a missing description shows as empty."""
    parameters = {"type": "object", "properties": {"path": {"type": "string"}}}
    tool = ToolSpec(
        name="list",
        description=None,
        parameters=parameters,
        strict=True,
    )

    definition = tool_definition(tool)

    assert definition == {"name": "list", "description": "", "parameters": parameters}
    assert definition["parameters"] is not parameters


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


def test_validate_tool_arguments_allows_explicit_null_only_for_derived_nullable_optional_parameters() -> None:
    """Explicit JSON ``null`` is admitted for derived ``X | None = None`` params, not non-nullable or authored ones."""

    def lookup(
        query: str,
        tag: str | None = None,
        legacy: Optional[int] = None,  # noqa: UP045
        count: int = 5,
    ) -> str:
        return f"{query}:{tag}:{legacy}:{count}"

    derived = adapt_callable(lookup)
    assert derived.parameters["properties"]["tag"] == {"type": "string"}
    assert derived.parameters["properties"]["legacy"] == {"type": "integer"}
    assert derived.parameters["properties"]["count"] == {"type": "integer"}
    assert derived.nullable_optional_parameters == frozenset({"tag", "legacy"})

    # Derived nullable optional parameters still admit explicit null via ToolFunction validation.
    validate_tool_arguments(derived, {"query": "hi", "tag": None, "legacy": None})
    validate_tool_arguments(derived, {"query": "hi"})
    with pytest.raises(ValueError, match="Invalid tool arguments"):
        validate_tool_arguments(derived, {"query": "hi", "tag": 123})
    # Non-nullable optional parameters with a concrete default still reject explicit null.
    with pytest.raises(ValueError, match="Invalid tool arguments"):
        validate_tool_arguments(derived, {"query": "hi", "count": None})
    # A property promoted to required or hidden by offered_function still rejects explicit null.
    required_tag = replace(derived, parameters={**derived.parameters, "required": ["query", "tag"]})
    with pytest.raises(ValueError, match="Invalid tool arguments"):
        validate_tool_arguments(required_tag, {"query": "hi", "tag": None})
    hidden_tag = replace(
        derived,
        parameters={
            **derived.parameters,
            "properties": {"query": {"type": "string"}},
            "additionalProperties": False,
        },
    )
    with pytest.raises(ValueError, match="Invalid tool arguments"):
        validate_tool_arguments(hidden_tag, {"query": "hi", "tag": None})

    # Authored schemas and raw dicts without null still reject explicit null.
    authored_schema = {
        "type": "object",
        "properties": {"query": {"type": "string"}, "tag": {"type": "string"}},
        "required": ["query"],
    }

    @passthrough_tool(name="authored_lookup", parameters=authored_schema)
    def authored_lookup(query: str, tag: str | None = None) -> str:
        return f"{query}:{tag}"

    authored = adapt_callable(authored_lookup)
    assert authored.nullable_optional_parameters == frozenset()
    with pytest.raises(ValueError, match="Invalid tool arguments"):
        validate_tool_arguments(authored, {"query": "hi", "tag": None})
    with pytest.raises(ValueError, match="Invalid tool arguments"):
        validate_tool_arguments(authored_schema, {"query": "hi", "tag": None})
