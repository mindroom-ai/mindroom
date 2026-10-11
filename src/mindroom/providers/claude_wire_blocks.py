"""Claude request wire names and a dict helper shared by the Claude request modules."""

from __future__ import annotations

from typing import Any, cast

TOOL_SEARCH_TOOL_TYPE = "tool_search_tool_regex_20251119"
TOOL_SEARCH_TOOL_NAME = "tool_search_tool_regex"
SERVER_TOOL_USE_BLOCK_TYPE = "server_tool_use"
TOOL_SEARCH_RESULT_BLOCK_TYPE = "tool_search_tool_result"


def as_dict(value: object) -> dict[str, Any] | None:
    """Return the value as a string-keyed dict when possible."""
    return cast("dict[str, Any]", value) if isinstance(value, dict) else None
