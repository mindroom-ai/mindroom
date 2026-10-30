"""JSON schema constants for the custom toolkits that author their functions' schemas."""

from __future__ import annotations

_JSON_VALUE_SCHEMA: dict[str, object] = {
    "anyOf": [
        {"type": "object", "additionalProperties": True},
        {"type": "array", "items": {}},
        {"type": "string"},
        {"type": "number"},
        {"type": "integer"},
        {"type": "boolean"},
        {"type": "null"},
    ],
}
JSON_OBJECT_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": _JSON_VALUE_SCHEMA,
}
