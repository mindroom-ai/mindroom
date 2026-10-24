# Adapted from Agno (https://github.com/agno-agi/agno), Copyright 2025-2026 Agno Inc., Apache License 2.0; modified by MindRoom.
"""The strict JSON schema OpenAI structured outputs accept, derived from a pydantic output model."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pydantic import BaseModel

__all__ = ["strict_response_schema"]


def _is_dict_field(schema: dict[str, Any]) -> bool:
    """Return whether the schema describes a ``dict[str, T]`` field, which strict mode cannot close."""
    additional = schema.get("additionalProperties")
    return (
        schema.get("type") == "object"
        and isinstance(additional, dict)
        and "type" in additional
        and "properties" not in schema
    )


def _sanitize(schema: Any) -> None:  # noqa: ANN401 - any JSON schema node
    if isinstance(schema, dict):
        if schema.get("type") == "object":
            if "additionalProperties" not in schema or schema.get("additionalProperties") is True:
                schema["additionalProperties"] = False
            if "properties" in schema:
                schema["required"] = [
                    name
                    for name, property_schema in schema["properties"].items()
                    if not _is_dict_field(property_schema)
                ]
        if "default" in schema and schema["default"] is None:
            schema.pop("default")
        for value in schema.values():
            _sanitize(value)
    elif isinstance(schema, list):
        for item in schema:
            _sanitize(item)


def strict_response_schema(output_model: type[BaseModel]) -> dict[str, Any]:
    """Return ``output_model``'s JSON schema with every object closed, every property required, and null defaults dropped."""
    schema = output_model.model_json_schema()
    _sanitize(schema)
    return schema
