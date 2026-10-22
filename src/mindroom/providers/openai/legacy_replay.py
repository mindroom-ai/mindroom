"""Historical message shapes the OpenAI-family formatters still read from stored runs.

Each repair returns request-only copies; stored history is never rewritten.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

from mindroom.llm.messages import Message

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["repair_legacy_responses_span", "repair_legacy_tool_replay", "split_combined_tool_results"]


# LEGACY_COMPAT: Combined Gemini tool-result messages.
# Legacy format: one role="tool" message with tool_call_id=None whose tool_calls entries each carry
# tool_call_id, tool_name, and content; selected when a tool message has no tool_call_id but has tool_calls.
# Last legacy release: v2026.4.35 stored them through Agno 2.4.7's Gemini adapter; v2026.4.36 (Agno 2.5.13)
# writes one canonical tool message per result.
# Handling: each entry becomes its own tool message, taking content from the combined content list when present,
# so every wire sees one result per call; the first split message keeps the combined message's metrics.
# Coverage: tests/providers/test_openai_family_wire.py::test_combined_gemini_tool_results_split_into_canonical_results
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


# LEGACY_COMPAT: Persisted Anthropic tool calls without empty arguments.
# Legacy format: Agno writers omitted the arguments field for empty Anthropic tool calls.
# Last legacy release: v2026.9.43; replacement: v2026.9.44 included the upstream Agno 3.0.7 writer fix.
# Handling: Supply `{}` arguments in copies of affected calls until those histories are migrated or dropped.
# Coverage: tests/providers/test_openai_family_wire.py::test_legacy_tool_replay_supplies_missing_arguments


# LEGACY_COMPAT: Persisted sparse OpenAI tool placeholders and orphan results.
# Legacy format: Streamed OpenAI history retained id-only tool placeholders and their orphan tool results.
# Last legacy release: v2026.7.172; replacement: v2026.7.173 filtered sparse placeholders during writes.
# Handling: Remove each placeholder and its matching result while preserving unchanged message identities.
# Coverage: tests/providers/test_openai_family_wire.py::test_legacy_tool_replay_removes_placeholders_and_orphans
def repair_legacy_tool_replay(messages: Sequence[Message]) -> list[Message]:
    """Supply missing call arguments and drop id-only placeholder calls with their results."""
    repaired: list[Message] = []
    removed_ids: set[str] = set()
    for message in messages:
        if message.role == "tool" and message.tool_call_id in removed_ids:
            continue
        if message.role != "assistant" or not message.tool_calls:
            repaired.append(message)
            continue
        calls: list[dict[str, Any]] = []
        changed = False
        for call in message.tool_calls:
            function = call.get("function")
            if not isinstance(function, dict):
                if isinstance(call_id := call.get("id"), str):
                    removed_ids.add(call_id)
                changed = True
            elif "arguments" not in function:
                calls.append({**call, "function": {**function, "arguments": "{}"}})
                changed = True
            else:
                calls.append(call)
        repaired.append(replace(message, tool_calls=calls) if changed else message)
    return repaired


# LEGACY_COMPAT: Responses history without reusable ordered provider output.
# Legacy format: Agno-only Responses spans retain a lossy reasoning tail and original function-call item IDs.
# Last legacy release: No universal writer cutoff; v2026.9.128 predates ordered capture in v2026.9.129,
# but current stored, nonportable responses can still omit ordered output.
# Handling: When ordered output cannot be reused, remove the tail and optional item IDs from request-only copies.
# Coverage: tests/providers/test_openai_family_requests.py::test_legacy_responses_replay_rebuilds_calls_without_reasoning
def repair_legacy_responses_span(message: Message, formatted_span: list[Any]) -> list[dict[str, Any]]:
    """Remove unverifiable replay remnants while preserving normalized call/result links."""
    if not message.tool_calls:
        return formatted_span[:1]
    return [
        {key: value for key, value in item.items() if key != "id"}
        for item in formatted_span
        if isinstance(item, dict) and item.get("type") == "function_call"
    ]
