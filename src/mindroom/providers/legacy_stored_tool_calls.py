"""Stored tool-call shapes earlier releases wrote, which every wire still replays from history.

Each repair returns request-only copies; stored history is never rewritten.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.llm.messages import Message

__all__ = ["repair_legacy_tool_calls"]


# LEGACY_COMPAT: Persisted Anthropic tool calls without empty arguments.
# Legacy format: Agno writers omitted the arguments field for empty Anthropic tool calls.
# Last legacy release: v2026.9.43; replacement: v2026.9.44 included the upstream Agno 3.0.7 writer fix.
# Handling: Supply `{}` arguments in copies of affected calls, on every wire, until those histories are migrated
# or dropped.
# Coverage: tests/providers/test_openai_family_wire.py::test_legacy_tool_replay_supplies_missing_arguments,
# tests/providers/test_provider_families.py::test_every_family_replays_the_tool_calls_any_provider_stored


# LEGACY_COMPAT: Persisted sparse OpenAI tool placeholders and orphan results.
# Legacy format: Streamed OpenAI history retained id-only tool placeholders and their orphan tool results.
# Last legacy release: v2026.7.172; replacement: v2026.7.173 filtered sparse placeholders during writes.
# Handling: Remove each placeholder and its matching result on every wire while preserving unchanged message
# identities.
# Coverage: tests/providers/test_openai_family_wire.py::test_legacy_tool_replay_removes_placeholders_and_orphans,
# tests/providers/test_provider_families.py::test_every_family_replays_the_tool_calls_any_provider_stored
def repair_legacy_tool_calls(messages: Sequence[Message]) -> list[Message]:
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
