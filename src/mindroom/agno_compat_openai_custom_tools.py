"""Parse and replay OpenAI Responses freeform custom tool calls, which Agno ignores."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from agno.models.message import Message
    from openai.types.responses import ResponseCustomToolCall

# AGNO_COMPAT: Responses custom tool calls are dropped.
# Reason: Agno 3.0.9 sends custom tool definitions unchanged but parses only `function_call` output
# items, stream and non-stream, and replays every call as `function_call`, so a freeform call such as
# Codex's apply_patch never runs and cannot be replayed.
# Upstream issue: Tracking gap; searching agno-agi/agno issues and PRs for custom_tool_call, freeform
# tools, and apply_patch on October 9, 2026 found nothing.
# Upstream PR: None identified.
# Remove when: Agno parses `custom_tool_call` output items into tool calls and replays them as
# `custom_tool_call` and `custom_tool_call_output` input items.
# Coverage: tests/test_openai_custom_tools.py::test_end_to_end_apply_patch_edits_workspace_file;
# tests/test_openai_custom_tools.py::test_reasoning_order_survives_custom_call.

CUSTOM_TOOL_CALL = "custom_tool_call"


def custom_tool_call(item: ResponseCustomToolCall) -> dict[str, Any]:
    """Return an Agno tool call for one custom tool call item, with its raw text as the ``input`` argument."""
    return {
        "id": item.id,
        "call_id": item.call_id,
        "type": "function",
        "function": {"name": item.name, "arguments": json.dumps({"input": item.input})},
    }


def tool_calls_with_custom(tool_calls: list[dict[str, Any]], output_items: Iterable[Any]) -> list[dict[str, Any]]:
    """Return Agno's parsed function calls with custom tool calls inserted in output order."""
    items = list(output_items)
    if not any(item.type == CUSTOM_TOOL_CALL for item in items):
        return tool_calls
    remaining = {call.get("call_id"): call for call in tool_calls}
    ordered: list[dict[str, Any]] = []
    for item in items:
        if item.type == CUSTOM_TOOL_CALL:
            ordered.append(custom_tool_call(item))
        elif item.type == "function_call" and item.call_id in remaining:
            ordered.append(remaining.pop(item.call_id))
    return [*ordered, *remaining.values()]


def _custom_item(item: dict[str, Any]) -> dict[str, Any]:
    if item["type"] == "function_call_output":
        return {"type": "custom_tool_call_output", "call_id": item["call_id"], "output": item["output"]}
    try:
        arguments = json.loads(item.get("arguments") or "{}")
    except json.JSONDecodeError:
        arguments = {}
    patch_input = arguments.get("input") if isinstance(arguments, dict) else None
    # Agno rewrites foreign item IDs to function-call IDs, so the replayed item omits its ID.
    return {
        "type": CUSTOM_TOOL_CALL,
        "call_id": item["call_id"],
        "name": item["name"],
        "input": patch_input if isinstance(patch_input, str) else "",
    }


def replay_custom_tool_items(
    formatted_input: list[Any],
    tools: Sequence[Any] | None,
    messages: Sequence[Message],
) -> list[Any]:
    """Return Agno's formatted Responses input with calls to this request's custom tools in their own item types.

    Call IDs come from the whole history, because a stored-response continuation sends a result without its call.
    """
    custom_names = {tool.get("name") for tool in tools or [] if isinstance(tool, dict) and tool.get("type") == "custom"}
    if not custom_names:
        return formatted_input
    custom_call_ids = {
        call_id
        for message in messages
        for call in message.tool_calls or []
        if call.get("function", {}).get("name") in custom_names
        for call_id in (call.get("call_id"), call.get("id"))
        if isinstance(call_id, str)
    } | {
        item.get("call_id")
        for item in formatted_input
        if isinstance(item, dict) and item.get("type") == "function_call" and item.get("name") in custom_names
    }
    return [
        _custom_item(item)
        if isinstance(item, dict)
        and item.get("type") in {"function_call", "function_call_output"}
        and item.get("call_id") in custom_call_ids
        else item
        for item in formatted_input
    ]
