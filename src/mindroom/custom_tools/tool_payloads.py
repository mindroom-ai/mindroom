"""Shared JSON payload helpers for custom tools."""

from __future__ import annotations

import json


def custom_tool_payload(
    tool_name: str,
    status: str,
    sort_keys: bool = True,
    /,
    **fields: object,
) -> str:
    """Build the JSON payload returned by custom tools."""
    payload: dict[str, object] = {"status": status, "tool": tool_name}
    payload.update(fields)
    return json.dumps(payload, sort_keys=sort_keys)
