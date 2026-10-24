# Adapted from Agno (https://github.com/agno-agi/agno), Copyright 2025-2026 Agno Inc., Apache License 2.0; modified by MindRoom.
"""Tool-result messages stored in a shape no reader expects, split when their run row is decoded."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.llm.messages import Message
from mindroom.llm.runs import TeamRunRecord

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.llm.runs import AnyRunRecord

__all__ = ["split_combined_tool_results"]


# LEGACY_COMPAT: Combined Gemini tool-result messages.
# Legacy format: one role="tool" message with tool_call_id=None whose tool_calls entries each carry
# tool_call_id, tool_name, and content; selected when a tool message has no tool_call_id but has tool_calls.
# Last legacy release: v2026.4.35 stored them through Agno 2.4.7's Gemini adapter; v2026.4.36 (Agno 2.5.13)
# writes one canonical tool message per result.
# Handling: every decoded run row, member responses included, carries each entry as its own tool message, taking
# content from the combined content list when present, so replay on every route, compaction input, and the
# tool-call windows see one result per call, and a rewritten row stores that shape; the first split message keeps
# the combined message's id, time, and metrics, and the others derive their ids from it.
# Coverage: tests/test_history_legacy_tool_results.py::test_combined_results_replay_as_one_history_message_per_result,
#   tests/test_history_legacy_tool_results.py::test_tool_call_window_counts_each_combined_result,
#   tests/test_history_legacy_tool_results.py::test_compaction_reads_each_combined_result_as_its_own_tool_result,
#   tests/test_history_legacy_tool_results.py::test_a_team_runs_member_responses_decode_each_combined_result,
#   tests/test_history_legacy_tool_results.py::test_combined_results_reach_every_route
def split_combined_tool_results(run: AnyRunRecord) -> None:
    """Expand the legacy combined tool-result messages of ``run`` and its member runs into one message per result."""
    run.messages = _split(run.messages)
    if isinstance(run, TeamRunRecord):
        for member in run.member_responses:
            member.messages = _split(member.messages)


def _split(messages: Sequence[Message]) -> list[Message]:
    result: list[Message] = []
    for message in messages:
        if message.role != "tool" or message.tool_call_id is not None or not message.tool_calls:
            result.append(message)
            continue
        contents = message.content if isinstance(message.content, list) else []
        for index, entry in enumerate(message.tool_calls):
            split = Message(
                role="tool",
                # Ids derived from the combined message keep every decode of the row the same.
                id=message.id if index == 0 else f"{message.id}:{index}",
                created_at=message.created_at,
                tool_call_id=entry.get("tool_call_id"),
                tool_name=entry.get("tool_name"),
                content=contents[index] if index < len(contents) else entry.get("content", ""),
            )
            if index == 0:
                split.metrics = message.metrics
            result.append(split)
    return result
