"""Claude block names, the rebuilt-block type, and a dict helper shared by the Claude request modules."""

from __future__ import annotations

from typing import Any, cast

# The ``Message.provider_data`` key under which a Claude assistant turn stores its response's blocks, in order and in
# the shape a later request sends them.
CLAUDE_BLOCKS_KEY = "claude_blocks"
TOOL_SEARCH_TOOL_TYPE = "tool_search_tool_regex_20251119"
TOOL_SEARCH_TOOL_NAME = "tool_search_tool_regex"
SERVER_TOOL_USE_BLOCK_TYPE = "server_tool_use"
TOOL_SEARCH_RESULT_BLOCK_TYPE = "tool_search_tool_result"


class RebuiltBlock(dict[str, Any]):
    """A Claude assistant block MindRoom rebuilt from a message's fields, not one a stored Claude response holds.

    It goes on the wire as the dict it is. The cache ladder places no breakpoint on it, so a breakpoint in an
    assistant turn sits only on a block a stored Claude response holds, which every later request echoes verbatim;
    a turn rebuilt from another provider's reply, or from the agent's own reply read back from the room, takes none.
    """

    __slots__ = ()


def as_dict(value: object) -> dict[str, Any] | None:
    """Return the value as a string-keyed dict when possible."""
    return cast("dict[str, Any]", value) if isinstance(value, dict) else None
