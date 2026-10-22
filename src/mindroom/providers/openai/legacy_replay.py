"""Historical message shapes the OpenAI-family formatters still read from stored runs.

Each repair returns request-only copies; stored history is never rewritten.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.llm.messages import Message

__all__ = ["repair_legacy_responses_span", "repair_legacy_tool_replay"]


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
