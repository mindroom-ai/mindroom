"""Shared parsing for tool settings that the dashboard stores as JSON objects."""

from __future__ import annotations

import json


def parse_string_mapping(value: dict[str, str] | str | None, *, field_name: str) -> dict[str, str] | None:
    """Parse one JSON-authored mapping while preserving native mappings."""
    if value is None or isinstance(value, dict):
        return value
    if not value.strip():
        return None
    msg = f"{field_name} must be a JSON object with string keys and values"
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        raise ValueError(msg) from None
    if not isinstance(parsed, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in parsed.items()
    ):
        raise ValueError(msg)
    return parsed
