"""Parse and replay OpenAI Responses freeform custom tool calls, which Agno ignores."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from agno.models.message import Message
    from agno.models.response import ModelResponse
    from openai.types.responses import ResponseCustomToolCall

CUSTOM_TOOL_CALL = "custom_tool_call"


# AGNO_COMPAT: Responses custom tool call output items are not parsed.
# Reason: Agno 3.0.9 sends custom tool definitions unchanged but parses only `function_call` output
# items, stream and non-stream, so a freeform call such as Codex's apply_patch never runs.
# Upstream issue: Tracking gap; searching agno-agi/agno issues and PRs for custom_tool_call, freeform
# tools, and apply_patch on October 9, 2026 found nothing.
# Upstream PR: None identified.
# Remove when: Agno parses `custom_tool_call` output items into tool calls, in output order, for streamed
# and non-streamed responses.
# Coverage: tests/test_openai_custom_tools.py::test_end_to_end_apply_patch_edits_workspace_file.


def _custom_tool_call(item: ResponseCustomToolCall) -> dict[str, Any]:
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
            ordered.append(_custom_tool_call(item))
        elif item.type == "function_call" and item.call_id in remaining:
            ordered.append(remaining.pop(item.call_id))
    return [*ordered, *remaining.values()]


def record_streamed_custom_tool_call(model_response: ModelResponse, assistant_message: Message, item: Any) -> None:  # noqa: ANN401
    """Record a completed streamed custom tool call the way Agno records a completed function call."""
    if item.type != CUSTOM_TOOL_CALL:
        return
    call = _custom_tool_call(item)
    model_response.tool_calls = [call]
    assistant_message.tool_calls = [*(assistant_message.tool_calls or []), call]


# AGNO_COMPAT: Responses custom tool calls are replayed as function calls.
# Reason: Agno 3.0.9 formats every assistant tool call and result as `function_call` and
# `function_call_output` input items, which the Responses API rejects for a custom tool.
# Upstream issue: Tracking gap; searching agno-agi/agno issues and PRs for custom_tool_call replay on
# October 9, 2026 found nothing.
# Upstream PR: None identified.
# Remove when: Agno replays calls to custom tools as `custom_tool_call` and `custom_tool_call_output`
# input items, including results sent on a stored-response continuation.
# Coverage: tests/test_openai_custom_tools.py::test_reasoning_order_survives_custom_call;
# tests/test_openai_custom_tools.py::test_stored_continuation_sends_custom_output.
def _custom_item(item: dict[str, Any]) -> dict[str, Any]:
    if item["type"] == "function_call_output":
        return {"type": "custom_tool_call_output", "call_id": item["call_id"], "output": item["output"]}
    try:
        arguments = json.loads(item.get("arguments") or "{}")
    except ValueError:
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
