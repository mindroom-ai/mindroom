"""Compatibility repair for historical OpenAI tool-call replay."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agno.models.message import Message


# Legacy format: Agno writers omitted the arguments field for empty Anthropic tool calls.
# Last legacy release: v2026.9.43; replacement: v2026.9.44 included the upstream Agno 3.0.7 writer fix.
# Handling: Supply `{}` arguments in copies of affected calls until those histories are migrated or dropped.
# Coverage: tests/test_openai_models.py::test_chat_models_supply_missing_tool_arguments_without_mutating_history.


# Legacy format: Streamed OpenAI history retained id-only tool placeholders and their orphan tool results.
# Last legacy release: v2026.7.172; replacement: v2026.7.173 filtered sparse placeholders during writes.
# Handling: Remove each placeholder and its matching result while preserving unchanged message identities.
# Coverage: tests/test_openai_models.py::test_chat_models_remove_persisted_sparse_placeholder_and_orphan_result.
def repair_legacy_openai_tool_replay(messages: list[Message]) -> list[Message]:
    """Repair function calls and remove sparse-stream placeholders from replay."""
    normalized_messages: list[Message] = []
    removed_tool_call_ids: set[str] = set()
    for message in messages:
        if message.role == "tool" and message.tool_call_id in removed_tool_call_ids:
            continue
        if message.role != "assistant" or not message.tool_calls:
            normalized_messages.append(message)
            continue

        changed = False
        normalized_tool_calls: list[dict[str, Any]] = []
        for tool_call in message.tool_calls:
            function = tool_call.get("function")
            if not isinstance(function, dict):
                tool_call_id = tool_call.get("id")
                if isinstance(tool_call_id, str):
                    removed_tool_call_ids.add(tool_call_id)
                changed = True
                continue
            if "arguments" in function:
                normalized_tool_calls.append(tool_call)
                continue
            normalized_tool_calls.append(
                {
                    **tool_call,
                    "function": {**function, "arguments": "{}"},
                },
            )
            changed = True

        normalized_messages.append(
            message.model_copy(update={"tool_calls": normalized_tool_calls}) if changed else message,
        )
    return normalized_messages
