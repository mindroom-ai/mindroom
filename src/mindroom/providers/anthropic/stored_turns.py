"""What a stored Claude assistant turn replays: the blocks it stored, or blocks rebuilt from its message fields.

A Claude reply stores its response's blocks once, in request shape, under ``claude_blocks`` (``wire.py`` writes them);
turns stored in earlier shapes read through ``legacy_stored_turns.py``. Any other assistant turn, such as another
provider's reply or the agent's own reply read back from the room, rebuilds from its text and tool calls.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mindroom.llm.tool_search import call_arguments
from mindroom.providers.anthropic.legacy_stored_turns import current_claude_turn, legacy_rebuilt_blocks
from mindroom.providers.claude_wire_blocks import CLAUDE_BLOCKS_KEY, RebuiltBlock

if TYPE_CHECKING:
    from mindroom.llm.messages import Message

__all__ = ["replayed_blocks"]


def replayed_blocks(message: Message) -> list[dict[str, Any]]:
    """Return the blocks an assistant turn replays: its stored Claude blocks, or a rebuild from its fields.

    A stored ``tool_use`` block is kept only while the message still carries the matching tool call: a response
    cut off by ``max_tokens`` can hold a tool use that never ran, and the API rejects one without a result. Rebuilt
    blocks are ``RebuiltBlock`` dicts, which never take a cache breakpoint.
    """
    message = current_claude_turn(message)
    stored = (message.provider_data or {}).get(CLAUDE_BLOCKS_KEY)
    if isinstance(stored, list):
        tool_call_ids = {call.get("id") for call in message.tool_calls or []}
        return [dict(block) for block in stored if block.get("type") != "tool_use" or block.get("id") in tool_call_ids]
    blocks = legacy_rebuilt_blocks(message)
    if isinstance(message.content, str) and message.content.strip():
        blocks.append(RebuiltBlock(text=message.content, type="text"))
    for call in message.tool_calls or []:
        function = call["function"]
        blocks.append(
            RebuiltBlock(id=call["id"], input=call_arguments(function), name=function["name"], type="tool_use"),
        )
    return blocks
