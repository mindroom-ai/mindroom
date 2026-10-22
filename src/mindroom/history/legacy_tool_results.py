# Adapted from Agno (https://github.com/agno-agi/agno), Copyright 2025-2026 Agno Inc., Apache License 2.0; modified by MindRoom.
"""Tool-result messages stored in a shape no provider route reads, split before history replays them."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.llm.messages import Message

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["split_combined_tool_results"]


# LEGACY_COMPAT: Combined Gemini tool-result messages.
# Legacy format: one role="tool" message with tool_call_id=None whose tool_calls entries each carry
# tool_call_id, tool_name, and content; selected when a tool message has no tool_call_id but has tool_calls.
# Last legacy release: v2026.4.35 stored them through Agno 2.4.7's Gemini adapter; v2026.4.36 (Agno 2.5.13)
# writes one canonical tool message per result.
# Handling: replayed history carries each entry as its own tool message, taking content from the combined content
# list when present, so every route and the tool-call window see one result per call; the first split message keeps
# the combined message's metrics, and the stored run is unchanged.
# Coverage: tests/test_history_legacy_tool_results.py::test_combined_results_replay_as_one_history_message_per_result,
#   tests/test_history_legacy_tool_results.py::test_tool_call_window_counts_each_combined_result,
#   tests/test_history_legacy_tool_results.py::test_combined_results_reach_every_route
def split_combined_tool_results(messages: Sequence[Message]) -> list[Message]:
    """Expand legacy combined tool-result messages into one canonical message per result."""
    result: list[Message] = []
    for message in messages:
        if message.role != "tool" or message.tool_call_id is not None or not message.tool_calls:
            result.append(message)
            continue
        contents = message.content if isinstance(message.content, list) else []
        for index, entry in enumerate(message.tool_calls):
            split = Message(
                role="tool",
                tool_call_id=entry.get("tool_call_id"),
                tool_name=entry.get("tool_name"),
                content=contents[index] if index < len(contents) else entry.get("content", ""),
            )
            if index == 0:
                split.metrics = message.metrics
            result.append(split)
    return result
